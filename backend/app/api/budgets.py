"""预算接口：设置、查询与超支提醒。
 1.GET /budgets?month=YYYY-MM：返回该月预算，并给出当月实际支出、使用比例和状态；
 2.PUT /budgets：按「月份 + 类别」覆盖写入一条预算；
 3.DELETE /budgets/{id}：删除一条预算。
状态判定：已用比例达到 1 为 exceeded，达到该预算行的 threshold（缺省 0.8）为 warning，其余 normal。
"""

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app import store
from app.core.errors import VALIDATION_ERROR_RESPONSES, ApiError
from app.db import ConnDep
from app.schemas.budget import (
    BudgetItem,
    BudgetOverview,
    BudgetStatus,
    BudgetUpsertRequest,
)
from app.schemas.transaction import Category

router = APIRouter(
    prefix="/budgets",
    tags=["预算"],
    # 422 使用统一 ErrorResponse，与其余业务路由保持一致。
    responses=VALIDATION_ERROR_RESPONSES,
)

# 月份边界按北京时间计算：用户说的「11 月」指 11-01 00:00 到 11-30 23:59:59。
_APP_TZ = timezone(timedelta(hours=8))

# 返回列表时总预算行排最前，其余按 Category 声明顺序。
_CATEGORY_ORDER = {category: index for index, category in enumerate(Category)}

# 月份查询参数只接受 YYYY-MM，与 BudgetUpsertRequest 的校验保持一致。
_MONTH_QUERY = Query(
    ...,
    pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    description="月份，格式 YYYY-MM",
    examples=["2026-11"],
)


def _month_range(month: str) -> tuple[datetime, datetime]:
    """把 YYYY-MM 转成当月首尾时间（北京时间，含毫秒精度）。

    结束时间取「下月 1 日 00:00」减 1 微秒，
    这样 store.category_totals() 的 <= date_to 不会把下月第一笔算进来。
    """
    year, month_number = int(month[:4]), int(month[5:])
    start = datetime(year, month_number, 1, tzinfo=_APP_TZ)
    next_month = datetime(
        year + (month_number == 12),
        month_number % 12 + 1,
        1,
        tzinfo=_APP_TZ,
    )
    return start, next_month - timedelta(microseconds=1)


def _status_of(ratio: float, threshold: float) -> str:
    """按已用比例与预算阈值给出状态字符串。

    threshold 来自 budgets.threshold（缺省 0.8），只用于 warning 判定；
    花光预算（ratio >= 1）无论阈值多少都算 exceeded。
    """
    if ratio >= 1:
        return "exceeded"
    if ratio >= threshold:
        return "warning"
    return "normal"


@router.get(
    "",
    response_model=BudgetOverview,
    summary="查询月度预算与使用状态",
    description="返回当月预算列表，并按当月实际支出计算 ratio 与 status。",
)
def get_budgets(month: Annotated[str, _MONTH_QUERY], conn: ConnDep) -> BudgetOverview:
    """读取某月预算并计算使用状态。

    参数:
        month: 查询参数，格式 YYYY-MM。
        conn: 请求级连接。

    返回:
        BudgetOverview：spent_cents 是当月总支出；
        items 中总预算行（category 为 null）排在最前，其后按类别顺序。
    """
    date_from, date_to = _month_range(month)
    rows = store.list_budgets(conn, month)
    totals, month_spent = store.category_totals(
        conn,
        date_from=date_from,
        date_to=date_to,
    )
    spent_by_category = {item.category: item.total_cents for item in totals}

    items: list[BudgetStatus] = []
    for row in rows:
        if row.category:
            category = Category(row.category)
            spent = spent_by_category.get(category, 0)
        else:
            category = None
            spent = month_spent
        ratio = spent / row.amount_cents
        items.append(
            BudgetStatus(
                id=row.id,
                category=category,
                amount_cents=row.amount_cents,
                spent_cents=spent,
                ratio=ratio,
                status=_status_of(ratio, row.threshold),
            )
        )
    items.sort(key=lambda item: (item.category is not None, _CATEGORY_ORDER.get(item.category, 99)))
    return BudgetOverview(month=month, spent_cents=month_spent, items=items)


@router.put(
    "",
    response_model=BudgetItem,
    summary="设置月度预算",
    description="同一月份同一类别重复提交时覆盖原金额。",
)
def upsert_budget(payload: BudgetUpsertRequest, conn: ConnDep) -> BudgetItem:
    """写入或覆盖一条预算。

    参数:
        payload: month / category / amount_cents；category 为 null 表示总预算。
        conn: 请求级连接。

    返回:
        BudgetItem：落库后的预算，包含 id。
    """
    row = store.upsert_budget(
        conn,
        month=payload.month,
        category=payload.category.value if payload.category else "",
        amount_cents=payload.amount_cents,
    )
    return BudgetItem(
        id=row.id,
        month=row.month,
        category=Category(row.category) if row.category else None,
        amount_cents=row.amount_cents,
    )


@router.delete(
    "/{budget_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除预算",
    responses={404: {"description": "预算不存在"}},
)
def delete_budget(
    budget_id: Annotated[int, Path(description="预算 id")],
    conn: ConnDep,
) -> None:
    """删除一条预算；不存在时返回 404。"""
    if not store.delete_budget(conn, budget_id):
        raise ApiError(
            code="BUDGET_NOT_FOUND",
            message="预算不存在",
            status_code=status.HTTP_404_NOT_FOUND,
        )
