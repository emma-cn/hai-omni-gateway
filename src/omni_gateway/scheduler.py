"""卡级调度：单写者状态机（一张卡一个锁，同一时刻只有它能动 sleep/wake）。

为什么要有这一层：
  vllm-omni 一个容器 = 一条 pipeline，多个模型不能同容器；而一张 24GB 卡
  又装不下两个醒着的大模型（实测 indextts 14.9 GiB / voxcpm2 17.3 GiB）。
  所以"共用一张卡"必然是时间片：切之前先把旧的睡下，再把新的唤醒。

  llama-swap 也想做这件事，但它把"模型状态"绑在 cmd 进程的生死上，
  而 vllm-omni 的 sleep 是"进程常驻、只让出显存"，两者范式冲突，
  实测换台会在 stop 阶段挂满客户端超时。所以这里自己写。

关键规则：
  1. 换台前必须等在途请求清零（drain），不能 abort 掉用户请求
  2. min_dwell：一个模型醒着至少驻留 N 秒，防止 A/B 交替把卡打成筛子
  3. 状态只由本模块改；API 层只能通过 acquire/release 间接驱动
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

from .config import CardConfig, GatewayConfig, ModelConfig
from .upstream import OmniClient

log = logging.getLogger(__name__)


class WakeFailed(Exception):
    """唤醒失败（OOM / 健康检查超时）。API 层应转成 503 + Retry-After。"""


class DrainTimeout(Exception):
    """换台前 drain 没能在 drain_timeout_s 内清零。

    上游（vllm-omni）的 sleep 语义是「暂停没完成 = 暂停失败，不许 sleep」，
    所以这里**不能**硬换台，只能拒绝本次切换：API 层转 503 + Retry-After，
    让客户端重试。硬换台的后果是在飞请求被掐掉、引擎状态与网关记账永久错位。
    """



class Card:
    def __init__(self, cfg: CardConfig, models: dict[str, ModelConfig], client: OmniClient) -> None:
        self.cfg = cfg
        self.models = models
        self.client = client
        self._lock = asyncio.Lock()
        self._inflight: dict[str, int] = {name: 0 for name in models}
        self._awake: str | None = None          # 当前醒着的模型
        self._last_switch = 0.0
        self._drained = asyncio.Event()
        self._drained.set()
        # 事件环：给管理页展示"谁什么时候被唤醒/睡下/失败"
        self.events: deque[dict[str, Any]] = deque(maxlen=200)

    def _event(self, kind: str, model: str | None, detail: str = "") -> None:
        self.events.appendleft({
            "ts": time.time(),
            "card": self.cfg.name,
            "kind": kind,          # wake | sleep | switch | fail | idle_sleep
            "model": model,
            "detail": detail,
        })
        log.info("card %s: %s %s %s", self.cfg.name, kind, model or "", detail)

    def stats(self) -> dict[str, Any]:
        return {
            "card": self.cfg.name,
            "awake": self._awake,
            "inflight": dict(self._inflight),
            "last_switch": self._last_switch,
            "min_dwell_s": self.cfg.min_dwell_s,
            "events": list(self.events)[:50],
        }

    # ---------------------------------------------------------------- 状态
    def state(self, model: str) -> str:
        return "awake" if self._awake == model else "asleep"

    @property
    def awake_model(self) -> str | None:
        return self._awake

    def inflight(self, model: str) -> int:
        return self._inflight.get(model, 0)

    # ---------------------------------------------------------------- 核心
    async def acquire(self, model: str) -> None:
        """确保 model 醒着，并占一个在飞名额。失败抛 WakeFailed。"""
        m = self.models[model]
        async with self._lock:
            # 并发上限（对齐 vllm 的 max_num_seqs）
            if self._inflight[model] >= m.concurrency:
                while self._inflight[model] >= m.concurrency and self._awake == model:
                    self._lock.release()
                    try:
                        await asyncio.sleep(0.02)
                    finally:
                        await self._lock.acquire()

            if self._awake != model:
                await self._switch_to(model)
            self._inflight[model] += 1
            self._drained.clear()

    async def release(self, model: str) -> None:
        async with self._lock:
            self._inflight[model] = max(0, self._inflight.get(model, 0) - 1)
            if self._inflight[model] == 0 and self._awake is not None and self._inflight.get(self._awake, 0) == 0:
                self._drained.set()

    async def _switch_to(self, target: str) -> None:
        cur = self._awake
        if cur is not None:
            # min_dwell：刚切过来的模型别马上被挤走
            wait = self.cfg.min_dwell_s - (time.monotonic() - self._last_switch)
            if wait > 0:
                log.debug("card %s: min_dwell 还差 %.1fs", self.cfg.name, wait)
                await asyncio.sleep(wait)

            # drain：必须等在途清零。上游规则（vllm-omni sleep 的语义）是
            # 「暂停没完成 = 暂停失败，不许 sleep」——超时不能硬换台，
            # 否则会把用户请求连同引擎状态一起掐掉，且事后无法复原。
            # 正确做法：拒绝本次切换，让客户端拿 503 + Retry-After 重试。
            deadline = time.monotonic() + self.cfg.drain_timeout_s
            while self._inflight.get(cur, 0) > 0 and time.monotonic() < deadline:
                self._lock.release()
                try:
                    await asyncio.sleep(0.05)
                finally:
                    await self._lock.acquire()
            if self._inflight.get(cur, 0) > 0:
                n = self._inflight[cur]
                self._event("fail", cur, f"drain 超时（{n} 条在飞），拒绝换台")
                raise DrainTimeout(
                    f"card {self.cfg.name}: {cur} 仍有 {n} 条在飞，"
                    f"{self.cfg.drain_timeout_s:.0f}s 内未清零；按上游规则不得 sleep，拒绝换台"
                )
            ok, err = await self.client.sleep(self.models[cur])
            if ok:
                self._event("sleep", cur)
            else:
                self._event("fail", cur, f"sleep: {err}")

        self._event("switch", target, f"from={cur}")
        ok, err = await self.client.wake(self.models[target])
        if not ok:
            # 醒不过来：把状态清掉，让下一次请求再试（不要留下"以为醒着"的假状态）
            self._awake = None
            self._event("fail", target, f"wake: {err}")
            raise WakeFailed(f"{target}: {err}")
        self._awake = target
        self._last_switch = time.monotonic()
        self._event("wake", target)

    async def sleep_if_idle(self) -> None:
        """空闲回收：ttl 到期就把模型睡下，把显存还回去。"""
        async with self._lock:
            cur = self._awake
            if cur is None:
                return
            m = self.models[cur]
            if m.ttl_s <= 0:
                return
            if self._inflight.get(cur, 0) > 0:
                return
            if time.monotonic() - self._last_switch < m.ttl_s:
                return
            log.info("card %s: %s 空闲超过 %.0fs，睡下", self.cfg.name, cur, m.ttl_s)
            ok, err = await self.client.sleep(m)
            if ok:
                self._awake = None
                self._event("idle_sleep", cur, f"ttl={m.ttl_s:.0f}s")
            else:
                log.warning("card %s: 空闲睡 %s 失败：%s", self.cfg.name, cur, err)
                self._event("fail", cur, f"idle_sleep: {err}")

    # ------------------------------------------------------- 管理页手动操作
    async def force_wake(self, model: str) -> None:
        """手动唤醒（管理页按钮）。会按正常换台流程走：先睡掉同卡其它模型。"""
        async with self._lock:
            if self._awake == model:
                return
            await self._switch_to(model)

    async def force_sleep(self, model: str) -> None:
        """手动睡下（管理页按钮）。有在飞请求时拒绝，避免掐掉用户请求。"""
        async with self._lock:
            if self._awake != model:
                return
            if self._inflight.get(model, 0) > 0:
                raise RuntimeError(f"{model} 还有 {self._inflight[model]} 条请求在飞，拒绝睡眠")
            ok, err = await self.client.sleep(self.models[model])
            if not ok:
                self._event("fail", model, f"manual_sleep: {err}")
                raise RuntimeError(f"睡眠失败: {err}")
            self._awake = None
            self._event("sleep", model, "manual")


class Scheduler:
    def __init__(self, cfg: GatewayConfig, client: OmniClient) -> None:
        self.cfg = cfg
        self.cards: dict[str, Card] = {}
        for card_name, card_cfg in cfg.cards.items():
            models = {n: m for n, m in cfg.models.items() if m.card == card_name}
            self.cards[card_name] = Card(card_cfg, models, client)
        self._task: asyncio.Task | None = None

    def card_of(self, model: str) -> Card:
        return self.cards[self.cfg.models[model].card]

    async def acquire(self, model: str) -> None:
        await self.card_of(model).acquire(model)

    async def release(self, model: str) -> None:
        await self.card_of(model).release(model)

    async def reaper_loop(self, interval: float = 5.0) -> None:
        while True:
            await asyncio.sleep(interval)
            for card in self.cards.values():
                try:
                    await card.sleep_if_idle()
                except Exception:
                    log.exception("reaper: card %s", card.cfg.name)

    def start(self) -> None:
        self._task = asyncio.create_task(self.reaper_loop())

    async def reconcile(self) -> None:
        """启动对账：把每个模型都睡一遍，把状态归零。

        为什么必须做：网关是这张卡的唯一调度者，但重启后它不知道后端谁还醒着
        （vllm-omni 的 is_sleeping 只是本地记账，不可信）。如果不归零就按需唤醒，
        会和"残留醒着的模型"抢显存 → 唤醒 OOM（实测 503）。
        sleep 对已睡的服务是幂等的（实测 4ms），对醒着的约 2.8s，代价可接受。
        """
        for card in self.cards.values():
            for name, m in card.models.items():
                try:
                    ok, err = await card.client.sleep(m)
                    card._event("reconcile", name, "ok" if ok else f"fail: {err}")
                except Exception as e:  # 单个后端异常不能拖死启动
                    card._event("reconcile", name, f"error: {type(e).__name__}: {e}")
            card._awake = None
            card._last_switch = 0.0

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
