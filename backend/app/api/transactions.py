"""记账接口。

四个路由的分工：
- POST /transactions/parse：规则解析一句话，写入 transaction_drafts，返回待确认草稿；
- POST /transactions：把确认后的字段写入 transactions，并消费对应草稿；
- GET /transactions：从 transactions 读取分页列表；
- DELETE /transactions/{id}：按 id 删除单条账目。
路由层只处理 HTTP 入参、状态码和响应组装；SQL、排序和事务在 app.store 中。
"""

import re
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query, status

from app import store
from app.core.errors import (
    VALIDATION_ERROR_RESPONSES,
    ApiError,
    ErrorResponse,
)
from app.db import ConnDep
from app.schemas.transaction import (
    Category,
    CreateTransactionRequest,
    ParseTextRequest,
    Source,
    Transaction,
    TransactionDraft,
    TransactionList,
    TransactionQuery,
)

router = APIRouter(
    prefix="/transactions",
    tags=["记账"],
    # 三个路由的 422 都使用统一 ErrorResponse，而不是 FastAPI 默认结构。
    responses=VALIDATION_ERROR_RESPONSES,
)

# 规则解析的关键词表：
# key 是在 text 中查找的子串，value 是命中后写入 TransactionDraft.category 的枚举值。
# parse_text() 按字典插入顺序遍历，命中第一个关键词就停止，因此「具体词」必须写在
# 「宽泛词」前面：药、医院要排在「买」之前，否则「买药」会被判成购物。
_KEYWORD_CATEGORY = {
    # 餐饮
    "饭": Category.FOOD, "午餐": Category.FOOD, "晚饭": Category.FOOD,
    "早饭": Category.FOOD, "早餐": Category.FOOD, "外卖": Category.FOOD,
    "咖啡": Category.FOOD, "奶茶": Category.FOOD, "饮料": Category.FOOD,
    "零食": Category.FOOD,
    # 医疗
    "药": Category.MEDICAL, "医院": Category.MEDICAL, "挂号": Category.MEDICAL,
    "体检": Category.MEDICAL,
    # 交通
    "打车": Category.TRANSPORT, "地铁": Category.TRANSPORT, "公交": Category.TRANSPORT,
    "加油": Category.TRANSPORT, "高铁": Category.TRANSPORT, "机票": Category.TRANSPORT,
    # 居住
    "房租": Category.HOUSING, "水电": Category.HOUSING, "物业": Category.HOUSING,
    "燃气": Category.HOUSING,
    # 娱乐
    "电影": Category.ENTERTAINMENT, "游戏": Category.ENTERTAINMENT,
    "演唱会": Category.ENTERTAINMENT, "旅游": Category.ENTERTAINMENT,
    # 购物：宽泛的「买」放最后，避免抢走前面更具体的类别
    "淘宝": Category.SHOPPING, "京东": Category.SHOPPING, "超市": Category.SHOPPING,
    "商场": Category.SHOPPING, "买": Category.SHOPPING,
}

# POST /transactions/parse 的 OpenAPI 200 示例。
# 只用于文档展示；实际返回由 TransactionDraft 序列化，未识别字段会返回 null。
_DRAFT_EXAMPLE = {
    "draft_id": "6f1c9b2e8a4d4c1f9b7e5a3d2c1b0a9e",
    "source": "text",
    "amount_cents": 2500,
    "merchant": None,
    "occurred_at": "2026-11-11T04:30:00Z",
    "category": "餐饮",
    "confidence": 0.4,
    "missing_fields": ["merchant", "occurred_at"],
    "raw_text": "午饭 25，和同事 AA",
}


@router.post(
    "/parse",
    response_model=TransactionDraft,
    summary="一句话记账：解析消费信息",
    description=(
        "将自然语言转换为结构化记账信息。\n\n"
        "amount_cents 可能为 null，表示未解析出金额，补齐后调用 POST /transactions 保存。"
    ),
    responses={200: {"content": {"application/json": {"example": _DRAFT_EXAMPLE}}}},
)
def parse_text(payload: ParseTextRequest, conn: ConnDep) -> TransactionDraft:
    """将一句话解析成待确认的草稿，并写入 transaction_drafts。

    参数:
        payload: 请求体。text 已由 ParseTextRequest 去掉首尾空格，长度 1~200。
        conn: 请求级连接，交给 store.save_draft() 写入草稿。

    返回:
        TransactionDraft：source 固定为 text；amount_cents 或 category 可能为 None；
        occurred_at 恒为 None；missing_fields 列出所有值为 None 的字段。

    调用关系:
        store.new_draft_id() 生成 draft_id -> 组装 TransactionDraft ->
        store.save_draft() 执行 INSERT 并 commit。
    """
    # 上层 schema 已完成去空格和长度校验，这里直接使用清理后的文本。
    text = payload.text

    # 只匹配第一个连续数字（允许 1~2 位小数），不区分“元/块”等单位。
    # match.group(1) 是数字字符串，例如 "25" 或 "25.50"。
    match = re.search(r"(\d+(?:\.\d{1,2})?)", text)
    amount_cents: int | None = None
    if match:
        # 元转分：Decimal 避免 25.1 * 100 的浮点误差；to_integral_value() 再取整。
        amount_cents = int((Decimal(match.group(1)) * 100).to_integral_value())

    # 按 _KEYWORD_CATEGORY 的插入顺序匹配第一个关键词；
    # value 是 Category 枚举，命中后 break，未命中时 category 保持 None。
    category: Category | None = None
    for keyword, value in _KEYWORD_CATEGORY.items():
        if keyword in text:
            category = value
            break

    draft = TransactionDraft(
        # draft_id 与 transaction_drafts.draft_id 对应，保存账目时回传这个值。
        draft_id=store.new_draft_id(),
        source=Source.TEXT,
        amount_cents=amount_cents,
        merchant=None,
        # 规则解析不做时间抽取，留空由用户确认时补。
        occurred_at=None,
        category=category,
        # 规则解析的固定置信度，不是模型概率。
        confidence=0.4,
        # 只收集值为 None 的字段名，顺序与 TransactionDraft 的声明顺序一致；
        # 前端按这个数组高亮待补字段。
        missing_fields=[
            name
            for name, value in (
                ("amount_cents", amount_cents),
                ("merchant", None),
                ("occurred_at", None),
                ("category", category),
            )
            if value is None
        ],
        raw_text=text,
    )
    # save_draft() 写入 transaction_drafts 并 commit；返回同一 draft 作为响应。
    return store.save_draft(conn, draft)


