"""页面：自然语言查账，展示结论、指标与明细。"""

from __future__ import annotations

import streamlit as st

from api_client import ApiError, api_post
from formatting import fmt_money, to_local, transactions_frame
from components import empty_state


QUICK_QUESTIONS = [
    "这个月吃饭花了多少钱",
    "这个月哪类消费最多",
    "最近有什么账单",
]


def run_query(question: str) -> None:
    """调用 /query/ask：后端解析问题条件并直接返回查询结果。"""
    if not question.strip():
        st.warning("请先输入问题。")
        return
    try:
        answer = api_post("/query/ask", json={"question": question.strip()})
    except ApiError as exc:
        st.error(f"查询失败：{exc.message}")
        return
    st.session_state["query_result"] = answer


def _fill_question(question: str) -> None:
    """快捷问题按钮的回调。

    on_click 回调在脚本重新执行、控件创建之前运行，此时写 session_state 才合法；
    直接在按钮的 if 分支里写会触发 StreamlitWidgetAlreadyInstantiatedError。
    """
    st.session_state["query_text"] = question
    st.session_state["query_pending"] = question


def render_query_page() -> None:
    """页面 2：自然语言查账。"""
    st.header("🔍 自然语言查账")
    st.caption("用一句话提问，系统解析类别与时间范围后直接查库，而不是让模型自由发挥。")

    st.text_input(
        "你的问题",
        placeholder="我上个月吃饭花了多少？",
        key="query_text",
    )
    cols = st.columns(len(QUICK_QUESTIONS))
    for index, question in enumerate(QUICK_QUESTIONS):
        cols[index].button(
            question,
            key=f"quick_question_{index}",
            on_click=_fill_question,
            args=(question,),
        )

    # 快捷问题点击后待执行的查询
    pending = st.session_state.pop("query_pending", None)
    if pending:
        run_query(pending)

    if st.button("🔎 查询", type="primary", key="run_query_button"):
        run_query(st.session_state.get("query_text", ""))

    answer = st.session_state.get("query_result")
    st.divider()
    if not answer:
        empty_state("💡", "还没有查询结果", "输入问题或点上面的示例，结果会显示在这里")
        return

    st.subheader("结论")
    st.markdown(f"### {answer.get('answer', '-')}")

    col_1, col_2, col_3 = st.columns(3)
    col_1.metric("合计金额", fmt_money(answer.get("total_cents")))
    col_2.metric("命中笔数", f"{answer.get('count', 0)} 笔")
    col_3.metric("类别", answer.get("category") or "全部")

    # 时间范围：两边都有就显示区间，只有一边就显示单日
    period = "不限"
    start_dt = to_local(answer.get("date_from")) if answer.get("date_from") else None
    end_dt = to_local(answer.get("date_to")) if answer.get("date_to") else None
    if start_dt and end_dt:
        period = f"{start_dt:%Y-%m-%d} ~ {end_dt:%Y-%m-%d}"
    elif start_dt or end_dt:
        period = f"{(start_dt or end_dt):%Y-%m-%d}"

    engine = answer.get("engine")
    engine_text = "大模型" if engine == "llm" else "规则兜底（未配置大模型时）"
    st.caption(f"时间范围：{period}　·　解析方式：{engine_text}")

    items = answer.get("items") or []
    if items:
        st.subheader("明细")
        st.dataframe(transactions_frame(items), width="stretch", hide_index=True)
