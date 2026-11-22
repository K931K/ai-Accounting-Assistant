"""FastAPI 应用入口。
启动顺序：
1. 导入本模块时创建 app，注册 OpenAPI 元数据；
2. FastAPI 启动 lifespan，调用 init_db() 创建缺失的表；
3. 注册 CORS、统一异常处理器；
4. 挂载 transactions / receipts / stats / budgets 四个路由；
5. 直接定义 /health 和 /categories 两个基础接口。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.ai import llm
from app.api import agent, budgets, query, receipts, stats, transactions
from app.core.errors import register_error_handlers
from app.db import init_db
from app.schemas.meta import AiStatusResponse, CategoryOption, HealthResponse
from app.schemas.transaction import Category


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """应用启动时建库建表。

    init_db() 来自 app.db，用 CREATE TABLE IF NOT EXISTS 建缺失的表，
    并给老库补上新增的列和索引；已有表结构和数据都不会被改动。
    """
    init_db()
    yield


app = FastAPI(
    title="记账助手 API",
    version="0.1.0",
    description=(
        "个人记账与消费分析接口。\n\n"
        "## 接口约定\n"
        "- 字段命名：snake_case\n"
        "- 金额：整数，单位「分」，字段名以 `_cents` 结尾\n"
        "- 时间：入参为 ISO 8601 带时区，接口返回 UTC\n"
        "- 错误：`{\"code\": \"...\", \"message\": \"...\", \"detail\": {}}`\n"
    ),
    # 绑定生命周期
    lifespan=lifespan,
    # 控制 /docs 和导入 Apifox 后的接口分组顺序。
    openapi_tags=[
        {"name": "记账", "description": "解析、保存与查询账目"},
        {"name": "查账", "description": "自然语言提问，返回查询条件或直接给结果"},
        {"name": "小票识别", "description": "上传小票或支付截图，生成待确认的草稿"},
        {"name": "智能助手", "description": "多轮对话：由 Dify 对话应用维护上下文，本地兜底单轮解析"},
        {"name": "统计", "description": "按类别汇总支出"},
        {"name": "预算", "description": "按月设置预算并判断是否接近或超出上限"},
        {"name": "元数据", "description": "类别下拉框等基础数据"},
        {"name": "运维", "description": "健康检查"},
    ],
)

# 浏览器跨域，先写着备用，streamlit不用这个
# 前端不用看这个，除非要换React
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8501",
        "http://127.0.0.1:8501",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 统一异常处理后，挂载业务路由
register_error_handlers(app)
app.include_router(transactions.router)
app.include_router(query.router)
app.include_router(receipts.router)
app.include_router(stats.router)
app.include_router(budgets.router)
app.include_router(agent.router)


#健康状态检查，后面docker正式部署后再改进，先设定返回版本号
@app.get("/health", tags=["运维"], summary="健康检查", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=app.version)


@app.get(
    "/ai/status",
    tags=["运维"],
    summary="大模型接入状态",
    response_model=AiStatusResponse,
)
def ai_status() -> AiStatusResponse:
    """返回当前模型配置，供前端显示「AI 已接入 / 规则模式」。"""
    return AiStatusResponse(
        configured=llm.configured(),
        provider=llm.provider(),
        model=llm.model_name(),
        vision_model=llm.vision_model_name(),
        vision_configured=llm.vision_configured(),
        fallback="规则解析（关键词 + 正则）",
    )


"""
categories 统一的字典接口
返回 Category 枚举的全部取值，供前端使用
数据库和解析结果使用同一套 Category 枚举，避免前端硬编码
"""
@app.get(
    "/categories",
    tags=["元数据"],
    summary="列出所有消费类别",
    response_model=list[CategoryOption],
)
def list_categories() -> list[CategoryOption]:
    return [CategoryOption(value=category, label=category.value) for category in Category]
