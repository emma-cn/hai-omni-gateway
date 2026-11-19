"""卡级调度：单写者状态机（acquire/release、换台顺序、drain、min_dwell、ttl、reconcile）。

这一层是网关唯一的真逻辑，用**直接驱动 Card** 的方式测：
不经过 HTTP，时间可控（min_dwell / ttl 用 0.05~0.4s 的真时间，跑得快也不会假绿）。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from conftest import base_cfg, multi_cfg
from omni_gateway.scheduler import DrainTimeout, SleepFailed, WakeFailed

pytestmark = pytest.mark.asyncio

A = "/models/TTS-A"
B = "/models/TTS-B"


async def test_first_acquire_wakes_target_without_sleeping(gw):
    async with gw() as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        assert card.awake_model == A
        assert g.backend.count("wakeup") == 1
        assert g.backend.count("sleep") == 0, "卡上原本没人醒，不该先睡"
        await card.release(A)


async def test_acquire_release_accounting(gw):
    async with gw() as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        await card.acquire(A)
        assert card.inflight(A) == 2
        await card.release(A)
        assert card.inflight(A) == 1
        await card.release(A)
        assert card.inflight(A) == 0
        # 多减一次不能把计数打成负数（否则后面的并发闸门全错）
        await card.release(A)
        assert card.inflight(A) == 0


async def test_switch_order_is_sleep_then_wake(gw):
    """换台顺序必须是：睡旧的（权重让出显存）→ 再唤醒新的，反过来必 OOM。"""
    async with gw() as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        await card.release(A)
        await card.acquire(B)
        seq = [a for a in g.backend.actions() if a in ("sleep", "wakeup", "resume")]
        assert seq == ["wakeup", "resume", "sleep", "wakeup", "resume"]
        assert g.backend.hosts_for("sleep") == ["omni-a"]
        assert "omni-b" in g.backend.hosts_for("wakeup")
        assert card.awake_model == B


async def test_wake_is_two_stage(gw):
    """唤醒必须 wakeup + resume 两段，少了 resume 请求会永久挂在准入闸门。"""
    async with gw() as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        assert g.backend.count("wakeup") == 1
        assert g.backend.count("resume") == 1
        assert g.backend.count("health") >= 1


async def test_min_dwell_delays_switch(gw):
    raw = base_cfg()
    raw["cards"]["card4"]["min_dwell_s"] = 0.3
    async with gw(raw) as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        await card.release(A)
        t0 = time.monotonic()
        await card.acquire(B)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.25, f"min_dwell 没生效（{elapsed:.3f}s）"
        assert card.awake_model == B


async def test_drain_waits_for_inflight_before_sleeping(gw):
    """旧模型还有请求在飞时不能睡它（不能 abort 用户请求）。"""
    async with gw() as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        task = asyncio.create_task(card.acquire(B))
        await asyncio.sleep(0.15)
        assert g.backend.count("sleep") == 0, "在飞请求还没结束就睡了旧模型"
        assert not task.done()
        await card.release(A)
        await asyncio.wait_for(task, timeout=2)
        assert g.backend.count("sleep") == 1
        assert card.awake_model == B


async def test_drain_timeout_refuses_switch(gw):
    """drain 超时后**必须拒绝换台**（上游规则：暂停没完成就不许 sleep）。

    历史教训：旧实现打完一条 warning 就继续换台，等于把在飞请求连同引擎状态一起掐掉，
    且事后无法复原（bug #1）。正确行为是抛 DrainTimeout → API 层 503 + Retry-After。
    """
    raw = base_cfg()
    raw["cards"]["card4"]["drain_timeout_s"] = 0.1
    async with gw(raw) as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        with pytest.raises(DrainTimeout):
            await asyncio.wait_for(card.acquire(B), timeout=3)
        # 关键断言：既没睡旧模型，也没唤醒新模型
        assert card.awake_model == A, "drain 未完成却切走了旧模型"
        assert g.backend.count("sleep") == 0, "drain 未完成却发起了 sleep"
        await card.release(A)  # 收尾


async def test_sleep_failure_aborts_switch(gw):
    """旧模型睡不下去时必须中止换台（bug #1：硬换 → 必 OOM → 打死后端进程）。"""
    async with gw() as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        await card.release(A)
        g.backend.errors["sleep"] = (500, "CUDA out of memory")
        wakes_before = g.backend.count("wakeup")
        with pytest.raises(SleepFailed):
            await card.acquire(B)
        assert card.awake_model == A, "睡失败后仍把状态改成了 B"
        assert g.backend.count("wakeup") == wakes_before, "睡失败后仍去唤醒新模型（必 OOM）"
        await card.release(A)


async def test_wakeup_failure_raises_and_clears_state(gw):
    async with gw() as g:
        g.backend.errors["wakeup"] = (500, "CUDA out of memory")
        card = g.sched.card_of(A)
        with pytest.raises(WakeFailed):
            await card.acquire(A)
        # 不能留下"以为醒着"的假状态
        assert card.awake_model is None
        assert card.inflight(A) == 0
        fail_events = [e for e in card.events if e["kind"] == "fail"]
        assert fail_events and "wake" in fail_events[0]["detail"]


