"""规则兜底解析：大模型不可用时用关键词和正则得到同样的字段。

输出与模型抽取保持一致：金额单位「分」、类别用 Category 枚举值、时间带时区。
函数只做纯文本判断，不访问网络，也不读写数据库。
"""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.schemas.transaction import Category

# 关键词 → 类别。按插入顺序匹配，命中即停，所以具体词要排在宽泛词前面：
# 「药」「医院」必须早于「买」，否则「买药」会被判成购物。
CATEGORY_KEYWORDS = {
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

# 记账用的本地时区，与前端提交的 +08:00 保持一致。
_APP_TZ = timezone(timedelta(hours=8))

# 匹配第一个金额数字，允许一两位小数；不区分「元 / 块」等单位。
_AMOUNT_PATTERN = re.compile(r"(\d+(?:\.\d{1,2})?)")

# 「最近 7 天」这类相对区间。
_RECENT_DAYS = re.compile(r"最近\s*(\d+)\s*天")


def amount_cents_from_text(text: str) -> int | None:
    """从文本里取第一个金额，元转分；取不到返回 None。"""
    match = _AMOUNT_PATTERN.search(text)
    if not match:
        return None
    return int((Decimal(match.group(1)) * 100).to_integral_value())


def category_from_text(text: str) -> Category | None:
    """按关键词表匹配类别，命中第一个就返回，未命中返回 None。"""
    for keyword, category in CATEGORY_KEYWORDS.items():
        if keyword in text:
            return category
    return None


def parse_text(text: str) -> dict:
    """规则抽取一句话记账，返回与模型抽取同名的字段。

    规则不识别商户和消费时间，这两项固定为 None，由用户在预览页补全。
    """
    return {
        "amount_cents": amount_cents_from_text(text),
        "merchant": None,
        "occurred_at": None,
        "category": category_from_text(text),
        # 规则匹配的固定置信度，不是模型概率。
        "confidence": 0.4,
    }


def intent_from_text(text: str) -> str:
    """判断问句意图：top=哪类最多，count=笔数，list=明细，其余按 total 处理。"""
    if any(word in text for word in ("最多", "哪一类", "哪个类别", "最贵")):
        return "top"
    if any(word in text for word in ("几笔", "多少笔", "次数", "几单")):
        return "count"
    if any(word in text for word in ("列表", "明细", "最近", "几条")):
        return "list"
    return "total"


def date_range_from_text(text: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    """按问句里的时间词给出闭区间起止时间。

    参数:
        text: 用户问句。
        now: 当前时间，带时区；用它推算「这个月」「上个月」等相对区间。

    返回:
        (起, 止)，都是带时区的 datetime；没有时间词时返回 (None, None) 表示不限时间。
    """
    local_now = now.astimezone(_APP_TZ)

    if "今天" in text:
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1) - timedelta(microseconds=1)
    if "昨天" in text:
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        return start, start + timedelta(days=1) - timedelta(microseconds=1)
    if "上个月" in text:
        first_this_month = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = (first_this_month - timedelta(days=1)).replace(day=1)
        return start, first_this_month - timedelta(microseconds=1)
    if "这个月" in text or "本月" in text:
        start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (start + timedelta(days=32)).replace(day=1)
        return start, next_month - timedelta(microseconds=1)
    if "今年" in text:
        start = local_now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, local_now.replace(
            month=12, day=31, hour=23, minute=59, second=59, microsecond=999999
        )
    recent = _RECENT_DAYS.search(text)
    if recent:
        start = local_now - timedelta(days=int(recent.group(1)))
        return start, local_now
    return None, None


def parse_question(text: str, now: datetime) -> dict:
    """规则解析自然语言查账，返回与模型输出同名的字段。"""
    date_from, date_to = date_range_from_text(text, now)
    return {
        "category": category_from_text(text),
        "date_from": date_from,
        "date_to": date_to,
        "intent": intent_from_text(text),
        "confidence": 0.35,
    }
