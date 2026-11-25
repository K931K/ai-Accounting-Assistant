"""金额、时间与表格的格式化工具，以及 CSV 导出。"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pandas as pd

from config import TZ


def to_cents(yuan: float) -> int:
    """元 → 分，四舍五入成整数。"""
    return int(round(yuan * 100))


def to_yuan(cents: int | None) -> float:
    """分 → 元。"""
    return (cents or 0) / 100


def fmt_money(cents: int | None) -> str:
    """格式化金额，带千分位。"""
    return f"¥{to_yuan(cents):,.2f}"


def parse_iso(value: str | None) -> datetime | None:
    """解析后端返回的 ISO 时间；失败返回 None。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_local(value: str | None) -> datetime | None:
    """UTC 时间字符串 → 东八区 datetime。"""
    parsed = parse_iso(value)
    return parsed.astimezone(TZ) if parsed else None


def fmt_local(value: str | None, pattern: str = "%Y-%m-%d") -> str:
    """UTC 时间字符串 → 本地日期文本，缺失时显示占位符。"""
    local = to_local(value)
    return local.strftime(pattern) if local else "-"


def iso_at(day: date, at: time = time(0, 0)) -> str:
    """把「日期 + 时刻」转成带时区的 ISO 字符串，供接口的 from / to 使用。"""
    return datetime.combine(day, at).replace(tzinfo=TZ).isoformat()


def month_options(count: int = 12) -> list[str]:
    """生成最近 count 个月的 YYYY-MM 列表，当月在最前。"""
    today = date.today()
    total = today.year * 12 + (today.month - 1)
    return [f"{(total - i) // 12:04d}-{(total - i) % 12 + 1:02d}" for i in range(count)]


def transactions_frame(items: list[dict]) -> pd.DataFrame:
    """把后端账目列表转成展示用的表格。"""
    if not items:
        return pd.DataFrame(columns=["日期", "商户", "类别", "金额", "备注"])
    return pd.DataFrame(
        [
            {
                "日期": fmt_local(item.get("occurred_at"), "%Y-%m-%d %H:%M"),
                "商户": item.get("merchant") or "-",
                "类别": item.get("category") or "-",
                "金额": fmt_money(item.get("amount_cents")),
                "备注": item.get("note") or "",
            }
            for item in items
        ]
    )


def export_csv(records: list[dict]) -> bytes:
    """把明细导出成 CSV（带 BOM，Excel 直接打开不乱码）。"""
    frame = pd.DataFrame(
        [
            {
                "日期": fmt_local(row.get("occurred_at"), "%Y-%m-%d %H:%M"),
                "商户": row.get("merchant") or "",
                "类别": row.get("category") or "",
                "金额(元)": to_yuan(row.get("amount_cents")),
                "备注": row.get("note") or "",
            }
            for row in records
        ]
    )
    return frame.to_csv(index=False).encode("utf-8-sig")
