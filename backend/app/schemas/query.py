"""自然语言查账的请求与响应模型。

金额与其它接口一致用「分」；时间进出都是带时区的 UTC。
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.schemas.transaction import Category, Transaction


class QueryRequest(BaseModel):
    """POST /query/parse 和 POST /query/ask 的请求体。"""

    question: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="用户的口语化问题",
        examples=["这个月吃饭花了多少钱"],
    )

    @field_validator("question")
    @classmethod
    def _strip_question(cls, value: str) -> str:
        """去掉首尾空格，并拒绝纯空格输入。"""
        value = value.strip()
        if not value:
            raise ValueError("question 不能为空")
        return value


class QueryIntentOut(BaseModel):
    """POST /query/parse 的响应：只给解析出的查询条件。"""

    category: Category | None = Field(None, description="限定的类别；未限定为 null")
    date_from: datetime | None = Field(None, description="起始时间，UTC；未限定为 null")
    date_to: datetime | None = Field(None, description="结束时间，UTC；未限定为 null")
    intent: str = Field(..., description="total=合计金额，count=笔数，top=哪类最多，list=明细")
    confidence: float = Field(..., ge=0.0, le=1.0, description="解析置信度")
    engine: str = Field(..., description="llm=大模型解析，rule=规则兜底")


class QueryAnswer(BaseModel):
    """POST /query/ask 的响应：查询条件 + 查到的数据 + 一句可直接展示的结论。"""

    question: str = Field(..., description="原始问题")
    intent: str = Field(..., description="total/count/top/list")
    category: Category | None = Field(None, description="限定的类别")
    date_from: datetime | None = Field(None, description="起始时间，UTC")
    date_to: datetime | None = Field(None, description="结束时间，UTC")
    total_cents: int = Field(..., description="命中范围的合计金额，单位「分」")
    count: int = Field(..., description="命中范围的账目笔数")
    answer: str = Field(..., description="结论文案，可直接显示")
    items: list[Transaction] = Field(default_factory=list, description="明细，仅 list 意图有值")
    engine: str = Field(..., description="llm=大模型解析，rule=规则兜底")
