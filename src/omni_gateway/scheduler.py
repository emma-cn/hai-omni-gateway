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

from .config import CardConfig, GatewayConfig, ModelConfig
from .upstream import OmniClient

log = logging.getLogger(__name__)


class WakeFailed(Exception):
    """唤醒失败（OOM / 健康检查超时）。API 层应转成 503 + Retry-After。"""


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

            # drain：等在途清零（超时后才放弃，绝不 abort 用户请求）
            deadline = time.monotonic() + self.cfg.drain_timeout_s
            while self._inflight.get(cur, 0) > 0 and time.monotonic() < deadline:
                self._lock.release()
                try:
                    await asyncio.sleep(0.05)
                finally:
                    await self._lock.acquire()
            if self._inflight.get(cur, 0) > 0:
                log.warning("card %s: drain 超时，仍有 %d 条在飞，继续换台", self.cfg.name, self._inflight[cur])
            ok, err = await self.client.sleep(self.models[cur])
            if not ok:
                log.warning("card %s: 睡 %s 失败：%s", self.cfg.name, cur, err)

        log.info("card %s: 换台 %s -> %s", self.cfg.name, cur, target)
        ok, err = await self.client.wake(self.models[target])
        if not ok:
            # 醒不过来：把状态清掉，让下一次请求再试（不要留下"以为醒着"的假状态）
            self._awake = None
            raise WakeFailed(f"{target}: {err}")
        self._awake = target
        self._last_switch = time.monotonic()

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
            else:
                log.warning("card %s: 空闲睡 %s 失败：%s", self.cfg.name, cur, err)


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

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
