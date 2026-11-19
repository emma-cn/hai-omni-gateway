"""测试基座：可编程的假 vllm-omni 后端 + 通过 ASGI 直连网关。

设计原则
--------
* **完全不碰真后端**：`httpx.AsyncClient` 被替换成 `MockTransport`，所以
  `http://omni-a:60589` 这类地址只在内存里路由，一个包都不会真的发出去
  （开发机上 60589 真的躺着 indextts，测试绝不能去睡它）。
* **网关的 lifespan 真跑**：`create_app()` 的启动对账（reconcile）会 sleep 所有模型，
  所以每个用例拿到 client 的那一刻会 `backend.clear()`，避免 reconcile 的调用
  干扰"换台/唤醒"的断言；专门测 reconcile 的用例自己去读原始记录。
* 一台"后端"用 host 区分（`omni-a` / `omni-b` / `omni-v` …），断言时能一眼看出
  某次 sleep/wake 打到了哪个模型。
"""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

from omni_gateway import app as appmod

# 打补丁前先抓住真类：补丁会改掉 httpx 模块属性，测试自己的客户端不能跟着被替换
REAL_ASYNC_CLIENT = httpx.AsyncClient

# --------------------------------------------------------------------------- 假后端


class _Chunks(httpx.AsyncByteStream):
    """按小块吐字节的真流式 body。

    为什么要它：`httpx.Response(200, content=b"...")` 在构造时就 `read()` 完了，
    `aiter_raw()` 会抛 `StreamConsumed`；而网关对上游用的是 `send(..., stream=True)`
    + `aiter_raw()`。真后端是流式返回，假后端必须也是流式，否则测的不是真路径。
    """

    def __init__(self, data: bytes, chunk: int = 7) -> None:
        self.data = data
        self.chunk = chunk

    async def __aiter__(self):
        for i in range(0, len(self.data), self.chunk):
            yield self.data[i:i + self.chunk]


def _resp(status: int = 200, body: bytes = b"", **headers: str) -> httpx.Response:
    return httpx.Response(status, stream=_Chunks(body),
                          headers={k.replace("_", "-"): v for k, v in headers.items()})


def _json_resp(payload: Any, status: int = 200) -> httpx.Response:
    import json as _json
    return _resp(status, _json.dumps(payload, ensure_ascii=False).encode(),
                 content_type="application/json")


