"""能力目录：把模型配置翻译成 OpenAI 客户端能自解释的字段。

设计原则：**只输出我们真的知道的值**。
不知道语种就返回 `[]` 并附带 `declared: false`，绝不编造"支持 30 种语言"这类漂亮数字——
一个能力目录如果会撒谎，客户端就没法据此做分支，那还不如没有。

`modalities` 由 `capabilities` 推导（这两者是同一条事实的两种说法）：
    speech        -> audio
    transcription -> audio（输入）+ text（输出）
    image         -> image
    video         -> video
未知能力原样透传（不认识的 capability 说明部署方自己扩展了，别丢）。
"""
from __future__ import annotations

from typing import Any

from .config import ModelConfig

# capability -> (输出模态, 输入模态)
_CAPABILITY_MODALITIES: dict[str, tuple[list[str], list[str]]] = {
    "speech": (["audio"], ["text"]),
    "transcription": (["text"], ["audio"]),
    "image": (["image"], ["text"]),
    "video": (["video"], ["text"]),
}


def modalities_of(model: ModelConfig) -> dict[str, list[str]]:
    outputs: list[str] = []
    inputs: list[str] = []
    for cap in model.capabilities:
        out, inp = _CAPABILITY_MODALITIES.get(cap, ([cap], []))
        outputs += [m for m in out if m not in outputs]
        inputs += [m for m in inp if m not in inputs]
    return {"input": inputs, "output": outputs}


def catalog_of(model: ModelConfig, *, status: str, inflight: int, awake: bool) -> dict[str, Any]:
    """单个模型的目录条目。`declared` 明确告诉客户端哪些字段是"未声明"而不是"不支持"。"""
    langs = list(getattr(model, "languages", []) or [])
    rates = list(getattr(model, "sample_rates", []) or [])
    fmts = list(getattr(model, "formats", []) or [])
    max_chars = int(getattr(model, "max_input_chars", 0) or 0)
    return {
        "modalities": modalities_of(model),
        "languages": langs,
        "max_input_chars": max_chars,
        "sample_rates": rates,
        "formats": fmts,
        "capabilities": list(model.capabilities),
        "status": status,          # awake / asleep
        "awake": awake,
        "inflight": inflight,
        # 哪个字段是"没声明"（空数组既可能是"没有"也可能是"没填"，客户端需要区分）
        "declared": {
            "languages": bool(langs),
            "max_input_chars": max_chars > 0,
            "sample_rates": bool(rates),
            "formats": bool(fmts),
        },
        # 延迟档位：只要有模型醒着就是 warm（换台/唤醒代价已知），否则 cold（要先唤醒）
        "latency_class": "warm" if awake else "cold",
        "lease_s": float(model.lease_s),
    }


def endpoint_families() -> dict[str, list[str]]:
    """404 自描述用：告诉客户端"这里支持哪些路子"。

    历史问题（issue #10）：旧页面轮询 `/api/events` 拿到默认 404（HTML/空体），
    客户端无法自解 —— 到底是路径错了、还是服务没实现、还是版本不对。
    """
    return {
        "audio": [
            "POST /v1/audio/speech",
            "POST /v1/audio/transcriptions",
            "GET /v1/audio/voices",
            "POST /v1/audio/voices",
            "DELETE /v1/audio/voices/{name}",
        ],
        "images": ["POST /v1/images/generations", "POST /v1/images/edits"],
        "videos": [
            "POST /v1/videos",
            "GET /v1/videos",
            "GET /v1/videos/{id}",
            "GET /v1/videos/{id}/content",
        ],
        "models": ["GET /v1/models"],
        "ops": [
            "GET /healthz",
            "GET /admin/state",
            "POST /admin/wake",
            "POST /admin/sleep",
            "POST /admin/reconcile",
        ],
    }