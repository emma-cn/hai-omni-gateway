"""P0-1 鉴权与多租户：A-101 ~ A-104。

这些用例对应产品侧验收矩阵里的同名 ID，但**验收真源仍是产品侧 accept.sh**；
这里的作用是让"鉴权语义"在 CI 里就有红灯，而不是等黑盒验收才发现。

关于 peer：ASGI transport 的 client 是 ("testclient", 50000)，
所以 base_cfg 把 "testclient" 列进 loopback_peers（等价于"本机"）。
要模拟"外部客户端"，就把 loopback_peers 收窄到真 loopback。
"""
from __future__ import annotations

import pytest

from conftest import base_cfg

A = "/models/TTS-A"

EXTERNAL_HOST = "203.0.113.9"      # RFC 5737 文档段，代表"外网客户端"
# 注意：真正的开关是 gw(..., client_host=...)；loopback_peers 保持默认即可
EXTERNAL = {}

# 显式标记：不依赖 asyncio_mode=auto（该配置项本仓没有，靠插件默认值会随版本漂）
pytestmark = pytest.mark.asyncio


def external_cfg(**auth_extra) -> dict:
    """构造"外部客户端视角"的配置：listen 非 loopback + 必须有 key 和 admin token。

    fail-closed 的要求是**两者都要有**（少一个就拒绝启动），所以这里默认都备齐，
    单个用例需要"缺什么"时用 auth_extra 覆盖掉即可。
    """
    raw = base_cfg()
    raw["listen"] = "0.0.0.0:8080"
    auth = dict(raw["auth"], **EXTERNAL)
    auth.setdefault("admin_token", "adm-1")
    auth.setdefault("keys", [{"name": "t", "key": "sk-test-1"}])
    auth.update(auth_extra)
    raw["auth"] = auth
    return raw


async def test_a101_external_request_without_key_is_401(gw):
    """A-101：非 loopback 无 key 访问 /v1/models → 401。"""
    raw = external_cfg(keys=[{"name": "t", "key": "sk-test-1"}])
    raw["listen"] = "0.0.0.0:8080"
    async with gw(raw, client_host=EXTERNAL_HOST) as g:
        r = await g.client.get("/v1/models")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"


async def test_a101_external_request_with_valid_key_passes(gw):
    """配了 key 之后，正确的 key 必须放行（否则鉴权就是把服务关死）。"""
    raw = external_cfg(keys=[{"name": "t", "key": "sk-test-1", "rpm": 100}])
    raw["listen"] = "0.0.0.0:8080"
    async with gw(raw, client_host=EXTERNAL_HOST) as g:
        r = await g.client.get("/v1/models", headers={"authorization": "Bearer sk-test-1"})
        assert r.status_code == 200
        # x-api-key 写法等价
        r2 = await g.client.get("/v1/models", headers={"x-api-key": "sk-test-1"})
        assert r2.status_code == 200


async def test_admin_surface_requires_token_even_on_loopback(gw):
    """运维面默认**不信任 loopback**：本机其它进程/SSH 隧道都能发 loopback 请求。

    这也是 A-102/A-103 的判据：验收脚本是从 127.0.0.1 打的。
    """
    strict = base_cfg()
    strict["auth"] = dict(strict["auth"], admin_loopback_bypass=False)   # 生产默认
    async with gw(strict) as g:              # 且没有 admin_token
        r = await g.client.get("/admin/state")
        assert r.status_code == 401
    raw = base_cfg()
    raw["auth"] = dict(raw["auth"], admin_token="adm-1", admin_loopback_bypass=False)
    async with gw(raw) as g:
        assert (await g.client.get("/admin/state")).status_code == 401
        assert (await g.client.get("/admin/state",
                                   headers={"authorization": "Bearer adm-1"})).status_code == 200
        # 浏览器场景：/ui 可以用 ?token=
        assert (await g.client.get("/ui?token=adm-1")).status_code == 200