async def test_resume_failure_raises(gw):
    """wakeup 成功但 resume 失败 = 服务活着不干活，必须当失败处理。"""
    async with gw() as g:
        g.backend.errors["resume"] = (500, "no resume")
        card = g.sched.card_of(A)
        with pytest.raises(WakeFailed) as ei:
            await card.acquire(A)
        assert "resume" in str(ei.value)
        assert card.awake_model is None


async def test_health_timeout_raises(gw):
    raw = base_cfg()
    raw["models"][A]["wake_timeout_s"] = 0.3
    async with gw(raw) as g:
        g.backend.dead.add("omni-a")
        card = g.sched.card_of(A)
        with pytest.raises(WakeFailed) as ei:
            await card.acquire(A)
        assert "health" in str(ei.value)
        assert card.awake_model is None


async def test_recovers_after_wake_failure(gw):
    async with gw() as g:
        g.backend.errors["wakeup"] = (500, "OOM")
        card = g.sched.card_of(A)
        with pytest.raises(WakeFailed):
            await card.acquire(A)
        del g.backend.errors["wakeup"]
        await card.acquire(A)          # 第二次必须能自己恢复
        assert card.awake_model == A
        assert card.inflight(A) == 1


async def test_concurrency_gate_queues_instead_of_failing(gw):
    """concurrency=1：第二条请求排队等待，而不是 5xx，也不是越界并发。"""
    raw = base_cfg()
    raw["models"][A]["concurrency"] = 1
    async with gw(raw) as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        second = asyncio.create_task(card.acquire(A))
        await asyncio.sleep(0.15)
        assert not second.done(), "并发闸门没生效：第二条直接进去了"
        await card.release(A)
        await asyncio.wait_for(second, timeout=2)
        assert card.inflight(A) == 1


async def test_concurrent_acquire_wakes_only_once(gw):
    """并发唤醒的 N 条请求，只该真正唤醒一次（单一写者）。"""
    raw = base_cfg()
    raw["models"][A]["concurrency"] = 8
    async with gw(raw) as g:
        card = g.sched.card_of(A)
        await asyncio.gather(*[card.acquire(A) for _ in range(4)])
        assert g.backend.count("wakeup") == 1
        assert card.inflight(A) == 4


async def test_ttl_idle_sleep(gw):
    raw = base_cfg()
    raw["models"][A]["ttl_s"] = 0.05
    async with gw(raw) as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        await card.release(A)
        await asyncio.sleep(0.12)
        await card.sleep_if_idle()
        assert g.backend.count("sleep") == 1
        assert card.awake_model is None
        assert any(e["kind"] == "idle_sleep" for e in card.events)


async def test_ttl_zero_never_sleeps(gw):
    raw = base_cfg()
    raw["models"][A]["ttl_s"] = 0
    async with gw(raw) as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        await card.release(A)
        await asyncio.sleep(0.1)
        await card.sleep_if_idle()
        assert g.backend.count("sleep") == 0
        assert card.awake_model == A


async def test_no_idle_sleep_while_inflight(gw):
    """用户正在等产物时被空闲回收睡掉 = 请求挂死。"""
    raw = base_cfg()
    raw["models"][A]["ttl_s"] = 0.01
    async with gw(raw) as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        await asyncio.sleep(0.1)
        await card.sleep_if_idle()
        assert g.backend.count("sleep") == 0
        assert card.awake_model == A


async def test_idle_sleep_failure_keeps_awake_state(gw):
    """睡失败不能假装睡下了：状态留着，下一轮再试。"""
    raw = base_cfg()
    raw["models"][A]["ttl_s"] = 0.01
    async with gw(raw) as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        await card.release(A)
        g.backend.errors["sleep"] = (500, "cannot sleep")
        await asyncio.sleep(0.05)
        await card.sleep_if_idle()
        assert g.backend.count("sleep") == 1
        assert card.awake_model == A, "睡眠失败却把状态清成了睡着"
        assert any(e["kind"] == "fail" and "idle_sleep" in e["detail"] for e in card.events)


async def test_reconcile_sleeps_every_model_and_zeroes_state(gw):
    """启动对账：把所有后端睡一遍再归零（否则残留醒着的模型会抢显存）。"""
    async with gw(multi_cfg()) as g:
        g.backend.clear()
        g.sched.card_of(A)._awake = A
        await g.sched.reconcile()
        assert g.backend.count("sleep") == len(g.cfg.models)
        assert all(card.awake_model is None for card in g.sched.cards.values())
        assert all(card.inflight(m) == 0
                   for card in g.sched.cards.values() for m in card.models)


async def test_reconcile_survives_unreachable_backend(gw):
    """某个后端没起来不能拖死启动（实测过 voxcpm2 还没监听时的 ConnectError）。"""
    async with gw() as g:
        g.backend.raises.add("sleep")
        await g.sched.reconcile()          # 不抛异常
        events = [e for card in g.sched.cards.values() for e in card.events]
        assert any(e["kind"] == "reconcile" and ("fail" in e["detail"] or "error" in e["detail"])
                   for e in events)


async def test_two_models_same_card_never_awake_together(gw):
    async with gw() as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        await card.release(A)
        await card.acquire(B)
        assert g.backend.state["omni-a"] == "asleep"
        assert g.backend.state["omni-b"] == "awake"
