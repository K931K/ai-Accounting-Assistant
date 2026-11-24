"""智能助手接口的请求/响应模型。

- ChatRequest：一条用户消息，session_id 为空表示新会话；
- ChatResponse：回复文本、生成方式与会话标识；
- SessionHistory：会话历史，前端刷新后据此恢复多轮记录。
"""

from datetime import datetime

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """POST /agent/chat 的请求体。"""

    session_id: str | None = Field(
        None,
        max_length=32,
        description="会话 id，由上一轮响应带回；不传表示新会话",
    )
    message: str = Field(..., min_length=1, max_length=2000, description="用户消息")


class ChatResponse(BaseModel):
    """POST /agent/chat 的响应。"""

    session_id: str = Field(..., description="会话 id，下一轮原样带回")
    conversation_id: str | None = Field(
        None, description="Dify 会话 id；兼容接口多轮与本地兜底时为空"
    )
    reply: str = Field(..., description="助手回复")
    engine: str = Field(
        ..., description="dify=Dify 对话应用，llm=兼容接口按历史生成，local=本地兜底"
    )


class ChatMessage(BaseModel):
    """会话历史中的一条消息。"""

    role: str = Field(..., description="user / assistant")
    content: str = Field(..., description="消息内容")
    created_at: datetime = Field(..., description="入库时间，UTC")


class SessionHistory(BaseModel):
    """GET /agent/sessions/{id} 的响应。"""

    session_id: str = Field(..., description="会话 id")
    conversation_id: str | None = Field(None, description="Dify 会话 id")
    messages: list[ChatMessage] = Field(..., description="按时间正序的消息列表")
