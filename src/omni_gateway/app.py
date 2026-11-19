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

import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import __version__
from .config import GatewayConfig
from .scheduler import DrainTimeout, Scheduler, WakeFailed
from .upstream import OmniClient

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
    body = {"error": {"message": message, "type": type_, "code": status, **extra}}
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


def create_app(config_path: str) -> FastAPI:
    cfg = GatewayConfig.load(config_path)
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=1800.0, write=120.0, pool=5.0))
    omni = OmniClient(client)
    sched = Scheduler(cfg, omni)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        sched.start()
        caps = sorted({c for m in cfg.models.values() for c in m.capabilities})
        log.info("hai-omni-gateway %s 启动：%d 张卡 / %d 个模型 / 能力 %s",
                 __version__, len(cfg.cards), len(cfg.models), caps)
        # 启动对账：把后端全部睡一遍再归零，避免"以为都睡着、实际有模型醒着"导致唤醒 OOM
        await sched.reconcile()
        log.info("启动对账完成：全部模型已置为睡眠态")
        try:
            yield
        finally:
            await sched.stop()
            await client.aclose()

    app = FastAPI(title="hai-omni-gateway", version=__version__, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.sched = sched
    app.state.client = client

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
            data.append({
                "id": name,
                "object": "model",
                "owned_by": "hai-omni-gateway",
                "meta": {"omnigateway": {
                    "card": m.card,
                    "capabilities": m.capabilities,
                    "status": card.state(name),
                    "inflight": card.inflight(name),
                    "lease_s": m.lease_s,
                }},
            })
        return {"object": "list", "data": data}

    # ------------------------------------------------------------ 透传
    async def _passthrough(request: Request, model: str, path: str) -> Response:
        m = cfg.models[model]
        try:
            await sched.acquire(model)
        except WakeFailed as e:
            log.warning("唤醒 %s 失败: %s", model, e)
            return _error(503, f"模型 {model} 唤醒失败，请稍后重试", "wake_failed", retry_after=10)
        except DrainTimeout as e:
            # 上游规则：暂停没完成不许 sleep。这里拒绝换台而不是硬切，
            # 让客户端拿 503 重试（Retry-After 用一个 drain 周期的量级）。
            log.warning("拒绝换台（drain 未完成）: %s", e)
            return _error(503, f"显卡正忙，换台被拒绝：{e}", "drain_timeout", retry_after=5)

        released = False

        async def _release() -> None:
            nonlocal released
            if not released:
                released = True
                await sched.release(model)

        try:
            body = await request.body()
            headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
            upstream = await client.send(
                client.build_request(request.method, m.base_url + path, content=body, headers=headers),
                stream=True,
            )
            resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
            # 异步任务接口（视频）：POST 只返回 id，产物在别处取——
            # "这条请求在飞"不代表模型忙，立即释放占位，由 lease 负责不让它被挤掉。
            if path.startswith(ASYNC_PREFIXES) and request.method == "POST":
                await _release()

            async def body_iter() -> AsyncIterator[bytes]:
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    await upstream.aclose()
                    await _release()

            return StreamingResponse(
                body_iter(),
                status_code=upstream.status_code,
                headers=resp_headers,
                media_type=upstream.headers.get("content-type"),
            )
        except Exception as e:
            await _release()
            log.exception("转发失败 model=%s path=%s", model, path)
            return _error(502, f"上游转发失败: {type(e).__name__}: {e}", "upstream_error")

    def _resolve_model(request: Request, raw: bytes) -> str | None:
        if request.query_params.get("model"):
            return request.query_params["model"]
        try:
            return (json.loads(raw or b"{}") or {}).get("model")
        except Exception:
            return None

    async def _dispatch(request: Request) -> Response:
        path = request.url.path
        cap = _capability_for(request.method, path)
        raw = await request.body()
        model = _resolve_model(request, raw)
        if not model:
            # 语音/图像类接口的 model 可能在 multipart 表单或 query 里，退回到唯一候选
            candidates = [n for n, m in cfg.models.items() if not cap or cap in m.capabilities]
            if len(candidates) == 1:
                model = candidates[0]
            else:
                return _error(400, f"缺少 model 字段（候选：{candidates}）", "invalid_request_error")
        if model not in cfg.models:
            return _error(404, f"未知模型 {model}", "invalid_request_error")
        if cap and cap not in cfg.models[model].capabilities:
            return _error(
                400,
                f"模型 {model} 不支持该接口：需要能力 {cap}，它声明的是 {cfg.models[model].capabilities}",
                "unsupported_capability",
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
        except WakeFailed as e:
            return JSONResponse({"detail": f"唤醒失败: {e}"}, status_code=503)
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