class FakeBackend:
    """可编程的 vllm-omni 假后端。

    可调开关：
      errors[action]  = (status, body)   -> 该动作返回指定错误
      raises[action]  = True             -> 该动作抛 ConnectError（模拟进程挂了）
      delays[action]  = 秒               -> 该动作慢给你看（配合并发/超时用例）
      dead            = {host, ...}      -> 该 host 的 /health 一直 500（唤醒失败）
    action 取值：sleep / wakeup / resume / health / speech / transcription /
                voices / images / image_edits / video_create / video_get / video_content
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.errors: dict[str, tuple[int, str]] = {}
        self.raises: set[str] = set()
        self.delays: dict[str, float] = {}
        self.dead: set[str] = set()
        # False = 后端没有 /v1/omni/status（老镜像），网关应回退"盲睡"
        self.status_supported: bool = True
        # host -> "awake" / "asleep"
        self.state: dict[str, str] = {}
        self.audio = b"RIFF\x24\x00\x00\x00WAVEfake-wav-payload-for-md5-check"
        self.video = b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes"
        self.video_id = "vid-1"

    # ---------------------------------------------------------------- 断言辅助
    def clear(self) -> None:
        self.calls.clear()

    def actions(self) -> list[str]:
        return [self._action(c["path"], c["method"]) for c in self.calls]

    def hosts_for(self, action: str) -> list[str]:
        return [c["host"] for c in self.calls if self._action(c["path"], c["method"]) == action]

    def count(self, action: str) -> int:
        return len(self.hosts_for(action))

    def last(self, action: str) -> dict[str, Any] | None:
        for c in reversed(self.calls):
            if self._action(c["path"], c["method"]) == action:
                return c
        return None

    # ------------------------------------------------------------------ 路由表
    @staticmethod
    def _action(path: str, method: str = "POST") -> str:
        if path.endswith("/v1/omni/sleep"):
            return "sleep"
        if path.endswith("/v1/omni/wakeup"):
            return "wakeup"
        if path.endswith("/v1/omni/resume"):
            return "resume"
        if path.endswith("/v1/omni/status"):
            return "status"
        if path.endswith("/health"):
            return "health"
        if path.endswith("/v1/audio/speech"):
            return "speech"
        if path.endswith("/v1/audio/transcriptions"):
            return "transcription"
        if "/v1/audio/voices" in path:
            return "voices"
        if path.endswith("/v1/images/generations"):
            return "images"
        if path.endswith("/v1/images/edits"):
            return "image_edits"
        if path.endswith("/content"):
            return "video_content"
        if path.rstrip("/").endswith("/v1/videos"):
            return "video_create" if method == "POST" else "video_list"
        if "/v1/videos/" in path:
            return "video_get"
        return "unknown"

    # ------------------------------------------------------------------- 处理
    async def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        action = self._action(path, request.method)
        self.calls.append({
            "method": request.method,
            "host": host,
            "path": path,
            "query": request.url.query.decode(),
            "headers": dict(request.headers),
            "body": request.content,
            "action": action,
        })

        if action in self.delays:
            await asyncio.sleep(self.delays[action])
        if action in self.raises:
            raise httpx.ConnectError("fake backend down", request=request)
        if action in self.errors:
            code, text = self.errors[action]
            return _resp(code, text.encode(), content_type="text/plain")

        if action == "status":
            # 默认 404：模拟"还没打 status 补丁"的老镜像，逼网关走回退路径
            if self.status_supported is False:
                return _resp(404, b"Not Found", content_type="text/plain")
            # 默认"睡着"：这样普通的"按需唤醒"用例流程不变；
            # 想测"接管已醒着的后端"就显式 state[host] = "awake"
            asleep = self.state.get(host, "asleep") == "asleep"
            return _json_resp({
                "status": "SUCCESS",
                "stages": {"0": {"sleeping": asleep, "tracked_asleep": asleep}},
                "asleep": asleep,
                "paused": False,
                "sleeping_tags": ["wake"] if asleep else [],
                "vram": {"free_mb": 1000, "total_mb": 24000, "used_mb": 23000},
            })
        if action == "sleep":
            self.state[host] = "asleep"
            return _json_resp({"ok": True, "stage_ids": [0]})
        if action == "wakeup":
            self.state[host] = "awake"
            return _json_resp({"ok": True})
        if action == "resume":
            return _json_resp({"ok": True})
        if action == "health":
            if host in self.dead:
                return _resp(500, b"boom", content_type="text/plain")
            return _json_resp({"status": "healthy"})
        if action == "speech":
            return _resp(200, self.audio, content_type="audio/wav", x_upstream="fake")
        if action == "transcription":
            return _json_resp({"text": "hello from fake asr"})
        if action == "voices":
            if request.method == "DELETE":
                return _json_resp({"deleted": True})
            return _json_resp({"voice": "x"})
        if action == "images":
            return _json_resp({"created": 1, "data": [{"url": "http://img/1.png"}]})
        if action == "image_edits":
            return _json_resp({"created": 1, "data": [{"url": "http://img/2.png"}]})
        if action == "video_create":
            return _json_resp({"id": self.video_id, "object": "video", "status": "queued"},
                              status=202)
        if action == "video_list":
            return _json_resp({"object": "list", "data": []})
        if action == "video_get":
            return _json_resp({"id": self.video_id, "status": "completed"})
        if action == "video_content":
            return _resp(200, self.video, content_type="video/mp4")
        return _json_resp({"detail": f"fake backend has no route {path}"}, status=404)


# --------------------------------------------------------------------- 配置构造


def base_cfg() -> dict[str, Any]:
    """最小可用配置：一张卡两个 TTS 模型（同卡轮换是核心场景）。"""
    return {
        # 测试用 loopback 监听：非 loopback + 无 key 会被 fail-closed 拒绝启动
        "listen": "127.0.0.1:8080",
        "queue_limit": 200,
        # loopback 免 key；"testclient" 是 ASGI transport 的 peer 名
        "auth": {
            "loopback_bypass": True,
            # 测试基座等价"本地 dev"：运维面也免 token，免得每个用例都塞 header。
            # 生产默认是 False（运维面永远要 token），由
            # test_admin_surface_requires_token_even_on_loopback 专门覆盖。
            "admin_loopback_bypass": True,
            "loopback_peers": ["127.0.0.1", "::1", "localhost", "testclient"],
        },
        "cards": {"card4": {"min_dwell_s": 0.0, "drain_timeout_s": 0.5}},
        "models": {
            "/models/TTS-A": {
                "base_url": "http://omni-a:60589/",
                "card": "card4",
                "stage_ids": [0, 1],
                "capabilities": ["speech"],
                "ttl_s": 300,
                "concurrency": 2,
                "wake_timeout_s": 3,
            },
            "/models/TTS-B": {
                "base_url": "http://omni-b:60592",
                "card": "card4",
                "stage_ids": [0],
                "capabilities": ["speech"],
                "ttl_s": 300,
                "concurrency": 2,
                "wake_timeout_s": 3,
            },
        },
    }


def multi_cfg() -> dict[str, Any]:
    """多模态 + 多卡：speech / transcription / image / video 各一个模型。"""
    return {
        "listen": "127.0.0.1:8080",
        "queue_limit": 200,
        "auth": {
            "loopback_bypass": True,
            "admin_loopback_bypass": True,
            "loopback_peers": ["127.0.0.1", "::1", "localhost", "testclient"],
        },
        "cards": {
            "card4": {"min_dwell_s": 0.0, "drain_timeout_s": 0.5},
            "card5": {"min_dwell_s": 0.0, "drain_timeout_s": 0.5},
        },
        "models": {
            "/models/TTS-A": {
                "base_url": "http://omni-a:60589", "card": "card4",
                "capabilities": ["speech"], "ttl_s": 300, "concurrency": 2,
                "wake_timeout_s": 3,
            },
            "/models/VoxCPM2": {
                "base_url": "http://omni-b:60592", "card": "card4",
                "capabilities": ["speech"], "ttl_s": 300, "concurrency": 2,
                "wake_timeout_s": 3,
            },
            "/models/Step-Audio-2-mini": {
                "base_url": "http://omni-t:60596", "card": "card5",
                "capabilities": ["transcription"], "ttl_s": 300, "concurrency": 2,
                "wake_timeout_s": 3,
            },
            "/models/MiniMax-H3": {
                "base_url": "http://omni-v:60200", "card": "card5",
                "capabilities": ["video", "image"], "ttl_s": 0, "concurrency": 1,
                "wake_timeout_s": 3, "lease_s": 1800,
            },
        },
    }


# ----------------------------------------------------------------------- fixture


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def make_app(tmp_path, monkeypatch, backend):
    """把网关的 httpx.AsyncClient 换成 MockTransport，返回一个"造 app"的工厂。

    注意：`appmod.httpx` 就是 httpx 模块本身，所以这个补丁是**全局**的 ——
    测试自己的客户端一律走 `REAL_ASYNC_CLIENT`，绕开补丁。
    """
    transport = httpx.MockTransport(backend.handle)

    def _factory(**kw: Any):
        kw.setdefault("transport", transport)
        return REAL_ASYNC_CLIENT(**kw)

    def _make(raw: dict[str, Any] | None = None):
        cfg = copy.deepcopy(raw if raw is not None else base_cfg())
        path = tmp_path / "gateway.yaml"
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        monkeypatch.setattr(appmod.httpx, "AsyncClient", _factory)
        return appmod.create_app(str(path))

    return _make


@pytest.fixture
def gw(make_app, backend):
    """`async with gw(cfg) as g:` -> g.app / g.client / g.sched / g.backend。

    进入时 lifespan 已跑完（启动对账会 sleep 一遍），随后清空调用记录。
    """
    import contextlib

    @contextlib.asynccontextmanager
    async def _open(raw: dict[str, Any] | None = None, client_host: str | None = None):
        """client_host：模拟请求来自哪个地址。

        ⚠ httpx.ASGITransport 默认 client=("127.0.0.1", 123)，也就是**永远像本机**；
        要测"外部客户端"必须显式传 client_host，否则免 key 的 loopback 分支会一直命中
        （本仓就踩过：第一版鉴权用例全绿，但测的其实是本机流量）。
        """
        app = make_app(raw)
        async with app.router.lifespan_context(app):
            backend.clear()
            transport = (
                httpx.ASGITransport(app=app, client=(client_host, 4321))
                if client_host
                else httpx.ASGITransport(app=app)
            )
            async with REAL_ASYNC_CLIENT(transport=transport, base_url="http://gw.test") as c:
                yield SimpleNamespace(
                    app=app, client=c, backend=backend,
                    cfg=app.state.cfg, sched=app.state.sched,
                )

    return _open


# ------------------------------------------------------------------ 小工具


def speech_body(model: str = "/models/TTS-A", text: str = "你好") -> dict[str, Any]:
    return {"model": model, "input": text, "voice": "default",
            "response_format": "wav"}


def json_of(response: httpx.Response) -> dict[str, Any]:
    return json.loads(response.content.decode("utf-8"))