async def test_a102_admin_state_requires_admin_token(gw):
    """A-102：运维面 /admin/state 从外部无凭据 → 401。"""
    raw = external_cfg(keys=[{"name": "t", "key": "sk-test-1"}], admin_token="adm-1")
    raw["listen"] = "0.0.0.0:8080"
    async with gw(raw, client_host=EXTERNAL_HOST) as g:
        r = await g.client.get("/admin/state")
        assert r.status_code == 401
        ok = await g.client.get("/admin/state", headers={"authorization": "Bearer adm-1"})
        assert ok.status_code == 200


async def test_a103_admin_sleep_requires_auth_before_model_validation(gw):
    """A-103：运维写操作无凭据 → 401，**且不能先返回"未知模型 404"**。

    历史问题：以前先校验模型再鉴权，于是未鉴权客户端能通过 404/401 的差异
    探测"某个模型名是否存在"。鉴权必须在路由参数校验之前。
    """
    raw = external_cfg(keys=[{"name": "t", "key": "sk-test-1"}], admin_token="adm-1")
    raw["listen"] = "0.0.0.0:8080"
    async with gw(raw, client_host=EXTERNAL_HOST) as g:
        r = await g.client.post("/admin/sleep", json={"model": "/models/does-not-exist"})
        assert r.status_code == 401, "必须先鉴权，再校验模型（否则泄露模型存在性）"


async def test_a104_wrong_key_is_401_and_leaks_nothing(gw):
    """A-104：错误 key → 401，且响应里不能出现模型清单。"""
    raw = external_cfg(keys=[{"name": "t", "key": "sk-test-1"}])
    raw["listen"] = "0.0.0.0:8080"
    async with gw(raw, client_host=EXTERNAL_HOST) as g:
        r = await g.client.get("/v1/models", headers={"authorization": "Bearer sk-wrong"})
        assert r.status_code == 401
        body = r.text
        assert A not in body, "错误响应里泄露了模型名"
        assert "models" not in body


async def test_key_cannot_use_model_outside_its_scope(gw):
    """按 key 的模型边界：不在白名单里 → 403，不是 404（模型确实存在）。"""
    raw = external_cfg(keys=[{"name": "narrow", "key": "sk-narrow", "models": ["/models/other"]}])
    raw["listen"] = "0.0.0.0:8080"
    async with gw(raw, client_host=EXTERNAL_HOST) as g:
        r = await g.client.post(
            "/v1/audio/speech",
            json={"model": A, "input": "hi"},
            headers={"authorization": "Bearer sk-narrow"},
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "forbidden"


async def test_rpm_limit_returns_429(gw):
    """按 key 的速率上限：超了给 429 rate_limited，而不是继续放行。"""
    raw = external_cfg(keys=[{"name": "slow", "key": "sk-slow", "rpm": 2}])
    raw["listen"] = "0.0.0.0:8080"
    async with gw(raw, client_host=EXTERNAL_HOST) as g:
        h = {"authorization": "Bearer sk-slow"}
        codes = [(await g.client.get("/v1/models", headers=h)).status_code for _ in range(3)]
        assert codes[:2] == [200, 200]
        assert codes[2] == 429


async def test_missing_model_is_400_not_guessed(gw):
    """开放产品不猜候选：缺 model → 400 missing_model，并列出可用模型。"""
    async with gw() as g:
        r = await g.client.post("/v1/audio/speech", json={"input": "hi"})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "missing_model"


def test_non_loopback_listen_without_keys_refuses_to_start(tmp_path):
    """fail closed：非 loopback 监听 + 没有 key/admin token → 拒绝启动。"""
    import yaml

    from omni_gateway.app import create_app

    raw = base_cfg()
    raw["listen"] = "0.0.0.0:8080"
    raw["auth"] = {"loopback_bypass": True, "keys": []}
    cfg_path = tmp_path / "gw.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(RuntimeError) as ei:
        create_app(str(cfg_path))
    assert "拒绝启动" in str(ei.value)
