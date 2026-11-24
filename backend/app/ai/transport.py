"""模型请求的公共传输层：超时、重试与错误归类。

llm.py（OpenAI 兼容接口）与 dify.py（工作流与对话应用）共用这里的请求函数，
保证两套 provider 的超时、重试和错误语义一致；调用方只需要捕获 AiUnavailable。

重试只针对 429 / 5xx / 超时；400 单独包成 BadRequest，交给调用方决定能否降级，
其他 4xx 直接判定模型不可用。
"""

import json
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

# 重试退避基数（秒），第 n 次重试等待 RETRY_BACKOFF * n。
RETRY_BACKOFF = 0.8

# 进程级连接池：每次请求重建 TLS 连接既慢，也容易被网关中途断开（SSL EOF / RST）。
# httpx.Client 线程安全，FastAPI 的同步路由共用这一个即可；超时按请求传入，
# 因此 AI_TIMEOUT 改了也不用重建。
_client = httpx.Client(
    limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
)


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


def extra_headers() -> dict[str, str]:
    """环境变量 AI_EXTRA_HEADERS 里声明的附加请求头。

    部分网关要靠自定义请求头做路由或会话关联，这类头与具体 provider 绑定，
    放环境变量比写进代码更合适；值必须是 JSON 对象。

    返回:
        dict：请求头键值对；未配置、不是合法 JSON 或不是对象时返回空 dict，
        调用方因此不需要区分「未配置」和「配置写错」。
    """
    raw = os.getenv("AI_EXTRA_HEADERS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("AI_EXTRA_HEADERS 不是合法 JSON，本次不附加请求头")
        return {}
    if not isinstance(data, dict):
        logger.warning("AI_EXTRA_HEADERS 必须是 JSON 对象，本次不附加请求头")
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _post(
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
) -> httpx.Response:
    """发送 POST 并按状态码决定重试、降级还是判不可用。"""
    # 附加请求头放前面，让调用方显式传的 Authorization 等保持优先。
    request_headers = {**extra_headers(), **headers}
    last_error = ""
    for attempt in range(retries() + 1):
        try:
            resp = _client.post(
                url,
                headers=request_headers,
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
