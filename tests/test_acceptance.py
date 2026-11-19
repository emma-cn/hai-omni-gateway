"""README「验收标准」四条 + DESIGN §4 背压的单元化版本。

每条都对应一句承诺，挂了就说明文档与实现不一致：
  1. 官方 SDK 直连能出音频（真后端才有意义，见 E2E 脚本）
  2. WAV 与直连后端 md5 一致（这里用"字节完全一致"钉住透传无损）
  3. 睡着状态的请求：N 秒内唤醒并返回（带 X-Omni-Wake-ETA），否则 503 + Retry-After；
     **不允许挂 180 秒无响应**
  4. 换台期间第二个模型的请求排队而非失败；换台完成后由一次唤醒服务队列里的多条请求
"""
from __future__ import annotations

import asyncio
import time

import pytest

from conftest import base_cfg, json_of, speech_body

pytestmark = pytest.mark.asyncio

A = "/models/TTS-A"
B = "/models/TTS-B"


# ------------------------------------------------------- 验收 2：透传无损（md5）


async def test_acceptance_wav_bytes_identical_to_upstream(gw):
    async with gw() as g:
        r = await g.client.post("/v1/audio/speech", json=speech_body(A))
        assert r.status_code == 200
        assert r.content == g.backend.audio, "网关改写了音频字节（md5 会对不上）"
        assert r.headers["content-type"].startswith("audio/wav")


# ------------------------------------- 验收 3：睡着时请求要快、要能唤醒、不能挂死


async def test_acceptance_sleeping_request_wakes_and_returns(gw):
    async with gw() as g:
        card = g.sched.card_of(A)
        assert card.awake_model is None            # 刚启动对账完，全部睡着
        t0 = time.monotonic()
        r = await g.client.post("/v1/audio/speech", json=speech_body(A))
        elapsed = time.monotonic() - t0
        assert r.status_code == 200
        assert card.awake_model == A
        assert elapsed < 2.0, f"唤醒花了 {elapsed:.2f}s，超出预期"


@pytest.mark.xfail(strict=True, reason="未实现：README 承诺带 X-Omni-Wake-ETA，实际响应无此头（见报告 A2）")
async def test_acceptance_wake_eta_header_present(gw):
    async with gw() as g:
        r = await g.client.post("/v1/audio/speech", json=speech_body(A))
        assert "x-omni-wake-eta" in {k.lower() for k in r.headers}


async def test_acceptance_wake_failure_returns_503_with_retry_after(gw):
    """唤醒失败（OOM / 健康超时）必须快速 503 + Retry-After，不能挂 180s。"""
    raw = base_cfg()
    raw["models"][A]["wake_timeout_s"] = 0.3
    async with gw(raw) as g:
        g.backend.dead.add("omni-a")
        t0 = time.monotonic()
        r = await g.client.post("/v1/audio/speech", json=speech_body(A))
        elapsed = time.monotonic() - t0
        assert r.status_code == 503
        assert r.headers.get("retry-after") == "10"
        # 分类契约：显存不足必须是 wake_oom（可重试），不是笼统的 wake_failed
        assert json_of(r)["error"]["type"] == "wake_oom"
        assert elapsed < 3.0, f"唤醒失败后拖了 {elapsed:.1f}s 才回"


# --------------------------------------------- 验收 4：换台期间排队而不是失败


async def test_acceptance_requests_during_switch_are_queued_not_failed(gw):
    """A 刚醒、B 被请求：换台期间两条 B 的请求都得 200，不能有 5xx。"""
    raw = base_cfg()
    raw["cards"]["card4"]["min_dwell_s"] = 0.15
    async with gw(raw) as g:
        await g.client.post("/v1/audio/speech", json=speech_body(A))
        results = await asyncio.gather(
            *[g.client.post("/v1/audio/speech", json=speech_body(B)) for _ in range(3)]
        )
        assert [r.status_code for r in results] == [200, 200, 200]
        assert g.sched.card_of(B).awake_model == B


async def test_acceptance_one_wake_serves_the_whole_queue(gw):
    """换台完成后由**一次**唤醒服务队列里的多条请求（不能每条都唤醒一次）。"""
    raw = base_cfg()
    raw["cards"]["card4"]["min_dwell_s"] = 0.15
    raw["models"][B]["concurrency"] = 8      # 三条并发，别让并发闸门先把它挡成"排队"
    async with gw(raw) as g:
        card = g.sched.card_of(A)
        await card.acquire(A)
        await card.release(A)
        g.backend.clear()
        await asyncio.gather(*[card.acquire(B) for _ in range(3)])
        assert g.backend.hosts_for("wakeup") == ["omni-b"], "同一目标被唤醒了多次"
        assert card.inflight(B) == 3


# ------------------------------------------------- 背压：队列上限（DESIGN §4）


async def test_acceptance_queue_limit_returns_429(gw):
    """queue_limit 超了必须 429 + Retry-After，而不是让客户端无限等。"""
    raw = base_cfg()
    raw["models"][A]["concurrency"] = 1
    # queue_limit 的语义：**允许排队（等待中）的请求数上限**；0 = 不排队，满了直接 429。
    # 产品验收 A-203 的算术也是这个口径：concurrency=4 + queue_limit=16 → 20 个进系统、其余 429。
    raw["queue_limit"] = 0
    async with gw(raw) as g:
        g.backend.delays["speech"] = 0.6
        first = asyncio.create_task(g.client.post("/v1/audio/speech", json=speech_body(A)))
        try:
            await asyncio.sleep(0.05)
            second = await asyncio.wait_for(
                g.client.post("/v1/audio/speech", json=speech_body(A)), timeout=0.4
            )
            assert second.status_code == 429
            assert "retry-after" in {k.lower() for k in second.headers}
        finally:
            await first


async def test_acceptance_queue_over_limit_returns_429(gw):
    """目标契约：队列满时必须 429（而不是让客户端无限等）。

    与 `test_acceptance_queue_limit_returns_429` 的区别：那条测"concurrency 满 + queue_limit=1"，
    这条测"没有 queue_limit 也应给背压"，两者都表达目标而非现状。
    """
    raw = base_cfg()
    raw["models"][A]["concurrency"] = 1
    raw["queue_limit"] = 0     # 见上一条用例的语义说明
    async with gw(raw) as g:
        g.backend.delays["speech"] = 0.3
        first = asyncio.create_task(g.client.post("/v1/audio/speech", json=speech_body(A)))
        await asyncio.sleep(0.05)
        second = await asyncio.wait_for(
            g.client.post("/v1/audio/speech", json=speech_body(A)), timeout=2.0
        )
        await first
        assert second.status_code == 429, "队列满却让客户端干等（无背压）"


# ------------------------------------------------------ 无鉴权现状（安全基线）


@pytest.mark.xfail(strict=True, reason="未实现：P0-1 鉴权与多租户 / 验收 A-101~A-104 —— 运维面应要求 admin token")
async def test_acceptance_admin_surface_requires_auth(gw):
    """目标契约：`/admin/*` 无凭据必须 401。

    这条用例表达的是**目标**，不是现状。历史教训：早期版本写成
    `assert r.status_code == 200, "管理面竟然有鉴权？那就更新这条基线"`，
    等于把缺陷钉成期望值——等鉴权真落地时，这句模糊指引最可能被执行成"把鉴权回退掉"。
    `xfail(strict=True)` 保证鉴权落地后这条自动变红，逼人删标记而不是删功能。
    """
    async with gw() as g:
        r = await g.client.post("/admin/sleep", json={"model": A})
        assert r.status_code == 401, "运维面没有鉴权：任何客户端都能把模型睡掉"
