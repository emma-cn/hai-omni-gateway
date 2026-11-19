"""配置层：默认值、缺省回退、能力/租约字段解析。"""
from __future__ import annotations

import pytest
import yaml

from omni_gateway.config import GatewayConfig


def write(tmp_path, raw):
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return GatewayConfig.load(p)


def test_empty_file_falls_back_to_defaults(tmp_path):
    cfg = write(tmp_path, {})
    assert cfg.listen == "0.0.0.0:8080"
    assert cfg.queue_limit == 200
    assert cfg.cards == {}
    assert cfg.models == {}


def test_example_config_parses(tmp_path):
    """仓里的示例配置必须能直接加载（它是文档，也是默认值来源）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cfg = GatewayConfig.load(root / "config" / "gateway.example.yaml")
    assert "card4" in cfg.cards
    # min_dwell 的合理区间是 3~5s（设 20s 时换台要硬等满，实测一次切换 22s）
    assert 0 < cfg.cards["card4"].min_dwell_s <= 10
    tts = cfg.models["/models/IndexTTS-2.5"]
    assert tts.capabilities == ["speech"]
    assert tts.stage_ids == [0, 1]
    assert tts.concurrency == 4
    assert tts.card == "card4"


def test_model_defaults(tmp_path):
    cfg = write(tmp_path, {
        "cards": {"card4": {}},
        "models": {"/models/X": {"base_url": "http://h:1", "card": "card4"}},
    })
    m = cfg.models["/models/X"]
    assert m.stage_ids == [0]
    assert m.sleep_level == 1
    assert m.health_path == "/health"
    assert m.wake_timeout_s == 60.0
    assert m.request_timeout_s == 120.0
    assert m.ttl_s == 300.0
    assert m.concurrency == 4
    assert m.capabilities == ["speech"]
    assert m.lease_s == 0.0


def test_base_url_trailing_slash_stripped(tmp_path):
    cfg = write(tmp_path, {
        "cards": {"card4": {}},
        "models": {"/models/X": {"base_url": "http://h:1/", "card": "card4"}},
    })
    assert cfg.models["/models/X"].base_url == "http://h:1"


def test_model_without_card_uses_first_card(tmp_path):
    cfg = write(tmp_path, {
        "cards": {"card7": {}},
        "models": {"/models/X": {"base_url": "http://h:1"}},
    })
    assert cfg.models["/models/X"].card == "card7"
    assert list(cfg.cards) == ["card7"]


def test_model_with_no_card_and_no_cards(tmp_path):
    cfg = write(tmp_path, {"models": {"/models/X": {"base_url": "http://h:1"}}})
    assert cfg.models["/models/X"].card == "default"
    assert "default" in cfg.cards


def test_unknown_card_is_auto_created(tmp_path):
    cfg = write(tmp_path, {
        "cards": {"card4": {}},
        "models": {"/models/X": {"base_url": "http://h:1", "card": "card9"}},
    })
    assert "card9" in cfg.cards
    assert cfg.cards["card9"].min_dwell_s == 20.0


def test_multi_capability_and_lease_parse(tmp_path):
    cfg = write(tmp_path, {
        "cards": {"card7": {}},
        "models": {"/models/H3": {
            "base_url": "http://h:1", "card": "card7",
            "capabilities": ["video", "image"], "lease_s": 1800, "ttl_s": 0,
            "stage_ids": [0, 1],
        }},
    })
    m = cfg.models["/models/H3"]
    assert m.capabilities == ["video", "image"]
    assert m.lease_s == 1800
    assert m.ttl_s == 0
    assert m.stage_ids == [0, 1]


def test_as_dict_is_serialisable(tmp_path):
    cfg = write(tmp_path, {
        "cards": {"card4": {}},
        "models": {"/models/X": {"base_url": "http://h:1", "card": "card4"}},
    })
    d = cfg.as_dict()
    assert d["models"]["/models/X"]["card"] == "card4"
    assert d["cards"]["card4"]["drain_timeout_s"] == 10.0


@pytest.mark.parametrize("bad", [{"models": {"/m": {"base_url": "http://h:1", "card": "c", "nope": 1}}}])
def test_unknown_model_field_rejected(tmp_path, bad):
    """未知字段必须炸，不能静默忽略（否则拼错的 lease_s 会静默失效）。"""
    with pytest.raises(TypeError):
        write(tmp_path, {"cards": {"c": {}}, **bad})
