"""请求账本用例（A-601 / A-602）。

要证明的两件事：
  1. 每条请求**一行**日志落盘（含失败请求——没有失败样本，p95 只能算成功的，归因会偏）
  2. 三段耗时**分开记**（排队 / 唤醒 / 生成），否则 p95 一抖就无法归因
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from conftest import base_cfg

A = "/models/TTS-A"
pytestmark = pytest.mark.asyncio


def _read_lines(path: str, n: int = 50) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f.read().splitlines() if line.strip()][-n:]


def _cfg(path: str, enabled: bool = True) -> dict:
    cfg = base_cfg()
    cfg["ledger"] = {"enabled": enabled, "path": path, "ring": 50}
    return cfg


async def test_ledger_writes_one_line_per_request(gw, tmp_path):
    """A-601：一个合成请求 = 一行 JSONL，字段稳定到可以直接 jq。"""
    path = str(tmp_path / "requests.jsonl")
    async with gw(_cfg(path)) as g:
        await g.client.get("/v1/models")            # 非 /v1/audio/*，不记账
        r = await g.client.post(
            "/v1/audio/speech", json={"model": A, "input": "你好", "voice": "default"}
        )
        assert r.status_code == 200
        # 缓冲在 shutdown 时才兜底刷盘，所以断言放在 async with 之后
    lines = _read_lines(path)
    assert len(lines) == 1, f"应该只有 1 行（合成请求），实际 {len(lines)}"
    e = lines[0]
    for field in (
        "request_id", "model", "path", "status",
        "queue_wait_ms", "wake_ms", "gen_ms", "total_ms",
    ):
        assert field in e, f"账本缺字段 {field}"
    assert e["model"] == A
    assert e["path"] == "/v1/audio/speech"
    assert e["status"] == 200
    assert e["bytes_out"] > 0, "音频请求应记录输出字节数"


async def test_ledger_separates_three_segments(gw, tmp_path):
    """A-602：排队 / 唤醒 / 生成 三段分别有值，且能通过 /admin/ledger 读到聚合。"""
    path = str(tmp_path / "requests.jsonl")
    async with gw(_cfg(path)) as g:
        r = await g.client.post("/v1/audio/speech", json={"model": A, "input": "hi"})
        assert r.status_code == 200
        body = (await g.client.get("/admin/ledger")).json()
        assert body["enabled"] is True
        summary = body["summary"]
        assert summary["count"] >= 1
        for name in ("queue_wait_ms", "wake_ms", "upstream_wait_ms", "gen_ms", "total_ms"):
            assert name in summary, f"聚合里缺 {name}"
            assert summary[name]["p50"] is not None
        entry = body["entries"][-1]
        # 冷启首条必然换台 → wake_ms 应 > 0（分钟级精度的字段不该是 None）
        assert entry["wake_ms"] > 0, "冷启首条应记录到换台/唤醒耗时"
        assert entry["queue_wait_ms"] >= 0
        assert entry["gen_ms"] >= 0


async def test_upstream_wait_is_attributed_not_generation(gw, tmp_path):
    """A-602 的核心：上游排队（首字节前）必须单列，不能贴成"生成"。

    踩过：第一版只有一个 gen_ms，8 并发下 gen p95 = 44.3s 而 queue_wait 最大 0.02ms，
    p95/p50 差 62 倍，归因等于没有。现在 upstream_wait_ms（首字节前）+ gen_ms（首字节后）分段。
    """
    path = str(tmp_path / "requests.jsonl")
    async with gw(_cfg(path)) as g:
        g.backend.delays["speech"] = 0.30      # 让上游慢 0.3s
        r = await g.client.post("/v1/audio/speech", json={"model": A, "input": "hi"})
        assert r.status_code == 200
    e = _read_lines(path)[-1]
    assert "upstream_wait_ms" in e, "账本必须有 upstream_wait_ms"
    # 假后端 delay=0.3s 作用在整条响应上，首字节前后都会有时间，但都必须非负且不重叠
    assert e["upstream_wait_ms"] >= 0 and e["gen_ms"] >= 0
    assert e["queue_wait_ms"] + e["wake_ms"] + e["upstream_wait_ms"] + e["gen_ms"] <= e["total_ms"] + 20


async def test_segments_do_not_overlap(gw, tmp_path):
    """回归：queue_wait_ms 里不许包含 wake_ms。

    第一版把 acquire 总耗时当 queue_wait，导致唤醒时间被算进排队，
    两段数字几乎相等（实测 1209.791 vs 1209.772），分段归因彻底失效。
    """
    path = str(tmp_path / "requests.jsonl")
    async with gw(_cfg(path)) as g:
        r = await g.client.post("/v1/audio/speech", json={"model": A, "input": "hi"})
        assert r.status_code == 200
    e = _read_lines(path)[-1]
    assert e["wake_ms"] > 0, "冷启首条应有唤醒耗时"
    # 排队是零（无并发），不能因为换台被别人拖大
    assert e["queue_wait_ms"] < e["wake_ms"] / 2, (
        f"queue_wait={e['queue_wait_ms']} 疑似含了 wake={e['wake_ms']}"
    )
    # 三段之和不超过总耗时（允许毫秒级误差）
    assert e["queue_wait_ms"] + e["wake_ms"] + e["gen_ms"] <= e["total_ms"] + 5


async def test_upstream_queue_probe_scrapes_vllm(gw, tmp_path):
    """探针要真去抓上游 /metrics，并把排队深度写进 /metrics 与账本。

    这是 A-602「多少是等 max_num_seqs」唯一的硬数据来源：
    网关只能看见"首字节之前有多久"，看不见前面排了几个请求。
    """
    cfg = _cfg(str(tmp_path / "r.jsonl"))
    cfg["upstream_probe_interval_s"] = 0.05
    async with gw(cfg) as g:
        g.backend.metrics = (  # 假后端吐 vLLM 格式的指标
            "vllm_omni:num_requests_waiting{model_name=\"/models/TTS-A\"} 3.0\n"
            "vllm_omni:num_requests_running{model_name=\"/models/TTS-A\"} 2.0\n"
        )
        await g.client.post("/v1/audio/speech", json={"model": A, "input": "hi"})
        await asyncio.sleep(0.25)      # 等探针跑一两轮
        m = await g.client.get("/metrics")
        assert "omni_upstream_waiting" in m.text
        assert "omni_upstream_running" in m.text


async def test_ledger_records_failures_too(gw, tmp_path):
    """失败请求也要进账本：只统计成功请求的 p95 会系统性偏低。"""
    path = str(tmp_path / "requests.jsonl")
    async with gw(_cfg(path)) as g:
        r = await g.client.post("/v1/audio/speech", json={"input": "no model"})
        assert r.status_code == 400
    lines = _read_lines(path)
    assert lines, "400 也应有账本行"
    assert lines[-1]["status"] == 400
    assert lines[-1]["error"] == "missing_model"


async def test_ledger_disabled_means_no_file(gw, tmp_path):
    """关掉账本就不该产生文件（生产可用它彻底去掉这项 IO）。"""
    path = str(tmp_path / "off.jsonl")
    async with gw(_cfg(path, enabled=False)) as g:
        r = await g.client.post("/v1/audio/speech", json={"model": A, "input": "hi"})
        assert r.status_code == 200
        body = (await g.client.get("/admin/ledger")).json()
        assert body["enabled"] is False
    assert not os.path.exists(path), "账本关闭时不该写盘"
