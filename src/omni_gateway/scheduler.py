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


class SleepFailed(Exception):
    """旧模型睡不下去（OOM / 上游报错）。

    必须立刻中止换台：旧模型还占着显存，此时再去唤醒新模型 = 必然 OOM，
    而且会把整个后端 API server 打死（实测：进程退出，之后请求全部挂死）。
    """


class DrainTimeout(Exception):
    """换台前 drain 没能在 drain_timeout_s 内清零。

    上游（vllm-omni）的 sleep 语义是「暂停没完成 = 暂停失败，不许 sleep」，
    所以这里**不能**硬换台，只能拒绝本次切换：API 层转 503 + Retry-After，
    让客户端重试。硬换台的后果是在飞请求被掐掉、引擎状态与网关记账永久错位。
    """



class BackendUnavailable(Exception):
    """后端连不上 / 进程已死（ConnectError、连接被重置）。

    与"显存不够"必须分开：进程不在的时候重试唤醒**毫无意义**，
    只会把日志刷满、把客户端拖到超时。正确处置是快速 502 + 冷却期内不再尝试，
    等容器自愈（compose 的 restart: unless-stopped）后再放流量进来。
    """


class WakeOOM(Exception):
    """唤醒时显存不足（上游返回里带 CUDA OOM）。可重试，但要给 Retry-After。"""


class BadRequestRejected(Exception):
    """上游拒绝参数（4xx）。重试无用，也不应计入熔断。"""


class QueueFull(Exception):
    """排队人数超过 queue_limit。API 层转 429 + Retry-After。

    语义选择：宁可快速失败让客户端退避，也不要让它无限期挂着——
    无限排队会把"服务慢"伪装成"服务正常"，客户端 p99 直接失去意义。
    """


class RequestTimeout(Exception):
    """在队列里等到 request_timeout_s 仍没拿到执行名额。

    注意这**不是** 504 Gateway Timeout：请求根本没发到上游，是排队超时。
    API 层转 503 + Retry-After，并可被客户端安全重试（幂等键另算）。
    """


