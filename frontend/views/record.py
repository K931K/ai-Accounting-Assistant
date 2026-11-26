"""页面：一句话记账 / 小票识别，含草稿预览与保存。"""

from __future__ import annotations

from datetime import date, datetime

import streamlit as st

from config import CATEGORIES, DEFAULT_CATEGORY, TZ
from api_client import ApiError, api_post
from formatting import to_cents, to_local, to_yuan
from components import empty_state, flash


FORM_DEFAULTS = {
    "form_money": 0.0,
    "form_merchant": "",
    "form_category": DEFAULT_CATEGORY,
    "form_date": date.today(),
    "form_note": "",
}


def reset_form(apply_draft: bool = False) -> None:
    """把预览表单的字段恢复成默认值。

    必须在表单控件创建之前调用；控件已经渲染后再改 session_state 会抛
    StreamlitWidgetAlreadyInstantiatedError。
    """
    for key, value in FORM_DEFAULTS.items():
        st.session_state[key] = value
    if not apply_draft:
        st.session_state["draft_datetime"] = None


def queue_form_reset() -> None:
    """标记「下次渲染前重置表单」，供保存成功后使用。"""
    st.session_state["pending_form_reset"] = True


def apply_draft(draft: dict) -> None:
    """把后端返回的草稿转换成预览表单的初始值。

    后端草稿里的字段可能为 null（未识别），这里统一兜底：
    金额 0、商户空、类别「其他」、日期取草稿时间或今天。
    """
    occurred = to_local(draft.get("occurred_at"))
    st.session_state["draft"] = draft
    st.session_state["draft_datetime"] = occurred
    st.session_state["form_money"] = to_yuan(draft.get("amount_cents"))
    st.session_state["form_merchant"] = draft.get("merchant") or ""
    # 备注上限 200 字：一句话记账的原文就是一句备注，可以直接预填；
    # 小票 OCR 原文可能上千字，超过上限就不塞进备注，改在预览区以原文形式展示。
    raw_text = (draft.get("raw_text") or "").strip()
    st.session_state["form_note"] = raw_text if len(raw_text) <= 200 else ""
    st.session_state["form_date"] = occurred.date() if occurred else date.today()
    category = draft.get("category")
    st.session_state["form_category"] = (
        category if category in CATEGORIES else DEFAULT_CATEGORY
    )


def handle_parse_text(text: str) -> None:
    """调用 /transactions/parse 解析一句话描述。"""
    if not text.strip():
        st.warning("请先输入消费描述，例如「午饭 25」。")
        return
    try:
        draft = api_post("/transactions/parse", json={"text": text.strip()})
    except ApiError as exc:
        st.error(f"文字识别失败：{exc.message}")
        return
    apply_draft(draft)


def handle_parse_receipt(image) -> None:
    """调用 /receipts/parse 识别小票或支付截图。"""
    if image is None:
        st.warning("请先选择一张小票或支付截图。")
        return
    mime = image.type or "image/png"
    try:
        draft = api_post(
            "/receipts/parse",
            files={"file": (image.name, image.getvalue(), mime)},
        )
    except ApiError as exc:
        st.error(f"小票识别失败：{exc.message}")
        return
    apply_draft(draft)


def render_draft_preview(draft: dict) -> None:
    """渲染「解析结果 → 用户确认」的表单，保存时写入后端。

    草稿里未识别的字段在标签上标注，提示用户补充。
    """
    missing = set(draft.get("missing_fields") or [])
    engine = draft.get("engine")
    st.divider()
    st.subheader("📝 解析结果（可修改后保存）")

    if engine == "none":
        st.warning("未启用视觉模型，这次没识别出内容，请手动填写下面的字段。")
    elif engine == "rule":
        st.caption("当前由规则解析（未配置大模型），识别结果可能有偏差，请核对。")

    # 原文超过备注上限时单独展开显示，保证小票识别出的文字不会丢。
    raw_text = (draft.get("raw_text") or "").strip()
    if raw_text and len(raw_text) > 200:
        with st.expander("识别原文"):
            st.text(raw_text)

    with st.form("save_transaction_form"):
        col_a, col_b = st.columns(2)
        with col_a:
            st.number_input(
                "金额（元）" + ("　⚠️ 未识别" if "amount_cents" in missing else ""),
                min_value=0.0,
                step=0.5,
                format="%.2f",
                key="form_money",
            )
            st.text_input(
                "商户（必填）" + ("　⚠️ 未识别" if "merchant" in missing else ""),
                key="form_merchant",
                max_chars=30,
            )
        with col_b:
            st.selectbox(
                "消费类别" + ("　⚠️ 未识别" if "category" in missing else ""),
                CATEGORIES,
                key="form_category",
            )
            st.date_input(
                "消费日期" + ("　⚠️ 未识别" if "occurred_at" in missing else ""),
                key="form_date",
            )
        st.text_input("备注", key="form_note", max_chars=200)
        submitted = st.form_submit_button("💾 保存账单", type="primary")

    if not submitted:
        return

    amount = st.session_state["form_money"]
    if amount <= 0:
        st.error("金额必须大于 0。")
        return

    # 商户是后端必填字段；识别不出来时用类别名兜底，避免卡住保存
    category = st.session_state["form_category"]
    merchant = st.session_state["form_merchant"].strip() or category

    # 时间：优先保留解析出的时刻，日期用用户确认后的值，统一按东八区提交
    base = st.session_state.get("draft_datetime") or datetime.now(TZ)
    occurred = datetime.combine(
        st.session_state["form_date"], base.replace(tzinfo=None).time()
    ).replace(tzinfo=TZ)

    payload = {
        "draft_id": draft.get("draft_id"),
        "amount_cents": to_cents(amount),
        "merchant": merchant,
        "occurred_at": occurred.isoformat(),
        "category": category,
        "note": st.session_state["form_note"].strip() or None,
    }
    try:
        api_post("/transactions", json=payload)
    except ApiError as exc:
        st.error(f"保存失败：{exc.message}")
        return

    st.session_state.pop("draft", None)
    queue_form_reset()
    flash("保存成功，已写入数据库。")
    st.rerun()


def render_record_page() -> None:
    """页面 1：一句话记账 / 小票识别。"""
    # 保存成功后的下一次渲染：先把表单字段复位，再创建控件
    if st.session_state.pop("pending_form_reset", False):
        reset_form()

    st.header("📝 AI 智能记账")
    st.caption("输入一句消费描述，或上传小票、支付截图；识别结果确认后再入库。")

    col_text, col_image = st.columns([3, 2])
    with col_text:
        text = st.text_input(
            "消费描述", placeholder="午饭 25，和同事 AA", key="record_text"
        )
        if st.button("🔍 识别文字", type="primary", key="parse_text_button"):
            handle_parse_text(text)
    with col_image:
        image = st.file_uploader(
            "小票 / 截图", type=["png", "jpg", "jpeg"], key="receipt_uploader"
        )
        if st.button("📷 识别小票", key="parse_receipt_button"):
            handle_parse_receipt(image)

    draft = st.session_state.get("draft")
    if draft:
        render_draft_preview(draft)
    else:
        st.divider()
        empty_state("🧾", "还没有解析结果", "输入一句消费描述或上传小票，识别后会显示在这里")
