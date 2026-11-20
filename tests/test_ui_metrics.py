"""管理页与指标的凭据（issue #13 / A-1104）。

踩过的坑：#13 的原始报告自相矛盾——要求 `/ui` 继续 401（保留鉴权），
又要求页面里加 token 输入框。但 401 的页面浏览器根本渲染不出来，
输入框永远没人能填。正确切法是**页面壳公开、数据面全要 token**：

    GET /ui        -> 200（只有 HTML，无任何数据）
    GET /admin/*   -> 需要 admin token
    GET /metrics   -> 需要 admin token（与 /admin 同一口径）
"""
from __future__ import annotations

import pytest

from conftest import base_cfg

pytestmark = pytest.mark.asyncio

TOKEN = "adm-test-token-123"


def _cfg() -> dict:
    cfg = base_cfg()
    cfg["auth"] = dict(
        cfg["auth"],
        admin_token=TOKEN,
        admin_loopback_bypass=False,     # 运维面永远要 token
    )
    return cfg


async def test_ui_shell_is_public_but_admin_data_is_not(gw):
    """页面壳 200（否则 token 输入框无从填起），数据面仍然 401。"""
    async with gw(_cfg(), client_host="203.0.113.5") as g:
        ui = await g.client.get("/ui")
        assert ui.status_code == 200, "/ui 是页面壳，必须能打开"
        assert "text/html" in ui.headers.get("content-type", "")

        # 数据面：没有 token 一律 401
        for path in ("/admin/state", "/admin/ledger", "/metrics"):
            r = await g.client.get(path)
            assert r.status_code == 401, f"{path} 无 token 应 401"


async def test_ui_html_carries_token_support(gw):
    """页面必须自己带凭据入口：localStorage + Authorization（#13 的核心）。"""
    async with gw(_cfg()) as g:
        html = (await g.client.get("/ui")).text
    assert "localStorage" in html
    assert "Authorization" in html
    assert "Bearer " in html
    assert "admin_token" in html or "admin token" in html.lower()


async def test_admin_and_metrics_accept_token(gw):
    """/admin/state 与 /metrics 带正确 token 后 200；错 token 仍 401。"""
    async with gw(_cfg(), client_host="203.0.113.5") as g:
        h = {"authorization": f"Bearer {TOKEN}"}
        st = await g.client.get("/admin/state", headers=h)
        assert st.status_code == 200
        m = await g.client.get("/metrics", headers=h)
        assert m.status_code == 200
        body = m.text
        # 指标必须是文本格式且有真实数字，不能是空壳
        assert "omni_up 1" in body
        assert "omni_models{" in body
        assert "omni_inflight{" in body
        bad = await g.client.get("/metrics", headers={"authorization": "Bearer nope"})
        assert bad.status_code == 401


async def test_query_token_is_rejected(gw):
    """?token= 已废弃：URL 会进访问日志/历史/Referer。"""
    async with gw(_cfg(), client_host="203.0.113.5") as g:
        r = await g.client.get(f"/admin/state?token={TOKEN}")
        assert r.status_code == 401, "query 传 token 不再被接受"
