"""请求账本：每请求一行 JSONL，把排队 / 唤醒 / 生成三段耗时分开记。

为什么必须分开记（这是 A-602 的全部意义）：
    一条慢请求可能是三种完全不同的原因——
      排队慢  = 并发打满或队列上限压着（该调 concurrency / queue_limit）
      唤醒慢  = 换台 + 权重搬回显存（该看 min_dwell / 显存预算）
      生成慢  = 模型真的在算（该看 max_num_seqs / 文本长度）
    只记一个 `elapsed` 时，这三种混在一个数字里，p95 一抖就只能靠猜。

写盘策略：**不阻塞请求路径**。
    `record()` 只做一次 `put_nowait`，真正的写由后台 task 批量落盘。
    队列满时丢最旧的并记 `dropped`——账本自己绝不能变成新的故障点。

轮转：按大小轮转（`max_bytes`），保留 `keep` 个历史文件。盘满比丢日志更糟。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Any

log = logging.getLogger(__name__)


class Ledger:
    def __init__(
        self,
        path: str,
        *,
        max_bytes: int = 64 * 1024 * 1024,
        keep: int = 5,
        ring: int = 500,
        queue_size: int = 10000,
    ) -> None:
        self.path = path
        self.max_bytes = max(1, int(max_bytes))
        self.keep = max(1, int(keep))
        self._q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max(1, int(queue_size)))
        self._task: asyncio.Task | None = None
        self._dropped = 0
        self._written = 0
        self._disabled = False
        self.disabled_reason = ""
        self._recent: deque[dict[str, Any]] = deque(maxlen=max(1, int(ring)))

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        """尽力而为：目录建不出来就自我禁用，**绝不阻止服务启动**。

        账本是观测设施，不是关键路径。为了记日志而起不来服务，比丢日志更糟
        （生产上 `/var/log` 权限配错是常见事故，那时更需要服务活着）。
        """
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            # 探测一次可写性：只在启动时付这一次代价
            with open(self.path, "a", encoding="utf-8"):
                pass
        except Exception as e:
            self.disabled_reason = f"{type(e).__name__}: {e}"
            log.warning("账本禁用（%s），服务继续启动：%s", self.path, e)
            self._disabled = True
            return
        self._disabled = False
        self._task = asyncio.create_task(self._writer())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._drain_once()

    # ------------------------------------------------------------------ 记账
    def record(self, entry: dict[str, Any]) -> None:
        """非阻塞入队。请求路径上只调这一个方法。"""
        if getattr(self, "_disabled", False):
            return
        entry.setdefault("ts", time.time())
        self._recent.append(entry)
        try:
            self._q.put_nowait(entry)
        except asyncio.QueueFull:
            # 丢最旧的（队列里已是最新的一批），并记数——账本不许拖死服务
            try:
                self._q.get_nowait()
                self._q.put_nowait(entry)
            except Exception:
                pass
            self._dropped += 1

    # -------------------------------------------------------------- 后台 writer
    async def _writer(self) -> None:
        buf: list[str] = []
        last_flush = time.monotonic()
        while True:
            try:
                try:
                    item = await asyncio.wait_for(self._q.get(), timeout=1.0)
                    buf.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                except TimeoutError:
                    pass
                # 攒够 200 行或超过 1 秒就落盘：批量写省 fsync，又不至于丢太多
                if buf and (len(buf) >= 200 or time.monotonic() - last_flush >= 1.0):
                    self._flush(buf)
                    buf.clear()
                    last_flush = time.monotonic()
            except asyncio.CancelledError:
                if buf:
                    self._flush(buf)
                raise
            except Exception as e:  # 写盘失败不能打死 writer
                log.warning("账本写入失败：%s", e)
                await asyncio.sleep(1.0)

    async def _drain_once(self) -> None:
        buf: list[str] = []
        while not self._q.empty():
            try:
                buf.append(json.dumps(self._q.get_nowait(), ensure_ascii=False, separators=(",", ":")))
            except Exception:
                break
        if buf:
            self._flush(buf)

    def _flush(self, lines: list[str]) -> None:
        try:
            self._rotate_if_needed()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            self._written += len(lines)
        except Exception as e:
            log.warning("账本落盘失败（丢弃 %d 行）：%s", len(lines), e)

    def _rotate_if_needed(self) -> None:
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) >= self.max_bytes:
                for i in range(self.keep - 1, 0, -1):
                    src = f"{self.path}.{i}"
                    if os.path.exists(src):
                        os.replace(src, f"{self.path}.{i + 1}")
                os.replace(self.path, f"{self.path}.1")
        except Exception as e:
            log.warning("账本轮转失败：%s", e)

    # ------------------------------------------------------------------ 观测
    def recent(self, n: int = 100) -> list[dict[str, Any]]:
        return list(self._recent)[-max(1, int(n)):]

    def stats(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "disabled": self._disabled,
            "disabled_reason": self.disabled_reason,
            "written": self._written,
            "dropped": self._dropped,
            "queued": self._q.qsize(),
            "ring": len(self._recent),
        }


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return round(s[idx], 3)


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """把账本聚合成"三段耗时分布"，这是 p95 归因的最小数据集。"""

    def col(name: str) -> list[float]:
        out = []
        for e in entries:
            v = e.get(name)
            if isinstance(v, (int, float)):
                out.append(float(v))
        return out

    out: dict[str, Any] = {"count": len(entries)}
    for seg in ("queue_wait_ms", "wake_ms", "upstream_wait_ms", "gen_ms", "total_ms"):
        vals = col(seg)
        out[seg] = {
            "p50": _pct(vals, 50),
            "p95": _pct(vals, 95),
            "max": round(max(vals), 3) if vals else None,
        }
    # 换台次数分布：换台多的请求慢，是"卡被打成筛子"的直接证据
    switches = [len(e.get("switches") or []) for e in entries]
    out["switches"] = {"avg": round(sum(switches) / len(switches), 2) if switches else 0,
                       "max": max(switches) if switches else 0}
    return out
