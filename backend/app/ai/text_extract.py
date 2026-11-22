"""一句话记账的文本抽取：优先大模型，失败退回规则。

对外只有 extract()，返回的 ExpenseExtraction 与大模型无关：
engine 标明这次结果来自 llm 还是 rule，接口层据此决定置信度和提示文案。
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from app.ai import llm, parsing, rules
from app.schemas.transaction import Category

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是个人记账助手的字段抽取器。从用户的一句话里提取一笔消费，只输出一个 JSON 对象：
{"amount": 数字或 null, "merchant": 字符串或 null, "occurred_at": "ISO8601 带时区"或 null, "category": 字符串或 null, "confidence": 0 到 1 的小数}

规则：
1. amount 单位是元，只给数字，不带符号；
2. category 只能是 餐饮、交通、购物、娱乐、医疗、居住、其他 之一，判断不了写 null；
3. occurred_at 根据参考时间推算相对说法（今天、昨天、上周五），必须带 +08:00；
4. 用户没提到的字段一律写 null，不要编造；
5. confidence 表示你对整条结果的把握程度。

参考时间：{now}
"""


@dataclass(frozen=True)
class ExpenseExtraction:
    """一次文本抽取的结果。

    missing_fields 不在这里计算，接口层会根据值为 None 的字段自动得出。
    """

    amount_cents: int | None
    merchant: str | None
    occurred_at: datetime | None
    category: Category | None
    confidence: float
    engine: str


def extract(text: str) -> ExpenseExtraction:
    """把一句话解析成结构化支出。

    参数:
        text: 用户输入的自然语言，已去除首尾空格，长度 1~200。

    返回:
        ExpenseExtraction：未识别字段为 None；engine 为 llm 或 rule。
    """
    if llm.configured():
        try:
            payload = llm.extract_json(
                llm.TASK_TEXT,
                # 提示词里有 JSON 花括号，str.format 会把它当占位符，这里只做字符串替换。
                _SYSTEM_PROMPT.replace(
                    "{now}",
                    datetime.now(parsing.APP_TZ).isoformat(timespec="minutes"),
                ),
                text,
            )
            return ExpenseExtraction(
                amount_cents=parsing.amount_cents(payload.get("amount")),
                merchant=parsing.merchant(payload.get("merchant")),
                occurred_at=parsing.occurred_at(payload.get("occurred_at")),
                category=parsing.category(payload.get("category")),
                confidence=parsing.confidence(payload.get("confidence")),
                engine="llm",
            )
        except llm.AiUnavailable as exc:
            # 模型不可用不影响记账，退回规则解析并在日志里留痕。
            logger.warning("文本抽取退回规则解析：%s", exc)

    parsed = rules.parse_text(text)
    return ExpenseExtraction(
        amount_cents=parsed["amount_cents"],
        merchant=parsed["merchant"],
        occurred_at=parsed["occurred_at"],
        category=parsed["category"],
        confidence=parsed["confidence"],
        engine="rule",
    )
