"""AI 记账助手前端入口：页面配置、样式注入、侧边栏导航与页面分发。"""

from __future__ import annotations

import requests
import streamlit as st

from config import API_URL, APP_VERSION
from components import inject_styles, show_flash
from views.record import render_record_page
from views.query import render_query_page
from views.stats import render_stats_page
from views.budget import render_budget_page
from views.assistant import render_assistant_page


PAGES = {
    "record": "📝 一句话记账",
    "query": "🔍 自然语言查账",
    "stats": "📊 账单与统计",
    "budget": "🎯 预算管理",
    "assistant": "💬 智能助手",
}


@st.cache_data(ttl=20, show_spinner=False)
def backend_online() -> bool:
    """探测后端是否在线，结果缓存 20 秒避免每步都请求。"""
    try:
        requests.get(API_URL + "/health", timeout=3)
    except requests.RequestException:
        return False
    return True


def main() -> None:
    """应用入口：侧边栏导航 + 页面分发。"""
    st.set_page_config(page_title="AI 记账助手", page_icon="📒", layout="wide")
    inject_styles()

    # 支持通过 ?page=budget 直接打开某个页面（演示、截图、分享都方便）
    default_key = st.query_params.get("page", "record")
    keys = list(PAGES)
    index = keys.index(default_key) if default_key in keys else 0

    st.sidebar.markdown(
        """
        <div style="display:flex;align-items:center;gap:10px;padding:2px 0 8px;">
          <div style="width:40px;height:40px;border-radius:12px;background:#4F46E5;
                      display:flex;align-items:center;justify-content:center;
                      font-size:21px;box-shadow:0 4px 10px rgba(79,70,229,.25);">📒</div>
          <div>
            <div style="font-size:17px;font-weight:650;color:#1F2430;line-height:1.2;">
              AI 记账助手
            </div>
            <div style="font-size:12px;color:#7A8299;margin-top:2px;">
              个人消费记录与分析
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    selected_label = st.sidebar.radio(
        "功能菜单", list(PAGES.values()), index=index, label_visibility="collapsed"
    )
    selected_key = keys[list(PAGES.values()).index(selected_label)]

    st.sidebar.divider()
    if backend_online():
        st.sidebar.success("后端已连接")
    else:
        st.sidebar.error("后端未连接")
        st.sidebar.caption(f"请确认 {API_URL} 正在运行")
    st.sidebar.caption(f"{APP_VERSION} · 数据保存在本地 MySQL")

    show_flash()
    {
        "record": render_record_page,
        "query": render_query_page,
        "stats": render_stats_page,
        "budget": render_budget_page,
        "assistant": render_assistant_page,
    }[selected_key]()

    st.divider()
    st.caption(
        "AI 记账助手 · 课程演示项目；账目只保存在本机数据库，页面内容不构成理财建议。"
    )

if __name__ == "__main__":
    main()
