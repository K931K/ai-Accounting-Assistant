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

import httpx
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

# 各 provider 必需的配置项；未配置时接口直接走规则兜底，这里用于说清缺哪一项。
_REQUIRED_ENV = {
    "dify": ("DIFY_BASE_URL", "DIFY_API_KEY"),
    "openai": ("AI_API_KEY",),
}

__all__ = [
    "AiUnavailable",
    "TASK_QUERY",
    "TASK_RECEIPT",
    "TASK_TEXT",
    "chat",
    "chat_configured",
    "configured",
    "extract_json",
    "missing_config",
    "model_name",
    "provider",
    "vision_configured",
    "vision_model_name",
]


# 单次请求的输出上限。思考型模型先消耗思考 token 再输出正文，
# 800 这种小上限容易被思考占满、正文为空，这里留够余量。
MAX_TOKENS = 2000

# 网关不认识的可选字段：遇到 400 时按这个顺序逐个去掉重试。
_OPTIONAL_FIELDS = ("response_format", "thinking")


def provider() -> str:
    """当前 provider，取值 dify 或 openai。"""
    return os.getenv("AI_PROVIDER", "dify").strip().lower()


def _thinking_disabled() -> bool:
    """AI_THINKING=off 时是否要求模型跳过思考。

    字段抽取和意图解析都是短任务，思考过程只会多花 token 与等待时间；
    该开关发的是 DeepSeek 系接口的 thinking 字段，其他网关若返回 400
    会被 _post_chat() 自动去掉后重试，不影响正常调用。
    """
    return os.getenv("AI_THINKING", "").strip().lower() in {"off", "false", "0", "disabled"}


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


def missing_config() -> list[str]:
    """当前 provider 缺哪些环境变量，用于排障时指名道姓。

    返回:
        缺失的变量名列表；空列表表示配置齐全，AI 链路会真的走模型而不是规则。
        只判断「有没有填」，不校验值是否有效，密钥写错由调用失败时的日志暴露。
    """
    names = _REQUIRED_ENV.get(provider(), _REQUIRED_ENV["openai"])
    return [name for name in names if not os.getenv(name, "").strip()]


def vision_configured() -> bool:
    """图片识别是否可用。

    Dify 由工作流内部选模型，只要工作流可用即可；OpenAI 兼容方式需要视觉模型名与密钥。
    """
    if provider() == "dify":
        return dify.configured()
    return bool(vision_model_name()) and bool(_vision_api_key())


def chat_configured() -> bool:
    """多轮对话是否可用：Dify 对话应用，或已配置密钥的 OpenAI 兼容接口。"""
    if provider() == "dify":
        return dify.chat_configured()
    return bool(_api_key())


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


def _post_chat(url: str, payload: dict, headers: dict[str, str]) -> httpx.Response:
    """发送 /chat/completions 请求，400 时按可选字段逐个去掉后重试。

    不同网关对 response_format、thinking 这类可选字段的接受程度不一样，
    400 基本就是「不认识这个字段」；去掉再试一次，比直接判模型不可用划算。
    """
    while True:
        try:
            return transport.post_json(url, payload, headers)
        except transport.BadRequest as exc:
            dropped = next(
                (name for name in _OPTIONAL_FIELDS if name in payload and name in exc.body),
                None,
            )
            if dropped is None:
                raise AiUnavailable(f"模型服务拒绝请求：{exc.body[:200]}") from exc
            logger.warning("网关不支持 %s，去掉后重试：%s", dropped, exc.body[:120])
            payload.pop(dropped, None)


def _response_text(resp: httpx.Response) -> str:
    """取 /chat/completions 响应里的正文，空正文视为模型不可用。

    思考型模型先输出 reasoning_content 再给 content，输出上限被思考占满时
    content 是空串；这种时候不能把思考过程当结果用，只能判定本次调用失败。
    """
    try:
        choice = resp.json()["choices"][0]
        content = (choice.get("message") or {}).get("content")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AiUnavailable("模型响应缺少 choices 内容") from exc
    if not isinstance(content, str) or not content.strip():
        raise AiUnavailable(
            "模型没有返回正文（finish_reason="
            f"{choice.get('finish_reason')}），可调大 max_tokens 或设置 AI_THINKING=off"
        )
    return content


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

    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "temperature": 0.2,
        "max_tokens": MAX_TOKENS,
        # 兼容服务大多支持 JSON 模式；个别网关不认这个字段，_post_chat 会降级重试。
        "response_format": {"type": "json_object"},
    }
    if _thinking_disabled():
        payload["thinking"] = {"type": "disabled"}

    headers = {"Authorization": f"Bearer {api_key}"}
    resp = _post_chat(f"{base_url}/chat/completions", payload, headers)
    return _response_text(resp)


def chat(system: str, messages: list[dict[str, str]]) -> str:
    """按完整消息列表调用文本模型，返回助手回复正文。

    参数:
        system: 系统提示，约束助手的角色与边界。
        messages: 历史消息，元素为 {"role": "user"|"assistant", "content": str}，按时间正序；
            最后一条应当是本次用户输入。

    返回:
        str：模型回复正文，不是 JSON。

    异常:
        AiUnavailable：未配置、请求失败或返回正文为空，调用方应走本地兜底。
    """
    if not configured():
        raise AiUnavailable("未配置模型服务")

    payload: dict = {
        "model": model_name(),
        "messages": [{"role": "system", "content": system}, *messages],
        "temperature": 0.3,
        "max_tokens": MAX_TOKENS,
    }
    if _thinking_disabled():
        payload["thinking"] = {"type": "disabled"}

    started = time.perf_counter()
    resp = _post_chat(
        f"{_base_url()}/chat/completions",
        payload,
        {"Authorization": f"Bearer {_api_key()}"},
    )
    answer = _response_text(resp).strip()
    logger.info(
        "多轮对话完成 provider=openai model=%s 轮数=%s 耗时=%.2fs",
        payload["model"],
        len(messages),
        time.perf_counter() - started,
    )
    return answer


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
