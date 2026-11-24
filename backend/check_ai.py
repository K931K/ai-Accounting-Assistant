"""模型接入自检：确认 backend/.env 里的配置能真的跑通三条 AI 链路。

用法（在 backend 目录执行）：
    uv run python check_ai.py
    uv run python check_ai.py --image 小票.png

检查项：
  1. 文本模型：一句话记账（/transactions/parse 用的同一条链路）；
  2. 文本模型：自然语言查账的条件解析（/query/ask 用的同一条链路）；
  3. 视觉模型：小票识别（传入 --image 且配置了视觉模型时才跑）；
  4. 多轮对话：Dify 对话应用或兼容接口（未配置时 /agent/chat 走本地兜底）。

每一项都会打印解析结果与耗时；engine 为 rule 表示模型调用失败后退回了规则，
具体原因看日志里的 warning。配置不全时脚本说明缺哪一项并以退出码 1 结束。
"""

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

# 与 app/db.py 一致：按文件位置读 backend/.env，不受启动目录影响。
load_dotenv(Path(__file__).resolve().parent / ".env")

from app.ai import dify, llm, query_extract, receipt_ocr, text_extract  # noqa: E402


def _print_status() -> None:
    """打印当前模型配置，对应 GET /ai/status 的字段。"""
    missing = llm.missing_config()
    print("== 模型配置 ==")
    print(f"provider        : {llm.provider()}")
    print(f"文本模型         : {llm.model_name() if llm.configured() else '未配置'}")
    print(f"视觉模型         : {llm.vision_model_name() or '未配置'}")
    print(f"图片识别可用     : {'是' if llm.vision_configured() else '否'}")
    print(f"多轮对话可用     : {'是' if llm.chat_configured() else '否（/agent/chat 走本地兜底）'}")
    print(f"缺失的配置项     : {'、'.join(missing) if missing else '无'}")
    print(f"超时 / 重试      : {llm.AI_TIMEOUT:g}s / {llm.AI_RETRIES} 次")


def _check_text() -> bool:
    """跑一条一句话记账，engine 为 llm 才算模型真的通了。"""
    print("\n== 一句话记账 ==")
    result = text_extract.extract("昨天午饭在沙县吃了 23 块")
    print(f"engine      : {result.engine}")
    print(f"amount_cents: {result.amount_cents}")
    print(f"merchant    : {result.merchant}")
    print(f"occurred_at : {result.occurred_at}")
    print(f"category    : {result.category.value if result.category else None}")
    print(f"confidence  : {result.confidence}")
    return result.engine == "llm"


def _check_query() -> bool:
    """跑一条查账条件解析，确认问句能转成类别与时间区间。"""
    print("\n== 自然语言查账 ==")
    intent = query_extract.parse("这个月吃饭花了多少", datetime.now(UTC))
    print(f"engine   : {intent.engine}")
    print(f"category : {intent.category.value if intent.category else None}")
    print(f"intent   : {intent.intent}")
    print(f"date_from: {intent.date_from}")
    print(f"date_to  : {intent.date_to}")
    return intent.engine == "llm"


def _check_receipt(image_path: str) -> bool:
    """用指定图片跑一次小票识别，检查金额、商户与时间是否抽出来。"""
    print("\n== 小票识别 ==")
    if not llm.vision_configured():
        print("跳过：未配置 AI_VISION_MODEL / AI_VISION_API_KEY")
        return True

    path = Path(image_path)
    if not path.is_file():
        print(f"跳过：找不到图片 {path}")
        return True

    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    result = receipt_ocr.recognize(path.read_bytes(), mime)
    print(f"engine      : {result.engine}")
    print(f"amount_cents: {result.amount_cents}")
    print(f"merchant    : {result.merchant}")
    print(f"occurred_at : {result.occurred_at}")
    print(f"category    : {result.category.value if result.category else None}")
    print(f"raw_text    : {(result.raw_text or '')[:80]}")
    return result.engine == "llm"


def _check_chat() -> bool:
    """用对话链路跑一轮，确认多轮对话真的接通。"""
    print("\n== 多轮对话 ==")
    if not llm.chat_configured():
        print("跳过：未配置对话链路，/agent/chat 会走本地兜底")
        return True

    if llm.provider() == "dify":
        result = dify.chat("你好，帮我看看这个月花了多少", None)
        print(f"conversation_id: {result['conversation_id']}")
        print(f"reply          : {result['answer'][:120]}")
        return bool(result["answer"] and result["conversation_id"])

    answer = llm.chat(
        "你是记账助手，用一句中文回答。",
        [{"role": "user", "content": "你好，你能帮我做什么？"}],
    )
    print(f"reply          : {answer[:120]}")
    return bool(answer)


def main() -> int:
    """按配置逐项自检，全部通过返回 0。"""
    parser = argparse.ArgumentParser(description="检查大模型接入是否可用")
    parser.add_argument("--image", default="", help="用于小票识别自检的图片路径")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    _print_status()
    if not llm.configured():
        print(f"\n模型服务未配置，缺少：{'、'.join(llm.missing_config())}。")
        if llm.provider() == "dify":
            print("没有 Dify 时也可以把 AI_PROVIDER 改成 openai，填 AI_API_KEY 走兼容接口。")
        print("补齐后重跑；未配置期间三条 AI 链路都会退回规则解析。")
        return 1

    text_ok = _check_text()
    query_ok = _check_query()
    receipt_ok = _check_receipt(args.image) if args.image else True
    chat_ok = _check_chat()

    print("\n== 结论 ==")
    checks = (
        ("一句话记账", text_ok),
        ("自然语言查账", query_ok),
        ("小票识别", receipt_ok),
        ("多轮对话", chat_ok),
    )
    for name, ok in checks:
        print(f"{name}: {'通过' if ok else '失败（已退回规则，检查日志）'}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
