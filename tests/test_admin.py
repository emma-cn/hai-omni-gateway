"""管理面：/admin/state、手动唤醒/睡下、事件环。"""
from __future__ import annotations

import time

import pytest

from conftest import base_cfg, json_of

pytestmark = pytest.mark.asyncio

A = "/models/TTS-A"
B = "/models/TTS-B"


async def test_admin_state_shape(gw):
    async with gw() as g:
        body = json_of(await g.client.get("/admin/state"))
        assert body["version"]
        assert isinstance(body["cards"], list) and len(body["cards"]) == 1
        card = body["cards"][0]
        assert card["card"] == "card4"
        assert card["awake"] is None
        assert card["inflight"] == {A: 0, B: 0}
        assert card["min_dwell_s"] == 0.0
        assert isinstance(card["events"], list)

        models = {m["name"]: m for m in body["models"]}
        assert set(models) == {A, B}
        m = models[A]
        for key in ("card", "capabilities", "status", "inflight", "ttl_s",
                    "lease_s", "concurrency", "base_url"):
            assert key in m
        assert m["capabilities"] == ["speech"]
        assert m["base_url"] == "http://omni-a:60589"


async def test_admin_state_reports_awake_and_inflight(gw):
    async with gw() as g:
        await g.sched.acquire(A)
        body = json_of(await g.client.get("/admin/state"))
        card = body["cards"][0]
        assert card["awake"] == A
        assert card["inflight"][A] == 1
        assert card["last_switch"] > 0
        await g.sched.release(A)


async def test_admin_wake_unknown_model_404(gw):
    async with gw() as g:
        r = await g.client.post("/admin/wake", json={"model": "/models/NOPE"})
        assert r.status_code == 404
        assert "未知模型" in json_of(r)["detail"]


async def test_admin_wake_success(gw):
    async with gw() as g:
        r = await g.client.post("/admin/wake", json={"model": A})
        assert r.status_code == 200
        assert json_of(r)["status"] == "awake"
        card = g.sched.card_of(A)
        assert card.awake_model == A
        assert any(e["kind"] == "wake" and e["model"] == A for e in card.events)


async def test_admin_wake_failure_503(gw):
    async with gw() as g:
        g.backend.errors["wakeup"] = (500, "CUDA out of memory")
        r = await g.client.post("/admin/wake", json={"model": A})
        assert r.status_code == 503
        assert "唤醒失败" in json_of(r)["detail"]
        assert g.sched.card_of(A).awake_model is None


async def test_admin_sleep_success(gw):
    async with gw() as g:
        await g.client.post("/admin/wake", json={"model": A})
        r = await g.client.post("/admin/sleep", json={"model": A})
        assert r.status_code == 200
        assert json_of(r)["status"] == "asleep"
        assert g.sched.card_of(A).awake_model is None


async def test_admin_sleep_refuses_when_inflight(gw):
    """有请求在飞时拒绝睡下 —— 否则等于掐掉用户请求。"""
    async with gw() as g:
        await g.sched.acquire(A)
        r = await g.client.post("/admin/sleep", json={"model": A})
        assert r.status_code == 409
        assert "在飞" in json_of(r)["detail"]
        assert g.sched.card_of(A).awake_model == A
        await g.sched.release(A)


async def test_admin_sleep_unknown_model_404(gw):
    async with gw() as g:
        r = await g.client.post("/admin/sleep", json={"model": "/models/NOPE"})
        assert r.status_code == 404


async def test_admin_wake_switches_card_away_from_other_model(gw):
    """手动唤醒也要走"先睡旧的"：否则两个模型同时醒 = OOM。"""
    async with gw() as g:
        await g.sched.acquire(A)
        await g.sched.release(A)
        await g.client.post("/admin/wake", json={"model": B})
        assert g.backend.state["omni-a"] == "asleep"
        assert g.sched.card_of(A).awake_model == B


async def test_events_ring_is_bounded(gw):
    async with gw() as g:
        card = g.sched.card_of(A)
        for i in range(250):
            card._event("wake", A, f"n={i}")
        body = json_of(await g.client.get("/admin/state"))
        events = body["cards"][0]["events"]
        assert len(events) == 50, "接口只回最新 50 条"
        assert len(card.events) == 200, "内存事件环上限 200"
        assert events[0]["detail"] == "n=249", "最新事件在最前"
        assert "ts" in events[0] and events[0]["ts"] <= time.time() + 1


async def test_events_survive_restart_only_in_memory(gw):
    """事件环是进程内的（重启即空）：/admin/state 里没有持久化字段，别指望查历史。"""
    async with gw() as g:
        assert json_of(await g.client.get("/admin/state"))["cards"][0]["events"] != []
        # reconcile 事件在启动时就写进去了，之后没有外部来源
        kinds = {e["kind"] for e in json_of(await g.client.get("/admin/state"))["cards"][0]["events"]}
        # 事件类型随实现演进：老后端回退盲睡写 reconcile，新后端读状态写 reconcile_status
        assert kinds <= {"reconcile", "reconcile_status", "reconcile_awake"}
