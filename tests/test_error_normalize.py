"""上游错误归一化（A-411 / A-412）。

背景：vLLM 的错误体是 FastAPI 默认格式，`code` 是**数字**：

    {"error": {"message": "...", "type": "BadRequestError", "param": null, "code": 400}}

网关早先原样透传，等于对外同时存在两套错误风格——客户端没法统一按 code 分支。
更糟的一种：调用方把 ref 路径写错，上游回 **500** 并把服务器绝对路径写在 message 里。

本组用例锁三件事：
  1. 任何错误响应里 `code` 都是机器码，绝不是数字（A-411）
  2. 调用方的错不许返 5xx（A-412）
  3. 响应体里不许出现服务器绝对路径（A-412）
"""
from __future__ import annotations

import pytest

from conftest import base_cfg

A = "/models/TTS-A"
pytestmark = pytest.mark.asyncio


def _gw(backend_errors: dict[str, tuple[int, str]], tmp_path) -> dict:
    cfg = base_cfg()
    cfg["upstream_probe_interval_s"] = 0
    cfg["ledger"] = {"enabled": False}
    return cfg


async def test_upstream_numeric_code_is_normalized(gw):
    """上游 400 原样透传会给出 code:400（数字）——必须换成机器码。"""
    async with gw() as g:
        g.backend.errors["speech"] = (400, '{"error":{"message":"bad input","type":"BadRequestError","param":null,"code":400}}')
        r = await g.client.post("/v1/audio/speech", json={"model": A, "input": "hi"})
        assert r.status_code == 400
        err = r.json()["error"]
        assert err["code"] == "bad_request", f"code 必须是机器码，实际 {err['code']!r}"
        assert not isinstance(err["code"], int)


async def test_upstream_500_caused_by_client_becomes_400(gw):
    """ref 路径写错是调用方的错：上游给 500，网关要纠正成 400 并给 param。"""
    async with gw() as g:
        g.backend.errors["speech"] = (
            500,
            '{"error":{"message":"Speech generation failed: [Errno 2] No such file or directory: '
            "'/srv/models/ref/a.wav'\"}}",
        )
        r = await g.client.post("/v1/audio/speech", json={"model": A, "input": "hi", "ref_audio": "x"})
        assert r.status_code == 400, "客户端错误不该返回 5xx"
        err = r.json()["error"]
        assert err["code"] == "bad_request"
        assert err.get("param") == "ref_audio", "拿得到 param 客户端才能自动纠正"
        blob = r.text
        assert "No such file" in blob          # 原因保留（可读）
        assert "/srv/models" not in blob, "不许泄露服务器绝对路径"


async def test_upstream_5xx_stays_5xx_with_machine_code(gw):
    """真正的服务端故障仍是 5xx，但 code 也是机器码。"""
    async with gw() as g:
        g.backend.errors["speech"] = (503, '{"error":{"message":"engine died"}}')
        r = await g.client.post("/v1/audio/speech", json={"model": A, "input": "hi"})
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "upstream_error"


async def test_gateway_own_errors_use_machine_codes(gw):
    """网关自己产生的错误（缺 model / 未知模型）也必须是机器码。"""
    async with gw() as g:
        r1 = await g.client.post("/v1/audio/speech", json={"input": "hi"})
        assert r1.status_code == 400
        assert r1.json()["error"]["code"] == "missing_model"
        r2 = await g.client.post("/v1/audio/speech", json={"model": "/models/nope", "input": "hi"})
        assert r2.status_code == 404
        assert r2.json()["error"]["code"] == "model_not_found"
