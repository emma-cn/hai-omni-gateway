"""接口层：探活、/v1/models、model 解析、能力校验、请求体原样透传。"""
from __future__ import annotations

import pytest

from conftest import base_cfg, json_of, multi_cfg, speech_body

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------------ 探活与元信息


async def test_healthz(gw):
    async with gw() as g:
        r = await g.client.get("/healthz")
        assert r.status_code == 200
        body = json_of(r)
        assert body["status"] == "ok"
        assert body["version"]
        assert "card4" in body["cards"]
        # 刚启动时全部睡着
        assert body["cards"]["card4"]["awake"] is None


async def test_root_returns_service_meta(gw):
    async with gw() as g:
        r = await g.client.get("/")
        body = json_of(r)
        assert body["service"] == "hai-omni-gateway"
        assert body["ui"] == "/ui"
        assert set(body["models"]) == {"/models/TTS-A", "/models/TTS-B"}


async def test_ui_page_served(gw):
    async with gw() as g:
        r = await g.client.get("/ui")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "/admin/state" in r.text


async def test_openapi_docs_available(gw):
    async with gw() as g:
        r = await g.client.get("/openapi.json")
        assert r.status_code == 200
        paths = json_of(r)["paths"]
        for p in ("/v1/audio/speech", "/v1/audio/transcriptions",
                  "/v1/images/generations", "/v1/videos"):
            assert p in paths, f"{p} 未出现在 OpenAPI 里"


# ------------------------------------------------------------------- /v1/models


async def test_list_models_has_gateway_meta(gw):
    async with gw() as g:
        r = await g.client.get("/v1/models")
        data = json_of(r)["data"]
        assert len(data) == 2
        for item in data:
            meta = item["meta"]["omnigateway"]
            assert meta["card"] == "card4"
            assert meta["status"] == "asleep"
            assert meta["inflight"] == 0
            assert "lease_s" in meta
            assert item["owned_by"] == "hai-omni-gateway"


async def test_list_models_filter_by_capability(gw):
    async with gw(multi_cfg()) as g:
        speech = json_of(await g.client.get("/v1/models?capability=speech"))["data"]
        assert {m["id"] for m in speech} == {"/models/TTS-A", "/models/VoxCPM2"}

        video = json_of(await g.client.get("/v1/models?capability=video"))["data"]
        assert {m["id"] for m in video} == {"/models/MiniMax-H3"}

        # 没有模型声明这个能力 -> 空列表而不是全量
        none = json_of(await g.client.get("/v1/models?capability=embedding"))["data"]
        assert none == []


async def test_models_status_reflects_awake(gw):
    async with gw() as g:
        assert g.sched.card_of("/models/TTS-A").awake_model is None
        r = await g.client.post("/v1/audio/speech", json=speech_body("/models/TTS-A"))
        assert r.status_code == 200
        data = json_of(await g.client.get("/v1/models"))["data"]
        by_id = {m["id"]: m["meta"]["omnigateway"] for m in data}
        assert by_id["/models/TTS-A"]["status"] == "awake"
        assert by_id["/models/TTS-B"]["status"] == "asleep"


# ------------------------------------------------------------ model 字段解析


async def test_unknown_model_404(gw):
    async with gw() as g:
        r = await g.client.post("/v1/audio/speech", json=speech_body("/models/NOPE"))
        assert r.status_code == 404
        err = json_of(r)["error"]
        assert err["type"] == "model_not_found"
        assert "/models/NOPE" in err["message"]


async def test_404_is_self_describing(gw):
    """A-403 / issue #10：未知路径的 404 要列出端点族，客户端能自解。"""
    async with gw() as g:
        r = await g.client.get("/api/events")     # 旧页面轮询的那个路径
        assert r.status_code == 404
        body = json_of(r)
        assert body["error"]["code"] == "not_found"
        flat = str(body["endpoints"])
        for expect in ("/v1/audio/speech", "/v1/models", "/v1/videos"):
            assert expect in flat, f"404 自描述里缺 {expect}"


