"""模型请求的公共传输层：超时、重试与错误归类。

llm.py（OpenAI 兼容接口）与 dify.py（工作流与对话应用）共用这里的请求函数，
保证两套 provider 的超时、重试和错误语义一致；调用方只需要捕获 AiUnavailable。

重试只针对 429 / 5xx / 超时；400 单独包成 BadRequest，交给调用方决定能否降级，
其他 4xx 直接判定模型不可用。
"""

import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

# 重试退避基数（秒），第 n 次重试等待 RETRY_BACKOFF * n。
RETRY_BACKOFF = 0.8


class AiUnavailable(Exception):
    """模型服务不可用：未配置、超时、HTTP 错误或返回无法解析。

    调用方捕获后走规则兜底，接口本身仍然返回 200。
    """


class BadRequest(Exception):
    """服务端返回 400：通常是请求体里有它不认识的字段。

    由调用方判断能否降级（例如去掉 response_format）后再重试。
    """

    def __init__(self, body: str) -> None:
        super().__init__(body)
        self.body = body


def timeout() -> float:
    """单次请求超时（秒），每次调用时读环境变量，便于测试和热改。"""
    return float(os.getenv("AI_TIMEOUT", "20"))


def retries() -> int:
    """额外重试次数，每次调用时读环境变量。"""
    return int(os.getenv("AI_RETRIES", "2"))


def _post(
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
) -> httpx.Response:
    """发送 POST 并按状态码决定重试、降级还是判不可用。"""
    last_error = ""
    for attempt in range(retries() + 1):
        try:
            resp = httpx.post(
                url,
                headers=headers,
                json=json_body,
                data=data,
                files=files,
                timeout=timeout(),
            )
        except httpx.TimeoutException:
            last_error = f"请求超时（{timeout():g}s）"
        except httpx.HTTPError as exc:
            last_error = f"网络错误：{exc}"
        else:
            if resp.status_code == 400:
                raise BadRequest(resp.text[:500])
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {resp.status_code}：{resp.text[:200]}"
            elif resp.is_success:
                return resp
            else:
                raise AiUnavailable(
                    f"模型服务返回 HTTP {resp.status_code}：{resp.text[:200]}"
                )

        if attempt < retries():
            delay = RETRY_BACKOFF * (attempt + 1)
            logger.warning(
                "模型请求失败，%.1fs 后重试（第 %s 次）：%s",
                delay,
                attempt + 1,
                last_error,
            )
            time.sleep(delay)

    raise AiUnavailable(f"模型请求失败：{last_error}")


def post_json(url: str, payload: dict, headers: dict[str, str]) -> httpx.Response:
    """发送 JSON 请求，返回 2xx 响应。"""
    return _post(url, headers=headers, json_body=payload)


def post_multipart(
    url: str,
    data: dict,
    files: dict,
    headers: dict[str, str],
) -> httpx.Response:
    """发送 multipart 请求（文件上传），返回 2xx 响应。"""
    return _post(url, headers=headers, data=data, files=files)