class Card:
    def __init__(
        self,
        cfg: CardConfig,
        models: dict[str, ModelConfig],
        client: OmniClient,
        queue_limit: int = 200,
    ) -> None:
        self.cfg = cfg
        self.models = models
        self.client = client
        self._lock = asyncio.Lock()
        self._inflight: dict[str, int] = {name: 0 for name in models}
        self._awake: str | None = None          # 当前醒着的模型
        self._last_switch = 0.0
        self._drained = asyncio.Event()
        self._drained.set()
        # ---- 有界 FIFO 队列（P0-2）----
        # 为什么不用 asyncio.Semaphore：信号量不保证公平（后到可能先拿），
        # 也不给"当前排了多少人"这个东西，queue_limit 就没法判。
        # 所以自己记顺序：每个模型一条 ticket 队列，队首才有资格拿名额。
        self._queue_limit = max(0, int(queue_limit))
        self._cond: dict[str, asyncio.Condition] = {n: asyncio.Condition() for n in models}
        self._waiting: dict[str, deque[int]] = {n: deque() for n in models}
        self._seq = 0
        # 用 None 而不是 0.0 当"从未失败"的哨兵：
        # time.monotonic() 的起点随环境而变（CI 容器里可能只有零点几秒），
        # 拿 0.0 比较会让 `monotonic() - 0.0 < 30` 恒真 → 空闲睡永远被跳过。
        self._last_idle_fail: float | None = None
        self._idle_fail_backoff_s = 30.0
        # 后端"进程级不可用"的冷却：连不上时不反复唤醒
        self._unavailable_until = 0.0
        self._unavailable_cooldown_s = 15.0
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
    async def acquire(self, model: str) -> tuple[float, float]:
        """确保 model 醒着并占一个在飞名额，返回排队等待毫秒数。

        失败语义（都由 API 层翻译成 HTTP）：
          QueueFull      → 429 queue_full（队列已满，别排队了，退避重试）
          RequestTimeout → 503 request_timeout（排太久，没轮到你）
          WakeFailed / SleepFailed / DrainTimeout → 503（见各自 docstring）

        全程事件驱动（Condition.wait），没有任何 sleep 轮询：
        忙等在 concurrency 打满时会让每个等待请求每秒空转 50 次，几百个请求就能把
        CPU 吃满，而且取消时还会漏掉唤醒。
        """
        m = self.models[model]
        cond = self._cond[model]
        t0 = time.monotonic()
        deadline = t0 + m.request_timeout_s
        self._seq += 1
        ticket = self._seq

        async with cond:
            self._waiting[model].append(ticket)
            try:
                while True:
                    at_head = bool(self._waiting[model]) and self._waiting[model][0] == ticket
                    if at_head and self._inflight[model] < m.concurrency:
                        self._waiting[model].popleft()
                        self._inflight[model] += 1
                        self._drained.clear()
                        break
                    # 队列上限：排在自己前面的（不含自己）已经到顶就别再排了
                    if len(self._waiting[model]) - 1 >= self._queue_limit:
                        self._waiting[model].remove(ticket)
                        cond.notify_all()
                        raise QueueFull(
                            f"{model}: 队列已满（queue_limit={self._queue_limit}，"
                            f"在飞 {self._inflight[model]}/{m.concurrency}）"
                        )
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        self._waiting[model].remove(ticket)
                        cond.notify_all()
                        raise RequestTimeout(
                            f"{model}: 排队 {m.request_timeout_s:.0f}s 仍未获得执行名额"
                        )
                    try:
                        await asyncio.wait_for(cond.wait(), timeout=remain)
                    except TimeoutError:
                        self._waiting[model].remove(ticket)
                        cond.notify_all()
                        raise RequestTimeout(
                            f"{model}: 排队 {m.request_timeout_s:.0f}s 仍未获得执行名额"
                        ) from None
            except BaseException:
                # 任何异常都要把名额还回去/唤醒后面的人（取消也一样）
                cond.notify_all()
                raise

        # 名额已占住，再确保模型醒着；醒不过来必须把名额退回，否则会永久泄漏
        #
        # ⚠ 分段口径：queue_wait_ms 在这里**截止**——只算"等名额"。
        # 第一版直接把 acquire 总耗时当 queue_wait，结果唤醒 1.2s 全被算进排队，
        # 两段数字一模一样，归因完全失效（实测 1209.791 vs 1209.772）。
        queue_wait_ms = (time.monotonic() - t0) * 1000.0
        wake_ms = 0.0
        try:
            async with self._lock:
                if (
                    self._awake != model
                    and time.monotonic() < self._unavailable_until
                ):
                    # 刚判定过后端不可用：冷却期内不再打它（重试唤醒叫不醒一个死进程）
                    raise BackendUnavailable(
                        f"{model}: 后端处于不可用冷却期（剩余 "
                        f"{self._unavailable_until - time.monotonic():.0f}s）"
                    )
                if self._awake != model:
                    t_switch = time.monotonic()
                    await self._switch_to(model)
                    wake_ms = (time.monotonic() - t_switch) * 1000.0
        except BaseException:
            await self._release_slot(model)
            raise
        # 返回两段耗时，供账本分别记账（A-602）：
        #   queue_wait_ms = 等执行名额（并发满/队首阻塞）
        #   wake_ms       = 换台 + 唤醒（显存搬运）
        # 合成一个数字的话，p95 抖动就无法归因到具体环节。
        return queue_wait_ms, wake_ms

    async def _release_slot(self, model: str) -> None:
        cond = self._cond[model]
        async with cond:
            self._inflight[model] = max(0, self._inflight.get(model, 0) - 1)
            if (
                self._inflight[model] == 0
                and self._awake is not None
                and self._inflight.get(self._awake, 0) == 0
            ):
                self._drained.set()
            cond.notify_all()

    async def release(self, model: str) -> None:
        await self._release_slot(model)

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
            if not ok:
                # 睡不下去就不能换台：旧模型仍占着显存，再唤醒新模型必然 OOM，
                # 而 OOM 会把整个后端进程打死（实测），代价远大于让客户端重试。
                self._event("fail", cur, f"sleep: {err}")
                raise SleepFailed(f"card {self.cfg.name}: 睡下 {cur} 失败：{err}")
            self._event("sleep", cur)

        self._event("switch", target, f"from={cur}")
        ok, err, kind = await self.client.wake(self.models[target])
        if not ok:
            # 醒不过来：把状态清掉，让下一次请求再试（不要留下"以为醒着"的假状态）
            self._awake = None
            self._event("fail", target, f"wake[{kind}]: {err}")
            if kind == "backend_unavailable":
                # 进程不在，重试唤醒没有意义 → 冷却期内直接快速失败，别打后端
                self._unavailable_until = time.monotonic() + self._unavailable_cooldown_s
                raise BackendUnavailable(f"{target}: 后端不可用（{err}）")
            if kind == "wake_oom":
                raise WakeOOM(f"{target}: 唤醒时显存不足（{err}）")
            if kind == "bad_request":
                raise BadRequestRejected(f"{target}: 上游拒绝参数（{err}）")
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
            # 退避：上一次空闲睡失败过就先别急着重试。
            # 实测教训：不加退避时每 5s 重试一次，把后端打成 ReadError 风暴，
            # 日志刷屏且掩盖真实原因（当时真因是容器没开 --enable-sleep-mode）。
            if (
                self._last_idle_fail is not None
                and time.monotonic() - self._last_idle_fail < self._idle_fail_backoff_s
            ):
                return
            log.info("card %s: %s 空闲超过 %.0fs，睡下", self.cfg.name, cur, m.ttl_s)
            ok, err = await self.client.sleep(m)
            if ok:
                self._awake = None
                self._last_idle_fail = None
                self._event("idle_sleep", cur, f"ttl={m.ttl_s:.0f}s")
            else:
                self._last_idle_fail = time.monotonic()
                log.warning(
                    "card %s: 空闲睡 %s 失败（%.0fs 内不再重试）：%s",
                    self.cfg.name, cur, self._idle_fail_backoff_s, err,
                )
                self._event("fail", cur, f"idle_sleep: {err}")

    # ------------------------------------------------------- 管理页手动操作
    async def force_wake(self, model: str) -> None:
        """手动唤醒（管理页按钮）。会按正常换台流程走：先睡掉同卡其它模型。"""
        async with self._lock:
            if self._awake == model:
                return
            await self._switch_to(model)

    async def force_sleep(self, model: str) -> None:
        """手动睡下（管理页按钮）。有在飞请求时拒绝，避免掐掉用户请求。

        注意：**不看本地状态**。网关认为"睡着"不代表后端真的放掉了显存
        （网关重启、外部直接调过上游、OOM 后进程重启都会造成失配），
        所以这里无条件向上游发一次 sleep（上游 sleep 是幂等的）。
        """
        async with self._lock:
            if self._awake != model and self._inflight.get(model, 0) == 0:
                # 状态可能失真：照样向上游补一发 sleep（幂等）。
                # 但**绝不能**动 self._awake —— 卡上醒着的是别的模型，
                # 把它抹成 None 等于让网关失忆：下一个请求会以为卡是空的、
                # 直接唤醒新模型 → 与仍在显存里的模型撞车（测试报的 #11）。
                ok, err = await self.client.sleep(self.models[model])
                self._event("reconcile_sleep" if ok else "fail", model,
                            "" if ok else f"reconcile_sleep: {err}")
                if not ok:
                    raise RuntimeError(f"对账睡眠失败: {err}")
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
            self.cards[card_name] = Card(card_cfg, models, client, queue_limit=cfg.queue_limit)
        self._task: asyncio.Task | None = None

    def card_of(self, model: str) -> Card:
        return self.cards[self.cfg.models[model].card]

    async def acquire(self, model: str) -> tuple[float, float]:
        """返回 (queue_wait_ms, wake_ms)，供账本分段记账。"""
        return await self.card_of(model).acquire(model)

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
        """启动对账：把网关状态与后端真实状态对齐。

        两种路径，差别很大：
          A) 后端支持 GET /v1/omni/status（打了 status 补丁）→ **只读**，不动任何模型。
             这是"免对账"：我们直接知道每个 stage 睡没睡，把 _awake 设对即可。
          B) 后端是老镜像（404）→ 回退到"盲睡一遍"。盲睡有代价：对其实醒着、
             还有在飞请求的模型发 sleep，等于把用户请求掐掉。所以在 A 可用时不该盲睡。

        为什么要对账：网关是这张卡的唯一调度者，但重启后它不知道后端谁还醒着。
        不对账就按需唤醒，会和"残留醒着的模型"抢显存 → 唤醒 OOM（实测 503）。
        """
        for card in self.cards.values():
            awake_seen: list[str] = []
            for name, m in card.models.items():
                st = await card.client.status(m)
                if st is not None:
                    asleep = bool(st.get("asleep"))
                    if not asleep:
                        awake_seen.append(name)
                    card._event("reconcile_status", name,
                                f"asleep={asleep} vram={ (st.get('vram') or {}).get('used_mb') }MB")
                    continue
                # 老后端：回退盲睡
                try:
                    ok, err = await card.client.sleep(m)
                    card._event("reconcile", name, "ok(blind)" if ok else f"fail: {err}")
                except Exception as e:  # 单个后端异常不能拖死启动
                    card._event("reconcile", name, f"error: {type(e).__name__}: {e}")

            if len(awake_seen) == 1:
                card._awake = awake_seen[0]
                card._event("reconcile_awake", awake_seen[0], "按后端状态接管")
                log.info("card %s: 对账发现 %s 醒着，直接接管（不盲睡）", card.cfg.name, awake_seen[0])
            elif len(awake_seen) > 1:
                # 同卡多个醒着 = 之前已经撞过显存，只保留状态不让它更乱，并报警
                card._awake = awake_seen[0]
                log.error("card %s: 对账发现 %d 个模型同时醒着 %s（显存可能已超卖）",
                          card.cfg.name, len(awake_seen), awake_seen)
                card._event("fail", awake_seen[0], f"多模型同时醒着: {awake_seen}")
            else:
                card._awake = None
            card._last_switch = 0.0

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
