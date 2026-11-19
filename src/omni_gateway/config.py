"""配置：一张卡一组模型，模型指向一个 vllm-omni 实例。

配置格式见 config/gateway.example.yaml。设计原则：
  * 卡（card）是调度的最小单位：同一张卡上同一时刻只允许一个模型"醒着"
  * 模型用 vllm-omni 真实的服务名做 ID（客户端发的 model 字段原样透传，
    不改写请求体——llama-swap 的 model 字段是受保护字段，我们自己写也不改，
    省掉一类隐蔽 bug）
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ModelConfig:
    name: str                      # 同时也是客户端请求里的 model 值
    base_url: str                  # 例：http://127.0.0.1:60589
    card: str                      # 所属卡，例：card4
    stage_ids: list[int] = field(default_factory=lambda: [0])
    sleep_level: int = 1           # 1=权重备份到 CPU 内存（快醒）；2=丢弃（省内存）
    health_path: str = "/health"
    wake_timeout_s: float = 60.0   # 唤醒 + resume + 健康检查的总预算
    request_timeout_s: float = 120.0
    ttl_s: float = 300.0           # 空闲多久自动睡；0=永不
    concurrency: int = 4           # 允许同时在飞的请求数（对齐 vllm max_num_seqs）
    # 该模型能接哪些接口：speech / transcription / image / video
    capabilities: list[str] = field(default_factory=lambda: ["speech"])
    # 长任务的"租约"：>0 表示该模型一旦有任务在跑，至少锁住卡这么久不许被换走
    # （视频生成一次几分钟，绝不能中途被挤下去）
    lease_s: float = 0.0

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")


@dataclass
class CardConfig:
    name: str
    min_dwell_s: float = 20.0      # 一个模型醒着至少驻留多久才允许被挤掉（防抖）
    drain_timeout_s: float = 10.0  # 换台前等在途请求清零的上限


@dataclass
class KeyConfig:
    """一个调用方 key 的能力边界与配额。

    只放**真的会被读到**的字段（AGENTS 规则 1：配置项不许空挂）。
    按字符/按音频秒的日额度需要账本（P0-6）才能算准，等账本落地再加，
    现在加了也只能是摆设。
    """

    name: str
    key: str = ""                  # 值本身；更推荐用 key_env 从环境变量读
    key_env: str = ""              # 环境变量名，非空时优先于 key
    capabilities: list[str] = field(default_factory=list)   # 空 = 不限
    models: list[str] = field(default_factory=list)         # 空 = 不限
    rpm: int = 0                   # 每分钟请求数上限，0 = 不限
    expires_at: str = ""           # ISO 日期，空 = 不过期

    def resolved_key(self) -> str:
        import os

        if self.key_env:
            return os.environ.get(self.key_env, "").strip()
        return self.key.strip()

    def is_expired(self, now_ts: float | None = None) -> bool:
        if not self.expires_at:
            return False
        import time as _t

        now_ts = _t.time() if now_ts is None else now_ts
        try:
            import datetime as _dt

            return _dt.date.fromisoformat(self.expires_at).toordinal() < _dt.date.fromtimestamp(now_ts).toordinal()
        except ValueError:
            return False


@dataclass
class AuthConfig:
    admin_token: str = ""
    admin_token_env: str = "OMNI_ADMIN_TOKEN"
    # loopback 免 key：本地调试方便。注意反代场景要靠 trusted_proxies 兜住，
    # 否则所有请求的 peer 都是 127.0.0.1，等于全网免 key。
    loopback_bypass: bool = True
    # 运维面是否也享受 loopback 免 token。**默认 false**：
    # 本机的其它进程、SSH 隧道、本地浏览器插件都能发起 loopback 请求，
    # 一个匿名 GET /admin/sleep 就能把模型睡掉，代价太大。
    # 本地调试想省事就把这个打开（只在 dev 环境）。
    admin_loopback_bypass: bool = False
    # **默认空**：不信任任何 X-Forwarded-For。只有你确实在前面放了反代（且它会
    # 覆盖/追加 XFF）时，才把反代地址填进来。默认填 127.0.0.1 等于让本机任何进程
    # 冒充任意客户端（issue #12）。
    trusted_proxies: list[str] = field(default_factory=list)
    # 哪些 peer 地址算"本机"。测试环境里 ASGI transport 的 peer 是 "testclient"，
    # 所以这里做成可配置而不是硬编码——但默认值只含真 loopback。
    loopback_peers: list[str] = field(default_factory=lambda: ["127.0.0.1", "::1", "localhost"])
    keys: list[KeyConfig] = field(default_factory=list)

    @property
    def keys_by_value(self) -> dict[str, KeyConfig]:
        out: dict[str, KeyConfig] = {}
        for k in self.keys:
            v = k.resolved_key()
            if v:
                out[v] = k
        return out

    def resolved_admin_token(self) -> str:
        import os

        return self.admin_token.strip() or os.environ.get(self.admin_token_env, "").strip()


@dataclass
class GatewayConfig:
    listen: str = "0.0.0.0:8080"
    cards: dict[str, CardConfig] = field(default_factory=dict)
    models: dict[str, ModelConfig] = field(default_factory=dict)
    queue_limit: int = 200         # 每个模型排队上限，超了直接 429
    auth: AuthConfig = field(default_factory=AuthConfig)

    @staticmethod
    def load(path: str | Path) -> GatewayConfig:
        raw: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}
        cards = {
            name: CardConfig(name=name, **{k: v for k, v in (body or {}).items() if k != "name"})
            for name, body in (raw.get("cards") or {}).items()
        }
        models: dict[str, ModelConfig] = {}
        for name, body in (raw.get("models") or {}).items():
            body = dict(body or {})
            body.setdefault("card", next(iter(cards), "default"))
            models[name] = ModelConfig(name=name, **body)
        for m in models.values():
            if m.card not in cards:
                cards[m.card] = CardConfig(name=m.card)
        auth_raw = raw.get("auth") or {}
        keys = [
            KeyConfig(name=k.get("name") or f"key{i}", **{kk: vv for kk, vv in k.items() if kk != "name"})
            for i, k in enumerate(auth_raw.get("keys") or [])
        ]
        auth = AuthConfig(
            admin_token=auth_raw.get("admin_token", ""),
            admin_token_env=auth_raw.get("admin_token_env", "OMNI_ADMIN_TOKEN"),
            loopback_bypass=bool(auth_raw.get("loopback_bypass", True)),
            admin_loopback_bypass=bool(auth_raw.get("admin_loopback_bypass", False)),
            trusted_proxies=list(auth_raw.get("trusted_proxies") or []),
            loopback_peers=list(auth_raw.get("loopback_peers") or ["127.0.0.1", "::1", "localhost"]),
            keys=keys,
        )
        return GatewayConfig(
            listen=raw.get("listen", "0.0.0.0:8080"),
            cards=cards,
            models=models,
            queue_limit=int(raw.get("queue_limit", 200)),
            auth=auth,
        )

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
