"""页面：按月设置总预算 / 分类预算，展示使用进度与超支提醒。"""

from __future__ import annotations

import streamlit as st

from config import CATEGORIES
from api_client import ApiError, api_delete, api_get, api_put
from formatting import fmt_money, month_options, to_cents
from components import budget_bar, empty_state, flash


def render_budget_row(item: dict) -> None:
    """单条预算：类别、已用 / 预算、进度条、状态与删除按钮。"""
    name = item.get("category") or "整月总预算"
    ratio = float(item.get("ratio") or 0)
    status = item.get("status")
    spent, amount = item.get("spent_cents", 0), item.get("amount_cents", 0)
    remain = amount - spent

    with st.container(border=True):
        head, action = st.columns([5, 1])
        with head:
            st.markdown(f"**{name}**　{fmt_money(spent)} / {fmt_money(amount)}")
        with action:
            if st.button("删除", key=f"budget_delete_{item['id']}"):
                st.session_state["pending_budget_id"] = item["id"]

        budget_bar(ratio, status)

        if status == "exceeded":
            st.error(f"已超支 {fmt_money(-remain)}（{ratio:.0%}）")
        elif status == "warning":
            st.warning(f"接近上限，已用 {ratio:.0%}，剩余 {fmt_money(remain)}")
        else:
            st.caption(f"已用 {ratio:.0%}，剩余 {fmt_money(remain)}")

        if st.session_state.get("pending_budget_id") == item["id"]:
            st.warning("确认删除这条预算？")
            yes, no = st.columns(2)
            if yes.button("确认删除", type="primary", key=f"budget_confirm_{item['id']}"):
                try:
                    api_delete(f"/budgets/{item['id']}")
                except ApiError as exc:
                    st.error(f"删除失败：{exc.message}")
                else:
                    st.session_state.pop("pending_budget_id", None)
                    flash("预算已删除。")
                    st.rerun()
            if no.button("取消", key=f"budget_cancel_{item['id']}"):
                st.session_state.pop("pending_budget_id", None)
                st.rerun()


def render_budget_page() -> None:
    """页面 4：预算设置与超支提醒。"""
    st.header("🎯 预算管理")
    st.caption("按月份设置总额或分类预算，接近上限和超支时给出提醒。")

    month = st.selectbox("月份", month_options(), key="budget_month")

    try:
        overview = api_get("/budgets", params={"month": month})
    except ApiError as exc:
        st.error(f"读取预算失败：{exc.message}")
        return

    items = overview.get("items") or []
    spent = overview.get("spent_cents", 0)
    total_budget = next(
        (item["amount_cents"] for item in items if item.get("category") is None), None
    )

    col_1, col_2, col_3 = st.columns(3)
    col_1.metric("本月支出", fmt_money(spent))
    col_2.metric("本月总预算", fmt_money(total_budget) if total_budget else "未设置")
    if total_budget:
        remain = total_budget - spent
        col_3.metric("剩余" if remain >= 0 else "已超支", fmt_money(abs(remain)))
    else:
        col_3.metric("剩余", "-")

    st.divider()
    st.subheader("预算使用情况")
    if items:
        for item in items:
            render_budget_row(item)
    else:
        empty_state("🎯", "这个月还没设预算", "在下面添加一条总额或分类预算")

    st.divider()
    st.subheader("设置预算")
    st.caption("同一月份、同一类别重复设置会覆盖原金额；「整月总预算」不分类别。")
    with st.form("budget_form"):
        col_cat, col_amount = st.columns(2)
        with col_cat:
            target = st.selectbox("预算类别", ["整月总预算"] + CATEGORIES)
        with col_amount:
            amount = st.number_input(
                "预算金额（元）", min_value=1.0, step=100.0, value=1000.0, format="%.2f"
            )
        saved = st.form_submit_button("保存预算", type="primary")

    if saved:
        payload = {
            "month": month,
            "category": None if target == "整月总预算" else target,
            "amount_cents": to_cents(amount),
        }
        try:
            api_put("/budgets", json=payload)
        except ApiError as exc:
            st.error(f"保存失败：{exc.message}")
        else:
            flash(f"{month} 的「{target}」已保存。")
            st.rerun()
