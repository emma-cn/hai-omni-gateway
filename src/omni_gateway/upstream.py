"""vllm-omni 后端控制：睡眠 / 唤醒 / 健康检查。

实测（2026-11-19，单张 4090 开发机）：
  * 唤醒必须两段：POST /v1/omni/wakeup（权重换回显存）+ POST /v1/omni/resume
    （重开前端准入闸门）。少了 resume，请求会永久挂在 AsyncOmni.generate()
    的准入等待上——看着像"服务活着但不干活"。
  * 睡眠：POST /v1/omni/sleep {"stage_ids":[...], "level":1}
    level 1 权重备份到 CPU 内存，唤醒 1.3~1.9s；sleep 本身 1.8~2.8s。
  * vllm-omni **没有** vLLM 原生的 /sleep、/wake_up（实测 404），
    所以 llama-swap 自带的 vllm-wrapper 用不了。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .config import ModelConfig

log = logging.getLogger(__name__)


class OmniClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._c = client

    async def _post(self, url: str, payload: dict[str, Any], timeout: float) -> tuple[int, str]:
        try:
            r = await self._c.post(url, json=payload, timeout=timeout)
            return r.status_code, r.text[:300]
        except Exception as e:  # 连接失败等
            return 0, f"{type(e).__name__}: {e}"

    async def health(self, m: ModelConfig) -> bool:
        try:
            r = await self._c.get(m.base_url + m.health_path, timeout=3.0)
            return r.status_code == 200
        except Exception:
            return False

    async def sleep(self, m: ModelConfig) -> tuple[bool, str]:
        code, txt = await self._post(
            f"{m.base_url}/v1/omni/sleep",
            {"stage_ids": m.stage_ids, "level": m.sleep_level},
            timeout=180.0,
        )
        ok = code in (200, 201)
        log.info("sleep %s -> http=%s %s", m.name, code, "" if ok else txt)
        return ok, txt

    async def wake(self, m: ModelConfig) -> tuple[bool, str]:
        """wakeup + resume + 等健康；整段受 wake_timeout_s 约束。"""
        t0 = time.monotonic()
        deadline = t0 + m.wake_timeout_s

        code, txt = await self._post(
            f"{m.base_url}/v1/omni/wakeup", {"stage_ids": m.stage_ids}, timeout=min(120.0, m.wake_timeout_s)
        )
        if code not in (200, 201):
            log.warning("wakeup %s failed http=%s %s", m.name, code, txt)
            return False, f"wakeup http={code} {txt}"

        code, txt = await self._post(
            f"{m.base_url}/v1/omni/resume", {"stage_ids": m.stage_ids}, timeout=min(60.0, m.wake_timeout_s)
        )
        if code not in (200, 201):
            log.warning("resume %s failed http=%s %s", m.name, code, txt)
            return False, f"resume http={code} {txt}"

        while time.monotonic() < deadline:
            if await self.health(m):
                log.info("wake %s ok in %.2fs", m.name, time.monotonic() - t0)
                return True, ""
            await asyncio.sleep(0.2)
        log.warning("wake %s health timeout after %.1fs", m.name, m.wake_timeout_s)
        return False, "health check timeout"