async def test_response_has_request_id_but_no_fake_rate_headers(gw):
    """A-404 / A-405：x-request-id 永远带；**不限额时不发**限流头。

    为什么不发 `limit: 0`：OpenAI 语义里 0 = "你没有额度"，
    按 remaining 退避的客户端会立刻限流自己（假头比缺头危险）。
    """
    from conftest import base_cfg

    async with gw() as g:
        r = await g.client.get("/v1/models")          # loopback，无 key = 不限
        assert r.headers.get("x-request-id"), "缺 x-request-id"
        lowered = {k.lower() for k in r.headers}
        assert "x-ratelimit-limit-requests" not in lowered, "不限额时不该发 limit 头"
        r2 = await g.client.get("/v1/models", headers={"x-request-id": "abc123"})
        assert r2.headers["x-request-id"] == "abc123"

    # 有限额的 key：必须发真实值，且 remaining ≤ limit
    raw = base_cfg()
    raw["auth"] = dict(raw["auth"], keys=[{"name": "k", "key": "sk-1", "rpm": 5}])
    async with gw(raw, client_host="203.0.113.9") as g:
        r = await g.client.get("/v1/models", headers={"authorization": "Bearer sk-1"})
        assert r.status_code == 200
        limit = int(r.headers["x-ratelimit-limit-requests"])
        remaining = int(r.headers["x-ratelimit-remaining-requests"])
        assert limit == 5
        assert 0 <= remaining <= limit, f"remaining={remaining} 超出 [0,{limit}]"


async def test_models_is_a_capability_catalog(gw):
    """A-406/407/408：/v1/models 要带 modalities / languages / max_input_chars。"""
    async with gw() as g:
        r = await g.client.get("/v1/models")
        entry = json_of(r)["data"][0]
        assert "modalities" in entry
        assert entry["modalities"]["output"], "modalities.output 不该为空"
        assert "languages" in entry
        assert "max_input_chars" in entry
        # 未声明必须显式说明，别让客户端把"没填"当成"不支持"
        assert "declared" in entry


async def test_missing_model_is_rejected_not_guessed(gw):
    """缺 model 一律 400 missing_model —— 即使候选唯一也不猜。

    产品口径：候选数会随部署变化，猜唯一候选等于让行为随部署静默漂移。
    """
    async with gw(multi_cfg()) as g:
        r = await g.client.post("/v1/audio/transcriptions",
                                files={"file": ("a.wav", b"RIFF", "audio/wav")})
        assert r.status_code == 400
        assert json_of(r)["error"]["code"] == "missing_model"
        assert g.backend.count("transcription") == 0, "不该把请求转发给任何模型"


async def test_missing_model_with_several_candidates_400(gw):
    async with gw() as g:
        r = await g.client.post("/v1/audio/speech", json={"input": "hi"})
        assert r.status_code == 400
        msg = json_of(r)["error"]["message"]
        assert "缺少 model" in msg
        assert "/models/TTS-A" in msg  # 把候选列出来，方便排查


async def test_model_from_query_string(gw):
    async with gw() as g:
        r = await g.client.post("/v1/audio/speech?model=/models/TTS-B",
                                content=b"not-json")
        assert r.status_code == 200
        assert "omni-b" in g.backend.hosts_for("speech")


async def test_query_model_wins_over_body(gw):
    async with gw() as g:
        r = await g.client.post("/v1/audio/speech?model=/models/TTS-B",
                                json=speech_body("/models/TTS-A"))
        assert r.status_code == 200
        assert "omni-b" in g.backend.hosts_for("speech")


# ------------------------------------------------------------------ 能力校验


async def test_speech_request_to_video_model_400(gw):
    """README 承诺：不能把 TTS 请求转发给视频模型。"""
    async with gw(multi_cfg()) as g:
        r = await g.client.post("/v1/audio/speech", json=speech_body("/models/MiniMax-H3"))
        assert r.status_code == 400
        err = json_of(r)["error"]
        assert err["type"] == "capability_unsupported"
        assert err["code"] == "capability_unsupported"   # code 是机器码，不是 HTTP 状态
        assert g.backend.count("speech") == 0


async def test_video_request_to_tts_model_400(gw):
    async with gw(multi_cfg()) as g:
        r = await g.client.post("/v1/videos", json={"model": "/models/TTS-A", "prompt": "x"})
        assert r.status_code == 400
        assert json_of(r)["error"]["type"] == "capability_unsupported"
        assert g.backend.count("video_create") == 0


