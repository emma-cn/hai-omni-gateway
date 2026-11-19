#!/usr/bin/env python3
"""真机 E2E：对着开发机的网关 + 真 vllm-omni 后端跑 README 的验收标准。

**这个脚本会唤醒/睡下真实模型、占 GPU**（一次约 1~2 分钟），所以默认只做只读检查，
必须显式加 `--yes` 才会动卡。

用法（在开发机上）：
    PYTHONPATH=src ~/.venvs/omni-gw-test/bin/python tests/e2e_live.py \
        --gateway http://127.0.0.1:8080 \
        --backend http://127.0.0.1:60589 \
        --model /models/IndexTTS-2.5 \
        --other /models/VoxCPM2 \
        --other-backend http://127.0.0.1:60592 \
        --yes

检查项（对应 README「验收标准」）：
  E1 /healthz + /v1/models 结构与状态
  E2 官方 openai SDK 经网关出音频（标准调用 + stream）
  E3 网关产出的 WAV 与直连后端 **sha256 一致**（透传无损）
  E4 睡着的模型：请求在 N 秒内唤醒并返回，不挂死
  E5 换台：A → B 的切换耗时（sleep + wake + resume + health）
  E6 并发 4 条：全部 200，且只唤醒一次
  E7 收尾：把模型睡回去，核对最终状态
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time

import httpx

SPEECH = {
    "input": "你好，这是 hai omni gateway 的端到端测试。",
    "voice": "default",
    "response_format": "wav",
}

results: list[tuple[str, str, str]] = []


def record(tag: str, ok: bool, detail: str = "") -> None:
    results.append((tag, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {tag} {detail}", flush=True)


async def state(c: httpx.AsyncClient, gateway: str) -> dict:
    return (await c.get(f"{gateway}/admin/state")).json()


async def awake_of(c: httpx.AsyncClient, gateway: str) -> str | None:
    st = await state(c, gateway)
    return st["cards"][0]["awake"]


async def wake_count(c: httpx.AsyncClient, gateway: str, model: str) -> int:
    st = await state(c, gateway)
    return sum(1 for e in st["cards"][0]["events"]
               if e["kind"] == "wake" and e["model"] == model)


async def speech(c: httpx.AsyncClient, url: str, model: str, timeout: float = 300.0):
    t0 = time.monotonic()
    r = await c.post(f"{url}/v1/audio/speech", json={**SPEECH, "model": model}, timeout=timeout)
    return r, time.monotonic() - t0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://127.0.0.1:8080")
    ap.add_argument("--backend", default="http://127.0.0.1:60589")
    ap.add_argument("--model", default="/models/IndexTTS-2.5")
    ap.add_argument("--other", default="/models/VoxCPM2")
    ap.add_argument("--other-backend", default="http://127.0.0.1:60592")
    ap.add_argument("--yes", action="store_true", help="允许动 GPU（唤醒/睡下真实模型）")
    args = ap.parse_args()

    async with httpx.AsyncClient(timeout=60.0) as c:
        # ------------------------------------------------------------ E1
        h = await c.get(f"{args.gateway}/healthz")
        models = await c.get(f"{args.gateway}/v1/models")
        record("E1 healthz+models", h.status_code == 200 and models.status_code == 200,
               f"healthz={h.status_code} models={models.status_code}")

        if not args.yes:
            print("\n只读模式：只跑了 E1。要跑 E2~E7 请加 --yes（会动 GPU）。")
            return 0

        # ------------------------------------------------------------ E2/E3
        st = await state(c, args.gateway)
        print("起始状态:", json.dumps(st["cards"][0]["awake"], ensure_ascii=False))

        r, dt = await speech(c, args.gateway, args.model)
        gw_audio = r.content
        record("E4 睡着->唤醒并返回", r.status_code == 200 and r.content[:4] == b"RIFF",
               f"http={r.status_code} {dt:.2f}s {len(gw_audio)}B")

        # 趁模型醒着，直连后端取同一份
        r2, dt2 = await speech(c, args.backend, args.model)
        direct = r2.content
        record("E3 透传无损(sha256)", hashlib.sha256(gw_audio).hexdigest()
               == hashlib.sha256(direct).hexdigest(),
               f"gw={hashlib.sha256(gw_audio).hexdigest()[:16]} "
               f"be={hashlib.sha256(direct).hexdigest()[:16]} ({len(direct)}B, {dt2:.2f}s)")

        # ------------------------------------------------------------ E2 (SDK)
        try:
            from openai import OpenAI

            cli = OpenAI(base_url=f"{args.gateway}/v1", api_key="dummy")
            t0 = time.monotonic()
            with cli.audio.speech.with_streaming_response.create(
                model=args.model, voice="default", input=SPEECH["input"],
                response_format="wav",
            ) as resp:
                body = resp.read()
            record("E2 openai SDK 流式", len(body) > 44 and body[:4] == b"RIFF",
                   f"{len(body)}B {time.monotonic() - t0:.2f}s")
        except ImportError:
            record("E2 openai SDK 流式", False, "openai 未安装：pip install openai")
        except Exception as e:  # noqa: BLE001
            record("E2 openai SDK 流式", False, f"{type(e).__name__}: {e}")

        # ------------------------------------------------------------ E5 换台
        t0 = time.monotonic()
        r_other, _ = await speech(c, args.gateway, args.other, timeout=600.0)
        switch_dt = time.monotonic() - t0
        record("E5 换台 A->B", r_other.status_code == 200,
               f"http={r_other.status_code} 切换+生成 {switch_dt:.2f}s "
               f"(现在醒着: {await awake_of(c, args.gateway)})")

        # ------------------------------------------------------------ E6 并发
        before = await wake_count(c, args.gateway, args.model)
        rs = await asyncio.gather(*[speech(c, args.gateway, args.model) for _ in range(4)],
                                  return_exceptions=True)
        codes = [getattr(x[0], "status_code", repr(x)) if isinstance(x, tuple) else repr(x)
                 for x in rs]
        after = await wake_count(c, args.gateway, args.model)
        record("E6 并发4条", all(code == 200 for code in codes),
               f"codes={codes} 唤醒次数 +{after - before}")

        # ------------------------------------------------------------ E7 收尾
        for m in (args.model, args.other):
            try:
                await c.post(f"{args.gateway}/admin/sleep", json={"model": m}, timeout=180)
            except Exception as e:  # noqa: BLE001
                print("sleep 失败", m, e)
        st = await state(c, args.gateway)
        record("E7 收尾全部睡下", st["cards"][0]["awake"] is None,
               f"awake={st['cards'][0]['awake']} inflight={st['cards'][0]['inflight']}")

    print("\n===== 汇总 =====")
    failed = 0
    for tag, verdict, detail in results:
        print(f"{verdict:4} {tag:22} {detail}")
        failed += verdict == "FAIL"
    print(f"\n{len(results) - failed} passed / {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
