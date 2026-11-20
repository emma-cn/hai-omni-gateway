"""FastAPI 应用：对外是标准 OpenAI / vllm-omni 接口（音频 / 图像 / 视频），对内是队列 + 调度。

路由按 endpoint 声明的能力（capability）匹配模型声明的 capabilities：
  音频  /v1/audio/speech、/v1/audio/transcriptions、/v1/audio/voices
  图像  /v1/images/generations、/v1/images/edits
  视频  /v1/videos、/v1/videos/{id}、/v1/videos/{id}/content

长任务（视频）与短任务（TTS）的差别：
  * 短任务：请求在飞 → 占住卡；返回即释放；换台成本 4~5s，可以频繁切
  * 长任务：一次生成几分钟 → 期间不允许换台；上游本身就是异步
    （POST /v1/videos 返回 id，GET 轮询，GET /content 取产物），
    网关只做透传，并用 lease 保证有活跃 job 时该模型不会被挤下去

已实现（MVP）：模型注册 / 卡级单写者调度 / 两段式唤醒 / 多模态透传 / 能力校验
未实现：鉴权与配额 / 异步 job 排队与持久化 / 按 token 预算准入 / Prometheus / OOM 熔断
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, catalog
from .auth import Authenticator, AuthError
from .config import GatewayConfig
from .ledger import Ledger, summarize
from .scheduler import (
    BackendUnavailable,
    BadRequestRejected,
    DrainTimeout,
    QueueFull,
    RequestTimeout,
    Scheduler,
    SleepFailed,
    WakeFailed,
    WakeOOM,
)
from .upstream import OmniClient, scrape_vllm_queue

log = logging.getLogger(__name__)

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}

# (method, path) -> 所需能力。模型必须在配置里声明对应 capability 才能接这条路。
ENDPOINTS: dict[tuple[str, str], str] = {
    ("POST", "/v1/audio/speech"): "speech",
    ("POST", "/v1/audio/transcriptions"): "transcription",
    ("POST", "/v1/audio/voices"): "speech",
    ("GET", "/v1/audio/voices"): "speech",   # 列音色：上游 200，网关曾漏注册 → 405
    ("POST", "/v1/images/generations"): "image",
    ("POST", "/v1/images/edits"): "image",
    ("POST", "/v1/videos"): "video",
    ("GET", "/v1/videos"): "video",
}

# 上游本身就是异步任务接口的前缀：POST 只返回 id，产物另取
ASYNC_PREFIXES = ("/v1/videos",)


def _error(status: int, message: str, type_: str = "gateway_error", **extra: Any) -> JSONResponse:
    headers: dict[str, str] = {}
    if "retry_after" in extra:
        headers["Retry-After"] = str(extra.pop("retry_after"))
    # code 是**机器码**（不是 HTTP 状态）：客户端按它分支，HTTP 状态只给人看。
    # 早期版本把数字塞进 code，导致 400/429 分不清语义，已废弃（见 docs/ERRORS.md）。
    body = {"error": {"message": message, "type": type_, "code": type_, **extra}}
    return JSONResponse(body, status_code=status, headers=headers)


def _capability_for(method: str, path: str) -> str | None:
    if (method, path) in ENDPOINTS:
        return ENDPOINTS[(method, path)]
    if path.startswith("/v1/audio/voices"):
        return "speech"
    if path.startswith("/v1/videos"):
        return "video"
    if path.startswith("/v1/images/"):
        return "image"
    if path.startswith("/v1/audio/"):
        return "speech"
    return None


def _assert_auth_sane(cfg: GatewayConfig) -> None:
    """非 loopback 监听 + 没配 key/admin token → 拒绝启动（fail closed）。

    最坏的部署不是"起不来"，而是"起来了但谁都能用"——尤其是运维面：
    一个匿名请求就能把模型睡掉。所以这里宁可直接崩，也不静默裸奔。
    """
    host = cfg.listen.rpartition(":")[0]
    is_loopback_listen = host in ("127.0.0.1", "localhost", "::1")
    if is_loopback_listen:
        return
    has_keys = bool(cfg.auth.keys_by_value)
    has_admin = bool(cfg.auth.resolved_admin_token())
    if not has_keys or not has_admin:
        missing = []
        if not has_keys:
            missing.append("auth.keys（业务面 API key）")
        if not has_admin:
            missing.append("auth.admin_token / 环境变量 OMNI_ADMIN_TOKEN（运维面）")
        raise RuntimeError(
            f"拒绝启动：listen={cfg.listen} 不是 loopback，但缺少 {' + '.join(missing)}。"
            "开放服务不允许无鉴权运行；调试请把 listen 改成 127.0.0.1:PORT。"
        )


def create_app(config_path: str) -> FastAPI:
    cfg = GatewayConfig.load(config_path)
    _assert_auth_sane(cfg)
    authr = Authenticator(cfg.auth)
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=1800.0, write=120.0, pool=5.0))
    omni = OmniClient(client)
    sched = Scheduler(cfg, omni)

    # 上游排队深度探针：每 5s 抓一次 vLLM 的 num_requests_waiting/running。
    # 没有它，「44 秒里多少是等 max_num_seqs」就只能靠推理（A-602 的归因要求可证）。
    upstream_queue: dict[str, dict[str, float]] = {}

    async def _probe_upstream() -> None:
        interval = cfg.upstream_probe_interval_s
        if interval <= 0:
            return
        while True:
            for name, m in cfg.models.items():
                sample = await scrape_vllm_queue(client, m.base_url)
                if sample:
                    upstream_queue[name] = sample
            await asyncio.sleep(interval)

    # 请求账本（A-601/602）：lifespan 里启停，这里先建实例
    led: Ledger | None = (
        Ledger(
            cfg.ledger.path,
            max_bytes=cfg.ledger.max_bytes,
            keep=cfg.ledger.keep,
            ring=cfg.ledger.ring,
        )
        if cfg.ledger.enabled
        else None
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        sched.start()
        if led is not None:
            led.start()     # 后台 writer task：请求路径只入队，不落盘阻塞
        probe_task = asyncio.create_task(_probe_upstream())
        caps = sorted({c for m in cfg.models.values() for c in m.capabilities})
        log.info("hai-omni-gateway %s 启动：%d 张卡 / %d 个模型 / 能力 %s",
                 __version__, len(cfg.cards), len(cfg.models), caps)
        # 启动对账：把后端全部睡一遍再归零，避免"以为都睡着、实际有模型醒着"导致唤醒 OOM
        await sched.reconcile()
        log.info("启动对账完成：全部模型已置为睡眠态")
        try:
            yield
        finally:
            probe_task.cancel()
            if led is not None:
                await led.stop()   # 把缓冲里残留的行刷盘，别丢最后一批
            await sched.stop()
            await client.aclose()

    app = FastAPI(title="hai-omni-gateway", version=__version__, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.sched = sched
    app.state.client = client

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """404 自描述：告诉客户端"这里到底有什么"。

        历史问题（issue #10）：客户端轮询 `/api/events` 一直拿到默认 404（空体），
        分不清是路径写错、版本不对、还是服务没实现 —— 只能反复重试。
        现在把端点族一并返回，客户端一次就能自解。
        """
        rid = getattr(request.state, "request_id", "")
        if exc.status_code == 404:
            return JSONResponse(
                {
                    "error": {
                        "message": f"没有这个端点：{request.url.path}",
                        "type": "not_found",
                        "code": "not_found",
                        "param": str(request.url.path),
                    },
                    "endpoints": catalog.endpoint_families(),
                },
                status_code=404,
                headers={"x-request-id": rid},
            )
        return JSONResponse(
            {
                "error": {
                    "message": str(getattr(exc, "detail", exc)),
                    "type": "http_error",
                    "code": exc.status_code,
                }
            },
            status_code=exc.status_code,
            headers={"x-request-id": rid},
        )

    # ---------------------------------------------------------------- 请求账本
    def _record_request(
        request: Request, model: str, path: str, stats: dict[str, Any], t_req: float
    ) -> None:
        """一条请求一行日志。字段名稳定，便于外部直接 `jq` 出三段分布。"""
        if led is None:
            return
        ident = getattr(request.state, "identity", None)
        led.record(
            {
                "request_id": getattr(request.state, "request_id", ""),
                "key": getattr(ident, "name", "") if ident else "",
                "client": request.client.host if request.client else "",
                "method": request.method,
                "path": path,
                "model": model,
                "status": stats.get("status") or 0,
                "error": stats.get("error") or None,
                # 三段耗时（A-602）：任何一段异常都独立可见，便于归因 p95
                "queue_wait_ms": round(stats.get("queue_wait_ms", 0.0), 3),
                "wake_ms": round(stats.get("wake_ms", 0.0), 3),
                # 上游排队/抢占等待（首字节之前），与 gen_ms 严格不重叠
                "upstream_wait_ms": round(stats.get("upstream_wait_ms", 0.0), 3),
                # 请求发出时上游自己的队列深度（可能滞后 ≤5s，够用于归因）
                "upstream_waiting": (upstream_queue.get(model) or {}).get("waiting"),
                "upstream_running": (upstream_queue.get(model) or {}).get("running"),
                "gen_ms": round(stats.get("gen_ms", 0.0), 3),
                "total_ms": round((time.monotonic() - t_req) * 1000.0, 3),
                "bytes_out": stats.get("bytes_out", 0),
                "switches": stats.get("switches") or [],
            }
        )

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        """鉴权放在路由匹配**之前**。

        历史教训（A-103）：以前"未知模型 404"会先于"无凭据 401"返回，
        等于把某个模型名存不存在泄露给未鉴权客户端。
        """
        # 请求 ID：客户端给了就沿用（便于跨服务串联），没给就生成一个。
        # 全链路返回，客户端报障时能直接对上服务端日志。
        rid = (
            request.headers.get("x-request-id")
            or request.headers.get("x-correlation-id")
            or uuid.uuid4().hex
        )
        request.state.request_id = rid
        peer = request.client.host if request.client else None
        # 不再支持 ?token=：URL 会进访问日志、浏览器历史、Referer。
        # /ui 页面把 token 存在 localStorage 并放进 Authorization 头（见 static/ui.html）。
        headers = request.headers
        try:
            ident = authr.authenticate(request.url.path, peer, headers)
        except AuthError as e:
            headers = {"x-request-id": rid}
            if e.status == 429:
                headers["Retry-After"] = "60"
                # 429 也要带限流头：客户端据此知道额度上限与何时恢复
                rate = getattr(e, "rate", None)
                if rate:
                    limit, remaining, reset = rate
                    headers["x-ratelimit-limit-requests"] = str(limit)
                    headers["x-ratelimit-remaining-requests"] = str(remaining)
                    headers["x-ratelimit-reset-requests"] = str(reset)
            body: dict[str, Any] = {"message": e.message, "type": e.code, "code": e.code}
            if getattr(e, "reason", ""):
                body["reason"] = e.reason
            return JSONResponse({"error": body}, status_code=e.status, headers=headers)
        request.state.identity = ident
        response = await call_next(request)
        # 请求 ID 永远带；限流头**只在真的有限额时才发**。
        #
        # 为什么不发 "limit: 0"：OpenAI 语义里 0 = "你没有额度"，
        # 按 remaining 做退避的客户端会立刻限流自己 —— 那组头不传递信息，只传递恐慌。
        # 缺头比假头安全（OpenAI 也不强制发这组头）。
        limit, remaining, reset = authr.rate_snapshot(ident)
        response.headers["x-request-id"] = rid
        if limit > 0:
            response.headers["x-ratelimit-limit-requests"] = str(limit)
            response.headers["x-ratelimit-remaining-requests"] = str(remaining)
            response.headers["x-ratelimit-reset-requests"] = str(reset)
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            # build 由镜像/发布流水线注入（OMNI_BUILD），必须等于镜像 tag 后缀；
            # 没注入时退回版本号，便于本地裸跑。
            "build": os.environ.get("OMNI_BUILD") or __version__,
            "cards": {name: {"awake": card.awake_model} for name, card in sched.cards.items()},
        }

    @app.get("/v1/models")
    async def list_models(capability: str | None = None) -> dict[str, Any]:
        data = []
        for name, m in cfg.models.items():
            if capability and capability not in m.capabilities:
                continue
            card = sched.card_of(name)
            status = card.state(name)
            entry: dict[str, Any] = {
                "id": name,
                "object": "model",
                "owned_by": "hai-omni-gateway",
            }
            # 能力目录字段平铺在模型对象上（OpenAI 客户端习惯直接读），
            # 同时保留 meta.omnigateway 给运维面用。
            entry.update(catalog.catalog_of(
                m, status=status, inflight=card.inflight(name), awake=status == "awake"
            ))
            # meta.omnigateway 保留 status/inflight 以兼容既有客户端与用例
            # （顶层的是"能力目录"口径，meta 里的是"运维面"口径，同一事实两处可读）
            entry["meta"] = {
                "omnigateway": {
                    "card": m.card,
                    "status": status,
                    "inflight": card.inflight(name),
                    "capabilities": m.capabilities,
                    "lease_s": m.lease_s,
                }
            }
            data.append(entry)
        return {"object": "list", "data": data}

    # ------------------------------------------------------------ 透传
    async def _voices_all(request: Request) -> Response:
        """不带 model 的 `GET /v1/audio/voices`：汇总全部语音模型的音色。

        单模型失败不影响整体：某后端睡着/挂了，就在结果里标 `error`，
        而不是让整次发现请求 500——发现接口的价值在于"尽量告诉我有什么"。
        """
        merged: list[dict[str, Any]] = []
        sources: dict[str, Any] = {}
        for name, m in cfg.models.items():
            if "speech" not in m.capabilities:
                continue
            try:
                await sched.acquire(name)
            except Exception as e:  # noqa: BLE001 - 发现接口要把失败降级成字段
                sources[name] = {"ok": False, "error": type(e).__name__}
                continue
            try:
                upstream = await client.get(m.base_url + "/v1/audio/voices")
                if upstream.status_code != 200:
                    sources[name] = {"ok": False, "error": f"http={upstream.status_code}"}
                    continue
                payload = upstream.json()
                voices = payload.get("voices") or payload.get("data") or []
                for v in voices:
                    item = dict(v) if isinstance(v, dict) else {"name": str(v)}
                    item.setdefault("model", name)
                    merged.append(item)
                sources[name] = {"ok": True, "count": len(voices)}
            except Exception as e:  # noqa: BLE001
                sources[name] = {"ok": False, "error": type(e).__name__}
            finally:
                await sched.release(name)
        return JSONResponse(
            {
                "object": "list",
                "data": merged,
                "voices": merged,          # 兼容只读 voices 字段的客户端
                # 哪些模型的音色没取到：客户端可据此判断"少了"还是"真没有"
                "sources": sources,
                "models_queried": list(sources),
            }
        )

    async def _passthrough(request: Request, model: str, path: str) -> Response:
        m = cfg.models[model]
        t_req = time.monotonic()
        stats: dict[str, Any] = {"queue_wait_ms": 0.0, "wake_ms": 0.0,
                                 "upstream_wait_ms": 0.0, "gen_ms": 0.0,
                                 "bytes_out": 0, "status": 0, "error": None}

        def _fail(
            status: int, message: str, code: str, retry_after: int = 0, reason: str = ""
        ) -> Response:
            """失败也要进账本：没有失败样本，p95 就只能算成功请求，归因会偏。"""
            stats["status"] = status
            stats["error"] = code
            _record_request(request, model, path, stats, t_req)
            return _error(status, message, code, retry_after=retry_after, reason=reason)

        try:
            stats["queue_wait_ms"], stats["wake_ms"] = await sched.acquire(model)
        except WakeFailed as e:
            log.warning("唤醒 %s 失败: %s", model, e)
            return _fail(503, f"模型 {model} 唤醒失败，请稍后重试", "wake_failed", retry_after=10)
        except BackendUnavailable as e:
            # 后端进程不在：重试唤醒毫无意义。502 让调用方立刻失败，
            # 且避免把客户端拖到超时。冷却期由 scheduler 负责。
            log.error("后端不可用（不重试）: %s", e)
            return _fail(502, str(e), "backend_unavailable", retry_after=15)
        except WakeOOM as e:
            log.warning("唤醒时显存不足: %s", e)
            return _fail(503, str(e), "wake_oom", retry_after=10)
        except BadRequestRejected as e:
            # 上游拒参数：不重试、不计熔断，原样告诉调用方是它自己发错了
            log.warning("上游拒绝参数: %s", e)
            return _fail(400, str(e), "bad_request")
        except QueueFull as e:
            # 队列满：快速失败 + 告诉客户端多久再来，比让它无限等更有用
            log.warning("队列已满，拒绝排队: %s", e)
            # 429 的 code 统一用 OpenAI 的 rate_limit_exceeded（客户端按 code 分支），
            # 我们自己的"队列满"细分放进 reason 字段。
            return _fail(429, str(e), "rate_limit_exceeded", retry_after=2, reason="queue_full")
        except RequestTimeout as e:
            # 排队超时：请求根本没发到上游，客户端可安全重试
            log.warning("排队超时: %s", e)
            return _fail(503, str(e), "request_timeout", retry_after=5)
        except SleepFailed as e:
            # 旧模型睡不下去时绝不能继续唤醒：硬换必 OOM，OOM 会打死后端进程。
            log.error("睡眠失败，已中止换台: %s", e)
            return _fail(503, f"显卡释放失败，已中止换台：{e}", "sleep_failed", retry_after=15)
        except DrainTimeout as e:
            # 上游规则：暂停没完成不许 sleep。这里拒绝换台而不是硬切，
            # 让客户端拿 503 重试（Retry-After 用一个 drain 周期的量级）。
            log.warning("拒绝换台（drain 未完成）: %s", e)
            return _fail(503, f"显卡正忙，换台被拒绝：{e}", "drain_timeout", retry_after=5)

        released = False

        async def _release() -> None:
            nonlocal released
            if not released:
                released = True
                await sched.release(model)

        try:
            body = await request.body()
            headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
            # 单请求超时用模型自己的 request_timeout_s：
            # 全局 read=1800s 是"永不超时"的另一种说法，长任务也会因此挂到客户端先断。
            # 连接超时单独给 5s —— 连不上应当快速报错，而不是占着名额干等。
            req_timeout = httpx.Timeout(m.request_timeout_s, connect=5.0)
            t_gen = time.monotonic()
            stats["status"] = 0  # 待上游返回后填
            upstream = await client.send(
                client.build_request(
                    request.method, m.base_url + path, content=body, headers=headers, timeout=req_timeout
                ),
                stream=True,
            )
            resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
            # 异步任务接口（视频）：POST 只返回 id，产物在别处取——
            # "这条请求在飞"不代表模型忙，立即释放占位，由 lease 负责不让它被挤掉。
            stats["status"] = upstream.status_code
            if path.startswith(ASYNC_PREFIXES) and request.method == "POST":
                await _release()

            async def body_iter() -> AsyncIterator[bytes]:
                t_first: float | None = None
                try:
                    async for chunk in upstream.aiter_raw():
                        if t_first is None:
                            t_first = time.monotonic()
                            # 首字节之前的时间**不是生成**：那是上游（vLLM 的
                            # max_num_seqs 队列 / 并发抢占）在等。把它算进 gen_ms
                            # 会让 8 并发下的 p95 变成 p50 的 62 倍，归因彻底失效。
                            stats["upstream_wait_ms"] = (t_first - t_gen) * 1000.0
                        stats["bytes_out"] = stats.get("bytes_out", 0) + len(chunk)
                        yield chunk
                finally:
                    # gen_ms = 首字节之后的时间（真正的"在算"）；
                    # 没收到任何字节时退化为全程，避免把失败请求记成"0 耗时"。
                    t_end = time.monotonic()
                    if t_first is None:
                        stats["upstream_wait_ms"] = (t_end - t_gen) * 1000.0
                        stats["gen_ms"] = 0.0
                    else:
                        stats["gen_ms"] = (t_end - t_first) * 1000.0
                    await upstream.aclose()
                    await _release()
                    _record_request(request, model, path, stats, t_req)

            return StreamingResponse(
                body_iter(),
                status_code=upstream.status_code,
                headers=resp_headers,
                media_type=upstream.headers.get("content-type"),
            )
        except Exception as e:
            await _release()
            log.exception("转发失败 model=%s path=%s", model, path)
            return _fail(502, f"上游转发失败: {type(e).__name__}: {e}", "upstream_error")

    def _resolve_model(request: Request, raw: bytes) -> str | None:
        """从 query / JSON / 表单 / multipart 里取 model。

        为什么不"猜唯一候选"：候选数一变（多挂一个模型），缺 model 的请求就会静默
        落到别的模型上——开放产品不能有这种随部署漂移的行为。缺就报 400。
        """
        if request.query_params.get("model"):
            return request.query_params["model"]

        ctype = (request.headers.get("content-type") or "").lower()

        if "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
            # 只做"取一个字段"，不引第三方解析：multipart 里找 name="model" 的分段，
            # urlencoded 里按 key=value 拆。取值都要去掉引号与换行。
            text = raw.decode("utf-8", "replace")
            if "application/x-www-form-urlencoded" in ctype:
                from urllib.parse import parse_qs

                vals = parse_qs(text).get("model")
                return vals[0] if vals else None
            marker = 'name="model"'
            idx = text.find(marker)
            if idx >= 0:
                tail = text[idx + len(marker):]
                # 跳过其余 header 直到空行，然后取到下一个边界
                parts = tail.split("\r\n\r\n", 1)
                if len(parts) == 2:
                    value = parts[1].split("\r\n", 1)[0].strip()
                    return value or None
            return None

        try:
            return (json.loads(raw or b"{}") or {}).get("model")
        except Exception:
            return None

    async def _dispatch(request: Request) -> Response:
        path = request.url.path
        cap = _capability_for(request.method, path)
        raw = await request.body()
        t_dispatch = time.monotonic()
        model = _resolve_model(request, raw)

        # 发现类端点不需要 model：不带就返回全部模型的音色（每条标 model）。
        # "model 必填"是为了防止**生成**时静默落到别的模型上；列表接口没有这个问题，
        # 强制必填只会让客户端为了列一次音色先去查 /v1/models。
        if not model and path == "/v1/audio/voices" and request.method == "GET":
            return await _voices_all(request)

        def _reject(status: int, message: str, code: str, **kw: Any) -> Response:
            """在透传层之前就被拒的请求也要进账本（缺 model / 未知模型 / 能力不符）。"""
            _record_request(
                request,
                model or "",
                path,
                {"queue_wait_ms": 0.0, "wake_ms": 0.0, "gen_ms": 0.0,
                 "status": status, "error": code},
                t_dispatch,
            )
            return _error(status, message, code, **kw)
        if not model:
            # 开放产品**不猜**候选：候选数一变，缺 model 的请求就会静默落到别的模型上。
            # 正确行为是明确报错并列出可用模型。
            candidates = [n for n, m in cfg.models.items() if not cap or cap in m.capabilities]
            return _reject(
                400,
                f"缺少 model 字段。可用模型：{candidates}",
                "missing_model",
                param="model",
            )
        if model not in cfg.models:
            return _reject(404, f"未知模型 {model}", "model_not_found", param="model")
        if cap and cap not in cfg.models[model].capabilities:
            return _reject(
                400,
                f"模型 {model} 不支持该接口：需要能力 {cap}，它声明的是 {cfg.models[model].capabilities}",
                "capability_unsupported",
                param="model",
            )

        # 按 key 的边界：能力与模型白名单（P0-1）
        ident = getattr(request.state, "identity", None)
        if ident is not None:
            if not ident.allows_model(model):
                return _reject(
                    403,
                    f"该 API key（{ident.name}）无权调用模型 {model}",
                    "forbidden",
                    param="model",
                )
            if cap and not ident.allows_capability(cap):
                return _error(
                    403,
                    f"该 API key（{ident.name}）无权使用能力 {cap}",
                    "forbidden",
                )
        return await _passthrough(request, model, path)

    # 把声明过的 endpoint 一次性挂到 FastAPI
    for (method, path) in ENDPOINTS:
        app.add_api_route(path, _dispatch, methods=[method])

    # 带路径参数的接口单独注册
    @app.delete("/v1/audio/voices/{name}")
    async def voices_delete(name: str, request: Request) -> Response:
        return await _dispatch(request)

    @app.get("/v1/videos/{video_id}")
    async def videos_get(video_id: str, request: Request) -> Response:
        return await _dispatch(request)

    @app.get("/v1/videos/{video_id}/content")
    async def videos_content(video_id: str, request: Request) -> Response:
        return await _dispatch(request)

    # ------------------------------------------------------------ 管理页
    _UI = Path(__file__).with_name("static") / "ui.html"

    @app.get("/ui", include_in_schema=False)
    async def ui() -> Response:
        if not _UI.exists():
            return JSONResponse({"error": "ui.html 缺失"}, status_code=500)
        return FileResponse(_UI, media_type="text/html; charset=utf-8")

    @app.get("/")
    async def root() -> Response:
        return JSONResponse({
            "service": "hai-omni-gateway",
            "version": __version__,
            "ui": "/ui",
            "docs": "/docs",
            "models": sorted(cfg.models),
        })

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        """Prometheus 文本格式（A-1104）。

        凭据口径与 /admin/* 一致：**要 admin token**（`Authorization: Bearer <token>`）。
        为什么不放开只读：这里会暴露模型名、卡位与负载，等于把内部拓扑公开；
        而 Prometheus 的 scrape 配置本来就支持 `authorization.credentials`，成本很低。

        只输出**真的在发生**的数字：卡状态、在飞数、账本写入/丢弃、三段耗时分位数。
        没有任何"看起来专业但没人算"的指标。
        """
        lines: list[str] = [
            "# HELP omni_up 进程存活（恒 1，抓不到就是抓不到）",
            "# TYPE omni_up gauge",
            "omni_up 1",
            "# HELP omni_models 模型状态：1=醒着 0=睡着",
            "# TYPE omni_models gauge",
        ]
        for name in cfg.models:
            card = sched.card_of(name)
            awake = 1 if card.state(name) == "awake" else 0
            lines.append(f'omni_models{{model="{name}",card="{cfg.models[name].card}"}} {awake}')
        lines.append("# HELP omni_inflight 在飞请求数")
        lines.append("# TYPE omni_inflight gauge")
        for name in cfg.models:
            lines.append(f'omni_inflight{{model="{name}"}} {sched.card_of(name).inflight(name)}')

        if upstream_queue:
            lines += [
                "# HELP omni_upstream_waiting 上游 vLLM 队列中等待调度的请求数（每 5s 采样）",
                "# TYPE omni_upstream_waiting gauge",
            ]
            for name in cfg.models:
                sample = upstream_queue.get(name) or {}
                if sample:
                    lines.append(
                        f'omni_upstream_waiting{{model="{name}"}} {sample.get("waiting", 0)}'
                    )
            lines += [
                "# HELP omni_upstream_running 上游正在运行的请求数",
                "# TYPE omni_upstream_running gauge",
            ]
            for name in cfg.models:
                sample = upstream_queue.get(name) or {}
                if sample:
                    lines.append(
                        f'omni_upstream_running{{model="{name}"}} {sample.get("running", 0)}'
                    )

        if led is None:
            lines.append("# 账本关闭：omni_ledger_* 与 omni_segment_* 不可得")
        else:
            st = led.stats()
            lines += [
                "# HELP omni_ledger_written_total 已落盘行数",
                "# TYPE omni_ledger_written_total counter",
                f"omni_ledger_written_total {st['written']}",
                "# HELP omni_ledger_dropped_total 因队列满丢弃的行数（>0 说明记账跟不上）",
                "# TYPE omni_ledger_dropped_total counter",
                f"omni_ledger_dropped_total {st['dropped']}",
                "# HELP omni_ledger_disabled 账本是否因不可写而自我禁用",
                "# TYPE omni_ledger_disabled gauge",
                f"omni_ledger_disabled {1 if st['disabled'] else 0}",
            ]
            summary = summarize(led.recent(500))
            lines += [
                "# HELP omni_segment_ms 三段耗时分位数（最近窗口，非全量）",
                "# TYPE omni_segment_ms gauge",
            ]
            for seg in ("queue_wait_ms", "wake_ms", "upstream_wait_ms", "gen_ms", "total_ms"):
                for q, key in (("0.5", "p50"), ("0.95", "p95")):
                    v = summary.get(seg, {}).get(key)
                    if v is not None:
                        lines.append(f'omni_segment_ms{{segment="{seg}",quantile="{q}"}} {v}')
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.get("/admin/ledger")
    async def admin_ledger(request: Request, limit: int = 100) -> JSONResponse:
        """账本读数：最近 N 条 + 三段耗时聚合。

        A-601/602 的可验证面：`segments` 里三段分开给 p50/p95，
        这就是"p95 归因"的最小数据集（不用把 JSONL 拉回本地算）。
        """
        # 鉴权在中间件里统一做（/admin/* 需 admin token），这里不重复判断
        if led is None:
            return JSONResponse({"enabled": False, "stats": None, "entries": [], "summary": {}})
        entries = led.recent(limit)
        return JSONResponse(
            {
                "enabled": True,
                "stats": led.stats(),
                "summary": summarize(entries),
                "entries": entries,
            }
        )

    @app.get("/admin/state")
    async def admin_state() -> dict[str, Any]:
        return {
            "version": __version__,
            "cards": [card.stats() for card in sched.cards.values()],
            "models": [
                {
                    "name": name,
                    "card": m.card,
                    "capabilities": m.capabilities,
                    "status": sched.card_of(name).state(name),
                    "inflight": sched.card_of(name).inflight(name),
                    "ttl_s": m.ttl_s,
                    "lease_s": m.lease_s,
                    "concurrency": m.concurrency,
                    "base_url": m.base_url,
                }
                for name, m in cfg.models.items()
            ],
        }

    @app.post("/admin/wake")
    async def admin_wake(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        model = payload.get("model")
        if model not in cfg.models:
            return JSONResponse({"detail": f"未知模型 {model}"}, status_code=404)
        try:
            await sched.card_of(model).force_wake(model)
        except BackendUnavailable as e:
            return JSONResponse(
                {"detail": f"后端不可用: {e}", "code": "backend_unavailable"},
                status_code=502, headers={"Retry-After": "15"},
            )
        except BadRequestRejected as e:
            return JSONResponse({"detail": f"上游拒绝参数: {e}", "code": "bad_request"}, status_code=400)
        except (WakeOOM, WakeFailed, SleepFailed, DrainTimeout, QueueFull, RequestTimeout) as e:
            # 换台失败一律 503 + Retry-After：对客户端而言"现在切不过去，晚点再来"，
            # 未捕获会变成 500，而 500 会被客户端当成"网关有 bug，不该重试"。
            # detail 保持人话（客户端日志里要能直接读懂），code 给机器判断用。
            # WakeFailed 用"唤醒失败"措辞保持向后兼容（已有用例与客户端按这个词匹配）。
            kind = type(e).__name__.lower()
            prefix = "唤醒失败" if isinstance(e, (WakeFailed, WakeOOM)) else "换台失败"
            return JSONResponse(
                {"detail": f"{prefix}({kind}): {e}", "code": kind},
                status_code=503,
                headers={"Retry-After": "5"},
            )
        return JSONResponse({"ok": True, "model": model, "status": "awake"})

    @app.post("/admin/sleep")
    async def admin_sleep(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        model = payload.get("model")
        if model not in cfg.models:
            return JSONResponse({"detail": f"未知模型 {model}"}, status_code=404)
        try:
            await sched.card_of(model).force_sleep(model)
        except RuntimeError as e:
            return JSONResponse({"detail": str(e)}, status_code=409)
        return JSONResponse({"ok": True, "model": model, "status": "asleep"})

    @app.post("/admin/reconcile")
    async def admin_reconcile(payload: dict[str, Any] = Body(default={})) -> JSONResponse:
        """把网关状态与真实显存对齐：对指定模型（或全部）**无条件**发一次上游 sleep。

        存在理由：网关重启、外部直接调过上游、OOM 后进程重启，都会让"网关以为睡着"
        与"实际还占着显存"分叉。此时再唤醒别的模型就是 OOM。sleep 在上游是幂等的，
        所以这里不做判断，直接睡一遍再把本地状态归零。
        """
        target = payload.get("model")
        models = [target] if target else list(cfg.models)
        unknown = [m for m in models if m not in cfg.models]
        if unknown:
            return JSONResponse({"detail": f"未知模型 {unknown}"}, status_code=404)
        done, failed = [], []
        for m in models:
            try:
                await sched.card_of(m).force_sleep(m)
                done.append(m)
            except Exception as e:  # 单卡失败不影响其它卡
                failed.append({"model": m, "error": str(e)})
        code = 200 if not failed else 207
        return JSONResponse({"ok": not failed, "slept": done, "failed": failed}, status_code=code)

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg_path = os.environ.get("OMNI_GATEWAY_CONFIG", "config/gateway.yaml")
    cfg = GatewayConfig.load(cfg_path)
    host, _, port = cfg.listen.rpartition(":")
    uvicorn.run(create_app(cfg_path), host=host or "0.0.0.0", port=int(port or 8080))


if __name__ == "__main__":
    main()