@router.post(
    "",
    response_model=Transaction,
    status_code=status.HTTP_201_CREATED,
    summary="保存一笔账",
    description=(
        "保存一笔已确认的账目。\n\n"
        "传入 draft_id 时按草稿推断来源并删除草稿；不传时视为手工记账。"
    ),
    responses={
        201: {
            "content": {
                "application/json": {
                    "example": {
                        "id": 1,
                        "amount_cents": 2500,
                        "merchant": "食堂",
                        "occurred_at": "2026-11-11T04:30:00Z",
                        "category": "餐饮",
                        "source": "text",
                        "note": "和同事 AA",
                        "created_at": "2026-11-11T04:31:05Z",
                    }
                }
            }
        }
    },
)
def create_transaction(payload: CreateTransactionRequest, conn: ConnDep) -> Transaction:
    """把已确认的账目写入 transactions。

    参数:
        payload: CreateTransactionRequest；金额、商户、时间、类别已通过 schema 校验，
                 occurred_at 已转 UTC，note 空字符串已转 None。
        conn: 请求级连接，交给 store.create_transaction()。

    返回:
        Transaction：入库后的账目，包含数据库生成的 id 和 created_at。

    调用关系:
        store.create_transaction() 会按 payload.draft_id 查草稿：
        - 找到草稿：优先使用请求的 source，否则用草稿的 source，并删除草稿；
        - 未找到或未传：source 为空时记为 manual。
        草稿删除与账目插入在同一次 commit 中完成。
    """
    # 字段原样交给 store；来源推断、草稿消费和事务提交都在 store 内完成。
    return store.create_transaction(
        conn,
        amount_cents=payload.amount_cents,
        merchant=payload.merchant,
        occurred_at=payload.occurred_at,
        category=payload.category,
        source=payload.source,
        note=payload.note,
        draft_id=payload.draft_id,
    )


@router.delete(
    "/{transaction_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除一笔账",
    description="按 id 删除账目，成功返回 204 且没有响应体；id 不存在时返回 404。",
    responses={404: {"model": ErrorResponse, "description": "账目不存在"}},
)
def delete_transaction(transaction_id: int, conn: ConnDep) -> None:
    """删除 transactions 中的一条记录。

    参数:
        transaction_id: 路径参数，对应 transactions.id，由 FastAPI 转成 int。
        conn: 请求级连接，交给 store.delete_transaction()。

    返回:
        成功时无响应体（204）；id 不存在时抛 ApiError，由统一异常处理器转成
        {"code": "NOT_FOUND", "message": "账目不存在", "detail": {}}。
    """
    if not store.delete_transaction(conn, transaction_id):
        raise ApiError(
            code="NOT_FOUND",
            message="账目不存在",
            status_code=status.HTTP_404_NOT_FOUND,
        )


@router.get(
    "",
    response_model=TransactionList,
    summary="查询账目列表",
    description="支持时间范围、类别筛选与分页。",
)
def list_transactions(
    params: Annotated[TransactionQuery, Query()],
    conn: ConnDep,
) -> TransactionList:
    """从 transactions 读取分页账目。

    参数:
        params: TransactionQuery；FastAPI 从 query string 解析
                from/to/category/limit/offset。DateRangeQuery 已校验时区并转 UTC，
                且保证 from 不晚于 to。
        conn: 请求级连接，交给 store.list_transactions()。

    返回:
        TransactionList：items 是当前页账目，total 是符合筛选条件的总条数。

    调用关系:
        store.list_transactions() 先执行 COUNT 得到 total，再执行
        SELECT + ORDER BY occurred_at DESC, id DESC + LIMIT/OFFSET。
    """
    items, total = store.list_transactions(
        conn,
        date_from=params.date_from,
        date_to=params.date_to,
        category=params.category,
        limit=params.limit,
        offset=params.offset,
    )
    return TransactionList(items=items, total=total)
