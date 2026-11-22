"""大模型调用入口：OpenAI 兼容接口与 Dify 工作流两种实现。

对外只暴露 extract_json()：给任务名、系统提示和用户输入，返回模型输出的 dict。
字段校验和兜底在调用方（text_extract / receipt_ocr / query_extract）处理。

配置全在环境变量里，见 backend/.env.example：
- AI_PROVIDER=openai 时走 /chat/completions，模型和地址可换成任意兼容服务；
- AI_PROVIDER=dify 时走 Dify 应用的 /v1/workflows/run，输入为 task + text；
- 文本模型与视觉模型可以分开配置：AI_VISION_BASE_URL / AI_VISION_API_KEY
  留空时复用文本模型那一组，填了就各走各的（文本用 DeepSeek、图片用带视觉的兼容服务）。
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

logger = logging.getLogger(__name__)

# 先读 backend/.env 再取模块级配置：应用启动时本模块可能早于 app.db 被导入，
# 不能让 AI_TIMEOUT / AI_RETRIES 落在 dotenv 加载之前。
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# 大模型调用超时；本地规则兜底是瞬时的，这里只约束外部请求。
AI_TIMEOUT = float(os.getenv("AI_TIMEOUT", "20"))

# 额外重试次数：网关偶发 429 / 5xx / 超时，抖动一次就退回规则太浪费。
AI_RETRIES = int(os.getenv("AI_RETRIES", "2"))

# 重试退避基数（秒），第 n 次重试等待 RETRY_BACKOFF * n。
RETRY_BACKOFF = 0.8

# 三种任务共用一套配置，Dify 侧用 task 字段区分工作流分支。
TASK_TEXT = "text_extract"
TASK_RECEIPT = "receipt_ocr"
TASK_QUERY = "query_parse"


class AiUnavailable(Exception):
    """大模型不可用：未配置、超时、返回非 JSON 或 HTTP 错误。

    调用方捕获后走规则兜底，接口本身仍然返回 200。
    """


class _BadRequest(Exception):
    """网关返回 400：通常是请求体里有它不认识的字段。

    由 _call_openai() 判断能否降级（例如去掉 response_format）后再重试，
    不直接当作模型不可用。
    """

    def __init__(self, body: str) -> None:
        super().__init__(body)
        self.body = body


def provider() -> str:
    """当前 provider，取值 openai 或 dify。"""
    return os.getenv("AI_PROVIDER", "openai").strip().lower()


def _api_key() -> str:
    """文本模型的密钥。"""
    return os.getenv("AI_API_KEY", "").strip()


def _vision_api_key() -> str:
    """视觉模型的密钥；没单独配置时复用文本模型密钥。"""
    return os.getenv("AI_VISION_API_KEY", "").strip() or _api_key()


def configured() -> bool:
    """是否配置了可用的文本模型服务。

    openai 需要 AI_API_KEY；dify 需要 DIFY_BASE_URL 和 DIFY_API_KEY。
    """
    if provider() == "dify":
        return bool(os.getenv("DIFY_BASE_URL") and os.getenv("DIFY_API_KEY"))
    return bool(_api_key())


def vision_configured() -> bool:
    """是否配置了图片识别：需要视觉模型名，以及能用的密钥。

    provider 为 dify 时图片识别不单独走工作流，按 visual 配置判定。
    """
    return bool(vision_model_name()) and bool(_vision_api_key())


def model_name() -> str:
    """当前文本模型名，用于 /ai/status 展示；dify 时返回工作流名。"""
    if provider() == "dify":
        return os.getenv("DIFY_WORKFLOW_NAME", "text_extract")
    return os.getenv("AI_MODEL", "deepseek-chat")


def vision_model_name() -> str:
    """视觉模型名；为空表示未配置图片识别。"""
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


def _post_with_retry(url: str, payload: dict, headers: dict[str, str]) -> httpx.Response:
    """发送 POST，对 429 / 5xx / 超时做有限重试。

    参数:
        url: 完整接口地址。
        payload: JSON 请求体。
        headers: 请求头，含 Authorization。

    返回:
        2xx 的 httpx.Response。

    异常:
        _BadRequest：返回 400，由调用方判断能否降级。
        AiUnavailable：重试耗尽，或返回其他不可恢复的状态码。
    """
    last_error = ""
    for attempt in range(AI_RETRIES + 1):
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=AI_TIMEOUT)
        except httpx.TimeoutException:
            last_error = f"请求超时（{AI_TIMEOUT:g}s）"
        except httpx.HTTPError as exc:
            last_error = f"网络错误：{exc}"
        else:
            if resp.status_code == 400:
                raise _BadRequest(resp.text[:500])
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {resp.status_code}：{resp.text[:200]}"
            elif resp.is_success:
                return resp
            else:
                raise AiUnavailable(f"模型服务返回 HTTP {resp.status_code}：{resp.text[:200]}")

        if attempt < AI_RETRIES:
            delay = RETRY_BACKOFF * (attempt + 1)
            logger.warning(
                "大模型请求失败，%.1fs 后重试（第 %s 次）：%s", delay, attempt + 1, last_error
            )
            time.sleep(delay)

    raise AiUnavailable(f"大模型请求失败：{last_error}")


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
        resp = _post_with_retry(url, payload, headers)
    except _BadRequest as exc:
        if "response_format" not in exc.body:
            raise AiUnavailable(f"模型服务拒绝请求：{exc.body[:200]}") from exc
        # 网关不支持 response_format：去掉该字段重试一次，提示词里已经要求 JSON。
        logger.warning("网关不支持 response_format，去掉后重试：%s", exc.body[:120])
        payload.pop("response_format", None)
        try:
            resp = _post_with_retry(url, payload, headers)
        except _BadRequest as retry_exc:
            raise AiUnavailable(f"模型服务拒绝请求：{retry_exc.body[:200]}") from retry_exc

    try:
        return resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AiUnavailable("模型响应缺少 choices 内容") from exc


def _call_dify(task: str, system: str, user: str) -> str:
    """调用 Dify 应用的工作流接口，返回工作流 outputs.result 文本。

    工作流约定：输入变量 task、system_prompt、text，输出变量 result（JSON 字符串）。
    """
    base = os.getenv("DIFY_BASE_URL", "").rstrip("/")
    payload = {
        "inputs": {"task": task, "system_prompt": system, "text": user},
        "response_mode": "blocking",
        "user": "ai-accounting",
    }
    headers = {"Authorization": f"Bearer {os.getenv('DIFY_API_KEY', '')}"}
    try:
        resp = _post_with_retry(f"{base}/v1/workflows/run", payload, headers)
    except _BadRequest as exc:
        raise AiUnavailable(f"Dify 拒绝请求：{exc.body[:200]}") from exc

    try:
        outputs = resp.json()["data"]["outputs"]
    except (KeyError, TypeError, ValueError) as exc:
        raise AiUnavailable("Dify 响应缺少 outputs") from exc
    # 工作流可以直接输出 JSON 字符串，也可以输出名为 result 的字段。
    if isinstance(outputs, dict) and "result" in outputs:
        return str(outputs["result"])
    return json.dumps(outputs, ensure_ascii=False)


def extract_json(
    task: str,
    system: str,
    user: str,
    *,
    image: bytes = b"",
    mime: str = "image/png",
) -> dict:
    """调用大模型并返回解析后的 JSON 对象。

    参数:
        task: TASK_TEXT / TASK_RECEIPT / TASK_QUERY，用于 Dify 侧分流与日志。
        system: 系统提示，约束模型输出字段与取值。
        user: 用户输入文本；图片识别时作为附带的文字说明。
        image / mime: 非空时按视觉模型调用，图片随消息一起发送。

    返回:
        dict：模型输出的 JSON 对象，字段由调用方校验。

    异常:
        AiUnavailable：未配置、请求失败或返回无法解析，调用方应走规则兜底。
    """
    if not configured():
        raise AiUnavailable("未配置大模型密钥")

    if image and provider() == "dify":
        # Dify 工作流的输入只有 text，把图片丢掉会返回与图片无关的结果，这里直接判不可用。
        raise AiUnavailable("Dify 工作流不支持图片输入，图片识别请用 openai 兼容的视觉模型")

    started = time.perf_counter()
    if provider() == "dify":
        model = model_name()
        result = _parse_json(_call_dify(task, system, user))
    else:
        model = vision_model_name() if image else model_name()
        if image and not model:
            raise AiUnavailable("未配置视觉模型 AI_VISION_MODEL")
        result = _parse_json(_call_openai(system, user, model, image, mime))

    logger.info(
        "大模型调用完成 task=%s model=%s 耗时=%.2fs",
        task,
        model,
        time.perf_counter() - started,
    )
    return result
