"""大模型调用入口：Dify 优先，OpenAI 兼容接口作为备选。

对外只暴露 extract_json()：给任务名、系统提示和用户输入，返回模型输出的 dict。
字段校验和兜底在调用方（text_extract / receipt_ocr / query_extract）处理。

配置全在环境变量里，见 backend/.env.example：
- AI_PROVIDER=dify 时走 Dify：三条抽取任务各对应一个工作流（DIFY_WORKFLOW_*），
  图片先上传再作为 file 变量传入；多轮对话走 /v1/chat-messages（见 app.ai.dify）；
- AI_PROVIDER=openai 时走任意 OpenAI 兼容服务的 /chat/completions；
- 文本模型与视觉模型可以分开配置：AI_VISION_BASE_URL / AI_VISION_API_KEY
  留空时复用文本模型那一组。
未配置密钥时 configured() 为 False，调用方直接走规则兜底。
"""

import base64
import json
import logging
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

from app.ai import dify, transport
from app.ai.transport import AiUnavailable

logger = logging.getLogger(__name__)

# 先读 backend/.env 再取模块级配置：应用启动时本模块可能早于 app.db 被导入，
# 不能让配置落在 dotenv 加载之前。
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# 仅用于 /ai/status 展示；实际请求每次从 transport 读环境变量。
AI_TIMEOUT = transport.timeout()
AI_RETRIES = transport.retries()

# 三种任务共用一套配置，Dify 侧按任务选工作流。
TASK_TEXT = "text_extract"
TASK_RECEIPT = "receipt_ocr"
TASK_QUERY = "query_parse"

__all__ = [
    "AiUnavailable",
    "TASK_QUERY",
    "TASK_RECEIPT",
    "TASK_TEXT",
    "chat_configured",
    "configured",
    "extract_json",
    "model_name",
    "provider",
    "vision_configured",
    "vision_model_name",
]


def provider() -> str:
    """当前 provider，取值 dify 或 openai。"""
    return os.getenv("AI_PROVIDER", "dify").strip().lower()


def _api_key() -> str:
    """OpenAI 兼容接口的文本模型密钥。"""
    return os.getenv("AI_API_KEY", "").strip()


def _vision_api_key() -> str:
    """视觉模型的密钥；没单独配置时复用文本模型密钥。"""
    return os.getenv("AI_VISION_API_KEY", "").strip() or _api_key()


def configured() -> bool:
    """是否配置了可用的文本模型服务。"""
    if provider() == "dify":
        return dify.configured()
    return bool(_api_key())


def vision_configured() -> bool:
    """图片识别是否可用。

    Dify 由工作流内部选模型，只要工作流可用即可；OpenAI 兼容方式需要视觉模型名与密钥。
    """
    if provider() == "dify":
        return dify.configured()
    return bool(vision_model_name()) and bool(_vision_api_key())


def chat_configured() -> bool:
    """多轮对话是否可用；目前只有 Dify 对话型应用支持。"""
    return provider() == "dify" and dify.chat_configured()


def model_name() -> str:
    """当前文本模型名，用于 /ai/status 展示。"""
    if provider() == "dify":
        return dify.workflow_name(TASK_TEXT)
    return os.getenv("AI_MODEL", "deepseek-flash")


def vision_model_name() -> str:
    """视觉模型名；Dify 方式返回工作流名，空串表示未启用图片识别。"""
    if provider() == "dify":
        return dify.workflow_name(TASK_RECEIPT) if dify.configured() else ""
    return os.getenv("AI_VISION_MODEL", "").strip()


def _base_url() -> str:
    """OpenAI 兼容服务的根地址，去掉结尾斜杠。"""
    return os.getenv("AI_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")


def _vision_base_url() -> str:
    """视觉模型服务的根地址；没单独配置时复用文本模型地址。"""
    return (os.getenv("AI_VISION_BASE_URL", "").strip() or _base_url()).rstrip("/")


def _strip_code_fence(text: str) -> str:
    """去掉模型偶尔包在外面的 ```json 代码块标记。"""
    match = re.search(r"\{.*\}", text, re.S)
    return match.group(0) if match else text


def _parse_json(text: str) -> dict:
    """把模型返回的文本解析成 dict；解析失败视为模型不可用。"""
    try:
        data = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError as exc:
        raise AiUnavailable(f"模型返回不是合法 JSON：{text[:120]}") from exc
    if not isinstance(data, dict):
        raise AiUnavailable("模型返回不是 JSON 对象")
    return data


def _to_data_url(image: bytes, mime: str) -> str:
    """图片转成 data URL，供视觉模型的 image_url 字段使用。"""
    encoded = base64.b64encode(image).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _call_openai(system: str, user: str, model: str, image: bytes, mime: str) -> str:
    """调用 OpenAI 兼容的 /chat/completions，返回模型输出的文本。

    image 非空时把图片作为 image_url 一起发送，用于小票识别；
    此时使用视觉模型的地址与密钥（未单独配置则复用文本模型）。
    """
    if image:
        base_url = _vision_base_url()
        api_key = _vision_api_key()
        content: object = [
            {"type": "text", "text": user},
            {"type": "image_url", "image_url": {"url": _to_data_url(image, mime)}},
        ]
    else:
        base_url = _base_url()
        api_key = _api_key()
        content = user

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "temperature": 0.2,
        "max_tokens": 800,
        # 兼容服务大多支持 JSON 模式；个别网关不认这个字段，下面会降级重试。
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    url = f"{base_url}/chat/completions"

    try:
        resp = transport.post_json(url, payload, headers)
    except transport.BadRequest as exc:
        if "response_format" not in exc.body:
            raise AiUnavailable(f"模型服务拒绝请求：{exc.body[:200]}") from exc
        # 网关不支持 response_format：去掉该字段重试一次，提示词里已经要求 JSON。
        logger.warning("网关不支持 response_format，去掉后重试：%s", exc.body[:120])
        payload.pop("response_format", None)
        try:
            resp = transport.post_json(url, payload, headers)
        except transport.BadRequest as retry_exc:
            raise AiUnavailable(f"模型服务拒绝请求：{retry_exc.body[:200]}") from retry_exc

    try:
        return resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AiUnavailable("模型响应缺少 choices 内容") from exc


def extract_json(
    task: str,
    system: str,
    user: str,
    *,
    image: bytes = b"",
    mime: str = "image/png",
) -> dict:
    """调用模型并返回解析后的 JSON 对象。

    参数:
        task: TASK_TEXT / TASK_RECEIPT / TASK_QUERY，决定 Dify 用哪个工作流。
        system: 系统提示，约束模型输出字段与取值。
        user: 用户输入文本；图片识别时作为附带的文字说明。
        image / mime: 非空时按视觉方式处理。

    返回:
        dict：模型输出的 JSON 对象，字段由调用方校验。

    异常:
        AiUnavailable：未配置、请求失败或返回无法解析，调用方应走规则兜底。
    """
    if not configured():
        raise AiUnavailable("未配置模型服务")

    started = time.perf_counter()
    if provider() == "dify":
        model = dify.workflow_name(task)
        raw = dify.run_workflow(task, system, user, image=image, mime=mime)
    else:
        model = vision_model_name() if image else model_name()
        if image and not model:
            raise AiUnavailable("未配置视觉模型 AI_VISION_MODEL")
        raw = _call_openai(system, user, model, image, mime)

    result = _parse_json(raw)
    logger.info(
        "模型调用完成 provider=%s task=%s model=%s 耗时=%.2fs",
        provider(),
        task,
        model,
        time.perf_counter() - started,
    )
    return result
