"""asleep / absent 的区分（产品要求：别把"后端不存在"显示成"睡眠"）。

语义差别对用户是决定性的：
    睡眠   = 等 1~2 秒能唤醒 → 客户端该重试
    absent = 容器不在/端口不通 → 永远叫不醒 → 客户端该报障，不是等

做法：后台探针每 5s 打一次后端 /health。**睡着时 /health 仍回 200**，
所以探针不会把"睡着"误判成"不存在"——这两件事分别由睡眠状态和后端存活判定。
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import base_cfg

A = "/models/TTS-A"
pytestmark = pytest.mark.asyncio


async def _wait_probe(rounds: int = 3) -> None:
    await asyncio.sleep(0.05 * rounds)


def _cfg(tmp_path) -> dict:
    cfg = base_cfg()
    cfg["upstream_probe_interval_s"] = 0.05
    cfg["ledger"] = {"enabled": False}
    return cfg


async def test_alive_backend_is_not_reported_absent(gw, tmp_path):
    """后端可达时状态是 awake/asleep，绝不能是 absent。"""
    async with gw(_cfg(tmp_path)) as g:
        await _wait_probe()
        body = (await g.client.get("/healthz")).json()
        assert body["upstream_alive"][A] is True
        m = body["models"][A]
        assert m["status"] in ("awake", "asleep"), f"不该是 {m['status']}"
        assert m["backend_alive"] is True


async def test_dead_backend_reports_absent_not_asleep(gw, tmp_path):
    """后端连不上 → absent（用户看到"睡眠"会白等）。"""
    async with gw(_cfg(tmp_path)) as g:
        # 让假后端对该 host 的 health 抛 ConnectError（模拟容器不在）
        g.backend.raises.add("health")
        await _wait_probe()

        body = (await g.client.get("/healthz")).json()
        assert body["upstream_alive"][A] is False
        assert body["models"][A]["status"] == "absent"

        # /v1/models 也要报 absent + backend_alive=False
        entry = next(m for m in (await g.client.get("/v1/models")).json()["data"] if m["id"] == A)
        assert entry["status"] == "absent"
        assert entry["backend_alive"] is False

        # /admin/state 同口径
        st = (await g.client.get("/admin/state")).json()
        row = next(m for m in st["models"] if m["name"] == A)
        assert row["backend_alive"] is False


async def test_sleeping_backend_is_not_absent(gw, tmp_path):
    """睡着的后端 /health 仍是 200 —— 这是"能唤醒"与"不存在"的分界。"""
    async with gw(_cfg(tmp_path)) as g:
        await g.client.post("/admin/sleep", json={"model": A})
        await _wait_probe()
        body = (await g.client.get("/healthz")).json()
        assert body["models"][A]["backend_alive"] is True
        assert body["models"][A]["status"] == "asleep"
