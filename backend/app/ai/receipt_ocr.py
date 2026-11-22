"""小票与支付截图的识别：用视觉模型抽取字段。

recognize() 永远返回 ReceiptRecognition，不抛异常：
未配置视觉模型或调用失败时所有字段为 None、engine 为 none，
接口层据此提示用户手动补全，上传功能本身不受影响。
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from app.ai import llm, parsing
from app.schemas.transaction import Category

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是小票识别助手。看图片里的消费凭证，输出一个 JSON 对象：
{"amount": 数字或 null, "merchant": 字符串或 null, "occurred_at": "ISO8601 带时区"或 null, "category": 字符串或 null, "raw_text": 图片里的关键文字, "confidence": 0 到 1 的小数}

规则：
1. amount 是实付金额，单位元，只给数字；
2. category 只能是 餐饮、交通、购物、娱乐、医疗、居住、其他 之一；
3. occurred_at 取小票上的交易时间，带 +08:00；看不清写 null；
4. raw_text 保留金额、商户、时间等关键文字，便于人工核对；
5. 图片里没有的信息一律写 null，不要猜。
"""


@dataclass(frozen=True)
class ReceiptRecognition:
    """一次小票识别的结果。

    raw_text 保留识别到的文字，便于人工核对，也可作为规则抽取的输入。
    """

    raw_text: str | None
    amount_cents: int | None
    merchant: str | None
    occurred_at: datetime | None
    category: Category | None
    confidence: float
    engine: str


def _empty() -> ReceiptRecognition:
    """未识别时的空结果，字段全空、置信度为 0。"""
    return ReceiptRecognition(
        raw_text=None,
        amount_cents=None,
        merchant=None,
        occurred_at=None,
        category=None,
        confidence=0.0,
        engine="none",
    )


def recognize(image: bytes, mime: str) -> ReceiptRecognition:
    """识别一张小票或支付截图。

    参数:
        image: 上传文件的内容字节，MIME 和大小已由接口层校验。
        mime: 图片的 MIME 类型，用于拼 data URL。

    返回:
        ReceiptRecognition：未识别字段为 None；engine 为 llm 或 none。
    """
    if not llm.vision_configured():
        return _empty()

    try:
        payload = llm.extract_json(
            llm.TASK_RECEIPT,
            _SYSTEM_PROMPT,
            "请识别这张消费凭证。",
            image=image,
            mime=mime,
        )
    except llm.AiUnavailable as exc:
        logger.warning("小票识别失败，返回空草稿：%s", exc)
        return _empty()

    raw_text = payload.get("raw_text")
    return ReceiptRecognition(
        raw_text=raw_text.strip()[:2000] if isinstance(raw_text, str) and raw_text.strip() else None,
        amount_cents=parsing.amount_cents(payload.get("amount")),
        merchant=parsing.merchant(payload.get("merchant")),
        occurred_at=parsing.occurred_at(payload.get("occurred_at")),
        category=parsing.category(payload.get("category")),
        confidence=parsing.confidence(payload.get("confidence")),
        engine="llm",
    )
