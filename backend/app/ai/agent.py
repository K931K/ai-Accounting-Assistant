"""智能助手：多轮对话的编排与会话持久化。

- Dify 对话型应用配置好时，消息转给 Dify，由它维护上下文与工具调用；
- 没配置或调用失败时，退化为本地单轮解析，仍然写入会话历史，
  这样前端的多轮界面在后端还没接 Dify 时也能用。

记账写入仍然走「草稿 -> 用户确认 -> POST /transactions」，助手不会直接落库。
"""

import logging
from dataclasses import dataclass

from pymysql.connections import Connection

from app import store
from app.ai import dify, llm, text_extract
from app.ai.transport import AiUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentReply:
    """一次对话的结果。

    engine 为 dify 表示由 Dify 对话应用生成，local 表示本地单轮兜底。
    """

    session_id: str
    reply: str
    engine: str
    conversation_id: str | None


def _local_reply(extraction: text_extract.ExpenseExtraction) -> str:
    """把本地单轮解析结果转成一句可读回复。"""
    if extraction.amount_cents:
        category = extraction.category.value if extraction.category else "其他"
        merchant = extraction.merchant or "未识别商户"
        return (
            f"我识别到一笔支出：{extraction.amount_cents / 100:.2f} 元，"
            f"类别「{category}」，商户「{merchant}」。确认后我再帮你记进账本。"
        )
    return "这句里还没看出金额。可以具体一点，例如「午饭 25」；也可以问我「这个月花了多少」。"


def chat(conn: Connection, message: str, session_id: str | None = None) -> AgentReply:
    """处理一条用户消息，返回助手回复并落库。

    参数:
        conn: 请求级连接。
        message: 用户输入，长度由 schema 校验。
        session_id: 前端持有的会话 id；为空时新建会话。

    返回:
        AgentReply：会话 id、回复文本、生成方式与 Dify 会话标识。
    """
    session = store.get_or_create_chat_session(conn, session_id)
    store.add_chat_message(conn, session.session_id, "user", message)

    if llm.chat_configured():
        try:
            result = dify.chat(message, session.conversation_id)
        except AiUnavailable as exc:
            # Dify 临时不可用时退回本地解析，保证对话不中断。
            logger.warning("Dify 对话失败，退回本地解析：%s", exc)
            reply = _local_reply(text_extract.extract(message))
            engine = "local"
            conversation_id = session.conversation_id
        else:
            reply = result["answer"]
            conversation_id = result["conversation_id"] or session.conversation_id
            if conversation_id and conversation_id != session.conversation_id:
                store.set_chat_conversation_id(conn, session.session_id, conversation_id)
            engine = "dify"
    else:
        reply = _local_reply(text_extract.extract(message))
        engine = "local"
        conversation_id = session.conversation_id

    store.add_chat_message(conn, session.session_id, "assistant", reply)
    return AgentReply(
        session_id=session.session_id,
        reply=reply,
        engine=engine,
        conversation_id=conversation_id,
    )
