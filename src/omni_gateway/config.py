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

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")


@dataclass
class CardConfig:
    name: str
    min_dwell_s: float = 20.0      # 一个模型醒着至少驻留多久才允许被挤掉（防抖）
    drain_timeout_s: float = 10.0  # 换台前等在途请求清零的上限


@dataclass
class GatewayConfig:
    listen: str = "0.0.0.0:8080"
    cards: dict[str, CardConfig] = field(default_factory=dict)
    models: dict[str, ModelConfig] = field(default_factory=dict)
    queue_limit: int = 200         # 每个模型排队上限，超了直接 429

    @staticmethod
    def load(path: str | Path) -> "GatewayConfig":
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
        return GatewayConfig(
            listen=raw.get("listen", "0.0.0.0:8080"),
            cards=cards,
            models=models,
            queue_limit=int(raw.get("queue_limit", 200)),
        )

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
