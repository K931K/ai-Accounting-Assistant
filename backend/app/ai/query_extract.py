"""自然语言查账的条件解析：优先大模型，失败退回规则。

只负责把问句变成查询条件，不碰数据库：接口层拿到 QueryIntent 后再调用 store。
intent 取值 total（合计金额）、count（笔数）、top（哪类最多）、list（明细列表）。
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from app.ai import llm, parsing, rules
from app.schemas.transaction import Category

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是记账应用的查询条件解析器。把用户的问题转成一个 JSON 对象：
{"category": 字符串或 null, "date_from": "ISO8601 带时区"或 null, "date_to": "ISO8601 带时区"或 null, "intent": "total" 或 "count" 或 "top" 或 "list", "confidence": 0 到 1 的小数}

规则：
1. category 只能是 餐饮、交通、购物、娱乐、医疗、居住、其他 之一，问题没限定类别就写 null；
2. date_from / date_to 是消费时间的闭区间，根据参考时间推算（这个月、上个月、今年以来），必须带 +08:00；
3. intent：问「多少钱 / 花了多少」用 total；问「几笔 / 几次」用 count；问「哪类最多」用 top；要明细或最近几条用 list；
4. 问题里没有的限定条件一律写 null，不要编造。

参考时间：{now}
"""


@dataclass(frozen=True)
class QueryIntent:
    """一次问句解析的结果。"""

    category: Category | None
    date_from: datetime | None
    date_to: datetime | None
    intent: str
    confidence: float
    engine: str


def _intent(value: object) -> str:
    """模型给的意图必须落在四个取值内，其余按 total 处理。"""
    allowed = {"total", "count", "top", "list"}
    return value if isinstance(value, str) and value in allowed else "total"


def parse(question: str, now: datetime) -> QueryIntent:
    """把问句解析成查询条件。

    参数:
        question: 用户问题，已去首尾空格。
        now: 当前时间，用于规则兜底推算相对日期。

    返回:
        QueryIntent：没有限定条件时 category 与起止时间都为 None。
    """
    if llm.configured():
        try:
            payload = llm.extract_json(
                llm.TASK_QUERY,
                _SYSTEM_PROMPT.format(now=now.astimezone(parsing.APP_TZ).isoformat(timespec="minutes")),
                question,
            )
            date_from, date_to = parsing.date_range(
                payload.get("date_from"), payload.get("date_to")
            )
            return QueryIntent(
                category=parsing.category(payload.get("category")),
                date_from=date_from,
                date_to=date_to,
                intent=_intent(payload.get("intent")),
                confidence=parsing.confidence(payload.get("confidence")),
                engine="llm",
            )
        except llm.AiUnavailable as exc:
            logger.warning("查询解析退回规则：%s", exc)

    parsed = rules.parse_question(question, now)
    return QueryIntent(
        category=parsed["category"],
        date_from=parsed["date_from"],
        date_to=parsed["date_to"],
        intent=parsed["intent"],
        confidence=parsed["confidence"],
        engine="rule",
    )
