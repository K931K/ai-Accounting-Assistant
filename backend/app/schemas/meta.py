"""基础数据和健康检查的响应模型。

- CategoryOption 对应 GET /categories 的列表项；
- HealthResponse 对应 GET /health 的响应。
"""

from pydantic import BaseModel, Field

from app.schemas.transaction import Category


class CategoryOption(BaseModel):
    """GET /categories 返回的一项。

    value 是 Category 枚举值，label 是展示文案；
    当前两者都取中文类别名，前端下拉框直接使用。
    """

    value: Category = Field(..., description="类别取值")
    label: str = Field(..., description="展示文案")


class HealthResponse(BaseModel):
    """GET /health 的响应。

    status 固定为 ok；version 来自 FastAPI 应用的 version 字段。
    """

    status: str = Field(..., description="固定为 ok")
    version: str = Field(..., description="应用版本号")


class AiStatusResponse(BaseModel):
    """GET /ai/status 的响应，用于前端和排障时确认模型是否已接上。"""

    configured: bool = Field(..., description="是否配置了可用的模型密钥")
    provider: str = Field(..., description="openai=兼容接口，dify=工作流")
    model: str = Field(..., description="文本模型名")
    vision_model: str = Field(..., description="视觉模型名；空串表示未启用图片识别")
    vision_configured: bool = Field(..., description="图片识别是否可用（模型名与密钥都齐全）")
    missing_config: list[str] = Field(
        default_factory=list,
        description="当前 provider 缺失的环境变量名；空列表表示配置齐全，AI 链路会真的走模型",
    )
    fallback: str = Field(..., description="不可用时的兜底方式")
