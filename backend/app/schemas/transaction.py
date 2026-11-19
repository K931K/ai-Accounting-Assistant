"""记账相关的 Pydantic 请求/响应模型。

这些模型是 HTTP 契约，不是数据库表；数据库表由 app/db.py 的建表语句创建。
- 请求模型负责字段类型、长度、时区和枚举校验；
- 响应模型负责把 store 层的结果序列化成 JSON；
- 字段命名统一 snake_case，金额单位「分」，时间入参必须带时区，响应统一 UTC。
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import DateRangeQuery, ensure_utc


class Category(str, Enum):
    """消费类别枚举。

    API、数据库和 /categories 接口共用这组中文值；
    写入数据库的是枚举的 value，不是 Python 枚举名。
    """

    FOOD = "餐饮"
    TRANSPORT = "交通"
    SHOPPING = "购物"
    ENTERTAINMENT = "娱乐"
    MEDICAL = "医疗"
    HOUSING = "居住"
    OTHER = "其他"


class Source(str, Enum):
    """记账来源枚举。

    API、数据库和解析草稿共用这组值：
    - text：POST /transactions/parse 生成的草稿；
    - receipt：POST /receipts/parse 生成的草稿；
    - manual：保存时没有草稿、也没有显式 source 的默认值。
    """

    TEXT = "text"
    RECEIPT = "receipt"
    MANUAL = "manual"


class ParseTextRequest(BaseModel):
    """POST /transactions/parse 的请求体。

    text 由路由函数 parse_text() 读取；长度 1~200，去首尾空格后不能为空。
    """

    text: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="用户输入的自然语言文本",
        examples=["午饭 25，和同事 AA"],
    )

    @field_validator("text")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        """去掉首尾空格，并拒绝纯空格输入。

        min_length 只能拦住空字符串，拦不住 "   "；
        这里再 strip 一次，保证 parse_text() 拿到的是有效文本。
        """
        value = value.strip()
        if not value:
            raise ValueError("text 不能为空")
        return value


class TransactionDraft(BaseModel):
    """解析接口返回的草稿，也是 store.save_draft() 的入参。

    各字段允许为 null，表示该项未识别；missing_fields 列出所有 null 字段名，
    前端据此把对应输入框标记为待补项。
    """

    # 允许从字典构造；当前路由直接构造草稿。
    model_config = ConfigDict(from_attributes=True)

    draft_id: str = Field(..., description="草稿 id，提交时回传")
    source: Source = Field(..., description="来源：text=一句话，receipt=小票图片")
    amount_cents: int | None = Field(None, description="金额，单位「分」；未识别时为 null")
    merchant: str | None = Field(None, description="商户名；未识别时为 null")
    occurred_at: datetime | None = Field(None, description="消费时间，UTC；未识别时为 null")
    category: Category | None = Field(None, description="分类结果；未识别时为 null")
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="本次抽取的置信度，0~1")
    missing_fields: list[str] = Field(
        default_factory=list,
        description="未识别出的字段名列表",
    )
    raw_text: str | None = Field(None, description="原始文本")
    engine: str = Field(
        "rule",
        description="解析引擎：llm=大模型，rule=规则兜底，none=未识别（图片未配置视觉模型）",
    )

    @field_validator("occurred_at")
    @classmethod
    def _normalize_occurred_at(cls, value: datetime | None) -> datetime | None:
        """草稿时间如果存在，统一转成 UTC。

        当前规则解析不产出时间，值为 None；后续接入模型后仍走同一校验。
        """
        return None if value is None else ensure_utc(value)


class CreateTransactionRequest(BaseModel):
    """POST /transactions 的请求体。

    字段由 store.create_transaction() 使用：
    - draft_id 指向 transaction_drafts，可为空；
    - source 为空时由草稿推断，仍为空则记 manual；
    - 其余字段必填，写进 transactions。
    """

    draft_id: str | None = Field(
        None,
        max_length=32,
        description="解析草稿 id；不传表示手工记账",
    )
    source: Source | None = Field(
        None,
        description="来源；不传时按草稿推断，无草稿则记为 manual",
    )
    amount_cents: int = Field(..., gt=0, description="金额，单位「分」，必须大于 0")
    merchant: str = Field(..., min_length=1, max_length=30, description="商户名")
    occurred_at: datetime = Field(..., description="消费时间，ISO 8601 带时区")
    category: Category = Field(..., description="消费类别")
    note: str | None = Field(None, max_length=200, description="备注")

    @field_validator("merchant")
    @classmethod
    def _strip_merchant(cls, value: str) -> str:
        """去掉商户名首尾空格，并拒绝纯空格输入。

        与数据库 merchant NOT NULL 对应，避免把 "   " 当作有效商户入库。
        """
        value = value.strip()
        if not value:
            raise ValueError("merchant 不能为空")
        return value

    @field_validator("occurred_at")
    @classmethod
    def _normalize_occurred_at(cls, value: datetime) -> datetime:
        """消费时间必须带时区，并转换为 UTC 后写入 transactions.occurred_at。"""
        return ensure_utc(value)

    @field_validator("note")
    @classmethod
    def _blank_note_to_none(cls, value: str | None) -> str | None:
        """备注去首尾空格；空字符串转 None，与数据库 note 允许 NULL 对应。"""
        if value is None:
            return None
        value = value.strip()
        return value or None


class Transaction(BaseModel):
    """保存成功后的账目响应，对应 transactions 表的一行。

    store.create_transaction() 用数据库返回的一行构造；
    id 和 created_at 由数据库生成，时间字段统一为 UTC。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="账目 id")
    amount_cents: int = Field(..., description="金额，单位「分」")
    merchant: str = Field(..., description="商户名")
    occurred_at: datetime = Field(..., description="消费时间，UTC")
    category: Category = Field(..., description="消费类别")
    source: Source = Field(..., description="记账来源")
    note: str | None = Field(None, description="备注，可为空")
    created_at: datetime = Field(..., description="入库时间，UTC")


class TransactionQuery(DateRangeQuery):
    """GET /transactions 的查询参数。

    FastAPI 从 query string 解析这些字段；DateRangeQuery 负责 from/to 的时区与顺序校验，
    本类补充 category、limit、offset，最终由 store.list_transactions() 拼 SQL。
    """

    category: Category | None = Field(None, description="按类别筛选")
    limit: int = Field(20, ge=1, le=100, description="每页条数")
    offset: int = Field(0, ge=0, description="偏移量")


class TransactionList(BaseModel):
    """分页查询结果，对应 GET /transactions 的响应。

    items 是当前页账目；total 是符合筛选条件的总条数，用于计算总页数。
    """

    items: list[Transaction] = Field(..., description="当前页账目")
    total: int = Field(..., description="符合条件的总数")
