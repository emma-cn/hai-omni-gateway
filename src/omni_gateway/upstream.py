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

    async def status(self, m: ModelConfig) -> dict[str, Any] | None:
        """读后端的权威睡眠状态（GET /v1/omni/status）。

        返回 None 表示后端不支持这个端点（老镜像）——调用方必须回退到
        "盲睡一遍"的旧对账方式，不能把 None 当成"已经睡着了"。
        """
        try:
            r = await self._c.get(
                f"{m.base_url}/v1/omni/status",
                params={"stage_ids": ",".join(str(s) for s in m.stage_ids)},
                timeout=10.0,
            )
        except Exception as e:
            log.info("status %s -> 不可达 %s", m.name, type(e).__name__)
            return None
        if r.status_code == 404:
            log.info("status %s -> 404（后端未打补丁）", m.name)
            return None
        if r.status_code != 200:
            log.info("status %s -> http=%s", m.name, r.status_code)
            return None
        try:
            return r.json()
        except Exception:
            return None

    async def sleep(self, m: ModelConfig) -> tuple[bool, str]:
        code, txt = await self._post(
            f"{m.base_url}/v1/omni/sleep",
            {"stage_ids": m.stage_ids, "level": m.sleep_level},
            timeout=180.0,
        )
        ok = code in (200, 201)
        log.info("sleep %s -> http=%s %s", m.name, code, "" if ok else txt)
        return ok, txt

    # 失败分类：三者处置完全不同，绝不能都叫 wake_failed
    #   backend_unavailable —— 连不上 / 进程死了（连接被拒、连接被重置）。
    #     重试唤醒毫无意义（进程不在，叫不醒），应当快速失败并等后端自愈。
    #   wake_oom —— 显存不够（上游返回里带 CUDA OOM）。可重试，但要带 Retry-After。
    #   bad_request —— 上游拒绝参数（4xx）。重试也没用，且不该计入熔断。
    KIND_OK = "ok"
    KIND_UNAVAILABLE = "backend_unavailable"
    KIND_OOM = "wake_oom"
    KIND_BAD_REQUEST = "bad_request"
    KIND_UNKNOWN = "wake_failed"

    @staticmethod
    def _looks_like_oom(text: str) -> bool:
        t = (text or "").lower()
        return ("out of memory" in t) or ("cuda error" in t) or ("nomem" in t)

    @classmethod
    def _classify(cls, code: int, txt: str) -> str:
        """把一次上游调用的结果归类。

        code=0 是 _post 的"没拿到 HTTP 响应"约定（连接失败/超时），
        它与"拿到了 4xx/5xx"性质完全不同：前者后端可能已经不在了。
        """
        if code == 0:
            return cls.KIND_UNAVAILABLE
        if 400 <= code < 500:
            return cls.KIND_BAD_REQUEST
        if cls._looks_like_oom(txt):
            return cls.KIND_OOM
        return cls.KIND_UNKNOWN

    async def wake(self, m: ModelConfig) -> tuple[bool, str, str]:
        """wakeup + resume + 等健康；整段受 wake_timeout_s 约束。

        返回 (ok, err, kind)。kind 见上面的四分类——调用方必须按 kind 决定
        是 502（后端不在）、503（显存不够，可重试）还是 400（参数不对）。
        """
        t0 = time.monotonic()
        deadline = t0 + m.wake_timeout_s

        code, txt = await self._post(
            f"{m.base_url}/v1/omni/wakeup", {"stage_ids": m.stage_ids}, timeout=min(120.0, m.wake_timeout_s)
        )
        if code not in (200, 201):
            kind = self._classify(code, txt)
            log.warning("wakeup %s failed kind=%s http=%s %s", m.name, kind, code, txt)
            return False, f"wakeup http={code} {txt}", kind

        code, txt = await self._post(
            f"{m.base_url}/v1/omni/resume", {"stage_ids": m.stage_ids}, timeout=min(60.0, m.wake_timeout_s)
        )
        if code not in (200, 201):
            kind = self._classify(code, txt)
            log.warning("resume %s failed kind=%s http=%s %s", m.name, kind, code, txt)
            return False, f"resume http={code} {txt}", kind

        while time.monotonic() < deadline:
            if await self.health(m):
                log.info("wake %s ok in %.2fs", m.name, time.monotonic() - t0)
                return True, "", self.KIND_OK
            await asyncio.sleep(0.2)
        log.warning("wake %s health timeout after %.1fs", m.name, m.wake_timeout_s)
        # 唤醒命令 200 了但健康检查一直不通：进程活着但引擎没起来，按显存/初始化失败处理（可重试）
        return False, "health check timeout", self.KIND_OOM
