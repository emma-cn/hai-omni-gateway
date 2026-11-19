"""多模态：视频（异步长任务）、图像、ASR、voices；以及"长任务租约"这块缺口。"""
from __future__ import annotations

import copy
import json

import pytest

from conftest import json_of, multi_cfg

pytestmark = pytest.mark.asyncio

H3 = "/models/MiniMax-H3"
VOX = "/models/VoxCPM2"
TTS = "/models/TTS-A"


def two_video_cfg():
    raw = copy.deepcopy(multi_cfg())
    raw["models"]["/models/HunyuanVideo"] = {
        "base_url": "http://omni-v2:60201", "card": "card5",
        "capabilities": ["video"], "ttl_s": 0, "concurrency": 1,
        "wake_timeout_s": 3,
    }
    return raw


# ------------------------------------------------------------------ 视频（长任务）


async def test_video_create_is_passthrough_202(gw):
    """上游异步语义要原样透传：202 + job id，网关不自己造 job。"""
    async with gw(multi_cfg()) as g:
        r = await g.client.post("/v1/videos", json={"model": H3, "prompt": "a cat"})
        assert r.status_code == 202
        body = json_of(r)
        assert body["id"] == "vid-1"
        assert body["status"] == "queued"
        sent = g.backend.last("video_create")
        assert sent["host"] == "omni-v"
        assert json.loads(sent["body"])["prompt"] == "a cat"


async def test_video_create_releases_placeholder_immediately(gw):
    """POST 只返回 id，几十毫秒就该把"在飞占位"还回去（长任务靠租约，不靠 inflight）。"""
    async with gw(multi_cfg()) as g:
        card = g.sched.card_of(H3)
        r = await g.client.post("/v1/videos", json={"model": H3, "prompt": "x"})
        assert r.status_code == 202
        assert card.inflight(H3) == 0, "异步接口的占位没有释放"
        # 但卡还得按"任务可能还在跑"来对待：模型仍醒着
        assert card.awake_model == H3


@pytest.mark.xfail(strict=True, reason="未实现：lease_s 只入库不记账，长任务跑到一半会把卡换走（DESIGN §7 自称待实现）")
async def test_running_video_job_holds_card_lease(gw):
    """有视频 job 在跑时，同卡的短任务不许把卡换走 —— 否则视频任务被挤下去重载几分钟。"""
    async with gw(multi_cfg()) as g:
        r = await g.client.post("/v1/videos", json={"model": H3, "prompt": "x"})
        assert r.status_code == 202
        # 同卡的 ASR 模型来一发短请求
        await g.client.post("/v1/audio/transcriptions",
                            files={"file": ("a.wav", b"RIFF", "audio/wav")})
        assert g.backend.state["omni-v"] == "awake", "视频任务在跑，卡却被换走了（租约没生效）"


async def test_video_get_single_video_model_routes_without_model_field(gw):
    """OpenAI 的轮询 GET 不带 model：只有一个视频模型时能唯一路由。"""
    async with gw(multi_cfg()) as g:
        r = await g.client.get("/v1/videos/vid-1")
        assert r.status_code == 200
        assert json_of(r)["status"] == "completed"
        assert g.backend.last("video_get")["host"] == "omni-v"


async def test_video_content_bytes_passthrough(gw):
    async with gw(multi_cfg()) as g:
        r = await g.client.get("/v1/videos/vid-1/content")
        assert r.status_code == 200
        assert r.content == g.backend.video
        assert r.headers["content-type"].startswith("video/mp4")


async def test_video_list(gw):
    async with gw(multi_cfg()) as g:
        r = await g.client.get("/v1/videos?model=" + H3)
        assert r.status_code == 200
        assert json_of(r)["object"] == "list"


@pytest.mark.xfail(strict=True, reason="未实现：多视频模型时 GET /v1/videos/{id} 无 model 字段 -> 400，轮询链路断（见报告 M1）")
async def test_video_get_with_two_video_models(gw):
    """两个视频模型并存时，轮询也得能路由（按 id 归属或默认视频模型）。"""
    async with gw(two_video_cfg()) as g:
        r = await g.client.get("/v1/videos/vid-1")
        assert r.status_code == 200, f"轮询被拒：{r.status_code} {r.text[:200]}"


async def test_video_get_two_video_models_current_400(gw):
    """现状基线：多视频模型时轮询确实 400（钉住，等修好后这条要一起改）。"""
    async with gw(two_video_cfg()) as g:
        r = await g.client.get("/v1/videos/vid-1")
        assert r.status_code == 400
        assert json_of(r)["error"]["type"] == "invalid_request_error"


# --------------------------------------------------------------------- 图像 / ASR


async def test_image_generation_passthrough(gw):
    async with gw(multi_cfg()) as g:
        r = await g.client.post("/v1/images/generations",
                                json={"model": H3, "prompt": "cat"})
        assert r.status_code == 200
        assert json_of(r)["data"][0]["url"]
        assert g.backend.last("images")["host"] == "omni-v"


async def test_image_edits_multipart_passthrough(gw):
    async with gw(multi_cfg()) as g:
        r = await g.client.post("/v1/images/edits",
                                data={"model": H3, "prompt": "make it snow"},
                                files={"image": ("a.png", b"\x89PNG", "image/png")})
        assert r.status_code == 200
        sent = g.backend.last("image_edits")
        assert sent["host"] == "omni-v"
        assert b"make it snow" in sent["body"]
        assert sent["headers"]["content-type"].startswith("multipart/form-data")


async def test_transcription_passthrough(gw):
    async with gw(multi_cfg()) as g:
        r = await g.client.post("/v1/audio/transcriptions",
                                files={"file": ("a.wav", b"RIFF", "audio/wav")})
        assert r.status_code == 200
        assert json_of(r)["text"]
        assert g.backend.last("transcription")["host"] == "omni-t"


async def test_voices_create_and_delete(gw):
    async with gw(multi_cfg()) as g:
        r = await g.client.post("/v1/audio/voices", json={"model": TTS, "name": "v1"})
        assert r.status_code == 200
        r = await g.client.delete("/v1/audio/voices/v1", params={"model": TTS})
        assert r.status_code == 200
        assert [c["method"] for c in g.backend.calls if c["action"] == "voices"] == ["POST", "DELETE"]


async def test_voices_delete_on_video_model_400(gw):
    async with gw(multi_cfg()) as g:
        r = await g.client.delete("/v1/audio/voices/v1", params={"model": H3})
        assert r.status_code == 400
        assert json_of(r)["error"]["type"] == "unsupported_capability"


# ------------------------------------------------------------------ 卡位隔离


async def test_models_on_different_cards_do_not_sleep_each_other(gw):
    """跨卡互不干扰：card4 的 TTS 与 card5 的 H3 可以同时醒着。"""
    async with gw(multi_cfg()) as g:
        await g.client.post("/v1/audio/speech", json={"model": TTS, "input": "hi"})
        await g.client.post("/v1/videos", json={"model": H3, "prompt": "x"})
        assert g.backend.state["omni-a"] == "awake"
        assert g.backend.state["omni-v"] == "awake"
        assert g.backend.count("sleep") == 0
