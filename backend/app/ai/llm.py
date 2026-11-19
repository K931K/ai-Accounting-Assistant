"""大模型调用入口：OpenAI 兼容接口与 Dify 工作流两种实现。

对外只暴露 extract_json()：给任务名、系统提示和用户输入，返回模型输出的 dict。
字段校验和兜底在调用方（text_extract / receipt_ocr / query_extract）处理。

配置全在环境变量里，见 backend/.env.example：
- AI_PROVIDER=openai 时走 /chat/completions，模型和地址可换成任意兼容服务；
- AI_PROVIDER=dify 时走 Dify 应用的 /v1/workflows/run，输入为 task + text。
未配置密钥时 configured() 为 False，调用方直接走规则兜底。
"""

import base64
import json
import os
import re

import httpx

# 大模型调用超时；本地规则兜底是瞬时的，这里只约束外部请求。
AI_TIMEOUT = float(os.getenv("AI_TIMEOUT", "20"))

# 三种任务共用一套配置，Dify 侧用 task 字段区分工作流分支。
TASK_TEXT = "text_extract"
TASK_RECEIPT = "receipt_ocr"
TASK_QUERY = "query_parse"


class AiUnavailable(Exception):
    """大模型不可用：未配置、超时、返回非 JSON 或 HTTP 错误。

    调用方捕获后走规则兜底，接口本身仍然返回 200。
    """


def provider() -> str:
    """当前 provider，取值 openai 或 dify。"""
    return os.getenv("AI_PROVIDER", "openai").strip().lower()


def configured() -> bool:
    """是否配置了可用的模型服务。

    openai 需要 AI_API_KEY；dify 需要 DIFY_BASE_URL 和 DIFY_API_KEY。
    """
    if provider() == "dify":
        return bool(os.getenv("DIFY_BASE_URL") and os.getenv("DIFY_API_KEY"))
    return bool(os.getenv("AI_API_KEY"))


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

    image 非空时把图片作为 image_url 一起发送，用于小票识别。
    """
    if image:
        content: object = [
            {"type": "text", "text": user},
            {"type": "image_url", "image_url": {"url": _to_data_url(image, mime)}},
        ]
    else:
        content = user

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "temperature": 0.2,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {os.getenv('AI_API_KEY', '')}"}
    try:
        resp = httpx.post(
            f"{_base_url()}/chat/completions",
            json=payload,
            headers=headers,
            timeout=AI_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AiUnavailable(f"模型服务请求失败：{exc}") from exc
    try:
        return resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
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
        resp = httpx.post(
            f"{base}/v1/workflows/run",
            json=payload,
            headers=headers,
            timeout=AI_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise AiUnavailable(f"Dify 请求失败：{exc}") from exc
    try:
        outputs = resp.json()["data"]["outputs"]
    except (KeyError, TypeError) as exc:
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
        task: TASK_TEXT / TASK_RECEIPT / TASK_QUERY，用于 Dify 侧分流。
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

    if provider() == "dify":
        return _parse_json(_call_dify(task, system, user))

    model = vision_model_name() if image else model_name()
    if image and not model:
        raise AiUnavailable("未配置视觉模型 AI_VISION_MODEL")
    return _parse_json(_call_openai(system, user, model, image, mime))
