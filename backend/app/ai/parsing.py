"""模型输出的字段清洗：把大模型返回的 JSON 收敛成本系统认识的类型。

模型可能给出字符串数字、缺时区的时间、枚举外的类别，这里统一处理：
- 金额：元 → 分，非正数视为未识别；
- 时间：必须转成带 UTC 时区的 datetime；
- 类别：必须落在 Category 枚举内；
- 商户：去掉首尾空格并截断到数据库列宽。

text_extract、receipt_ocr、query_extract 共用这些函数，保证三处规则一致。
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.schemas.common import ensure_utc
from app.schemas.transaction import Category

# 记账用的本地时区；模型给出不带时区的时间时按它理解。
APP_TZ = timezone(timedelta(hours=8))

# 商户名与数据库 varchar(30) 对齐。
MERCHANT_MAX = 30


def amount_cents(value: object) -> int | None:
    """金额从「元」转「分」；非法或非正数返回 None。"""
    if value is None:
        return None
    try:
        cents = int((Decimal(str(value)) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return None
    return cents if cents > 0 else None


def occurred_at(value: object) -> datetime | None:
    """时间字符串转成带 UTC 时区的 datetime；解析失败返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=APP_TZ)
    return ensure_utc(parsed)


def category(value: object) -> Category | None:
    """类别必须落在枚举内，其余一律视为未识别。"""
    if not isinstance(value, str):
        return None
    try:
        return Category(value.strip())
    except ValueError:
        return None


def merchant(value: object) -> str | None:
    """商户名去空格并截断到列宽；空串返回 None。"""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:MERCHANT_MAX] if cleaned else None


def confidence(value: object) -> float:
    """置信度收敛到 0~1；非法时给 0.5。"""
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return 0.5
    return min(max(parsed, 0.0), 1.0)


def date_range(
    start: object,
    end: object,
) -> tuple[datetime | None, datetime | None]:
    """解析问句里的起止时间，返回带 UTC 时区的闭区间。"""
    return occurred_at(start), occurred_at(end)