async def test_image_capability_is_separate_from_video(gw):
    """H3 声明了 video+image，两个入口都要通；TTS 模型两个都不通。"""
    async with gw(multi_cfg()) as g:
        ok = await g.client.post("/v1/images/generations",
                                 json={"model": "/models/MiniMax-H3", "prompt": "cat"})
        assert ok.status_code == 200
        bad = await g.client.post("/v1/images/generations",
                                  json={"model": "/models/TTS-A", "prompt": "cat"})
        assert bad.status_code == 400
        assert json_of(bad)["error"]["type"] == "capability_unsupported"


async def test_unknown_capability_endpoint_is_not_proxied(gw):
    """网关没声明的路径不该被黑洞转发（防"把任意路径透给后端"）。"""
    async with gw() as g:
        r = await g.client.post("/v1/embeddings", json={"model": "/models/TTS-A"})
        assert r.status_code in (404, 405)
        assert g.backend.calls == []


# -------------------------------------------------------------------- 透传语义


async def test_body_is_forwarded_byte_identical(gw):
    """model 字段不做改写（llama-swap 的教训）：上游收到的 body 必须逐字节一致。"""
    async with gw() as g:
        raw = b'{"model":"/models/TTS-A","input":"\xe4\xbd\xa0\xe5\xa5\xbd","seed":7}'
        r = await g.client.post("/v1/audio/speech", content=raw,
                                headers={"content-type": "application/json"})
        assert r.status_code == 200
        sent = g.backend.last("speech")
        assert sent["body"] == raw


async def test_path_is_not_rewritten(gw):
    async with gw() as g:
        await g.client.post("/v1/audio/speech", json=speech_body())
        await g.client.post("/v1/audio/voices", json={"model": "/models/TTS-A", "name": "x"})
        paths = [c["path"] for c in g.backend.calls if c["action"] in ("speech", "voices")]
        assert paths == ["/v1/audio/speech", "/v1/audio/voices"]


async def test_client_auth_header_is_forwarded(gw):
    async with gw() as g:
        await g.client.post("/v1/audio/speech", json=speech_body(),
                            headers={"authorization": "Bearer sk-test"})
        assert g.backend.last("speech")["headers"]["authorization"] == "Bearer sk-test"


async def test_hop_by_hop_headers_stripped(gw):
    """逐跳头不能透传；端到端头要透传。

    注意 `connection` / `content-length` 由 httpx 自己重算（默认就带
    `connection: keep-alive`），所以不能用它们判断"有没有透传" —— 只看
    客户端自带、httpx 不会补的那几个。
    """
    async with gw() as g:
        await g.client.post("/v1/audio/speech", json=speech_body(),
                            headers={"te": "trailers",
                                     "proxy-authorization": "Basic xxx",
                                     "x-trace": "abc"})
        sent = g.backend.last("speech")
        headers = sent["headers"]
        assert "te" not in headers
        assert "proxy-authorization" not in headers
        assert "transfer-encoding" not in headers
        assert headers.get("x-trace") == "abc"
        assert headers.get("content-length") == str(len(sent["body"]))


async def test_upstream_error_status_is_passed_through(gw):
    async with gw() as g:
        g.backend.errors["speech"] = (422, "bad voice")
        r = await g.client.post("/v1/audio/speech", json=speech_body())
        assert r.status_code == 422
        assert "bad voice" in r.text


async def test_upstream_unreachable_502(gw):
    async with gw() as g:
        g.backend.raises.add("speech")
        r = await g.client.post("/v1/audio/speech", json=speech_body())
        assert r.status_code == 502
        err = json_of(r)["error"]
        assert err["type"] == "upstream_error"
        assert "ConnectError" in err["message"]


async def test_inflight_is_released_after_response(gw):
    async with gw() as g:
        await g.client.post("/v1/audio/speech", json=speech_body())
        card = g.sched.card_of("/models/TTS-A")
        assert card.inflight("/models/TTS-A") == 0


async def test_inflight_is_released_even_on_upstream_error(gw):
    async with gw() as g:
        g.backend.errors["speech"] = (500, "boom")
        await g.client.post("/v1/audio/speech", json=speech_body())
        card = g.sched.card_of("/models/TTS-A")
        assert card.inflight("/models/TTS-A") == 0
