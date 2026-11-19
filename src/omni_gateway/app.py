"""FastAPI 应用：对外是标准 OpenAI / vllm-omni 接口，对内是队列 + 调度。

已实现（MVP）：
  GET  /healthz                  网关自身健康
  GET  /v1/models                合并各后端，带 status: awake / asleep
  POST /v1/audio/speech          透传；目标模型睡着则先唤醒（排队而非挂死）
  POST /v1/audio/voices          透传（上传命名音色）
  DELETE /v1/audio/voices/{name} 透传

未实现（后续）：
  * 鉴权 / 按 key 配额 / 计费
  * 异步 job（POST /v1/jobs → 202）
  * 按 token 预算的准入
  * Prometheus 指标
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import __version__
from .config import GatewayConfig
from .scheduler import Scheduler, WakeFailed
from .upstream import OmniClient

log = logging.getLogger(__name__)

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}


def _error(status: int, message: str, type_: str = "gateway_error", **extra: Any) -> JSONResponse:
    body = {"error": {"message": message, "type": type_, "code": status}}
    headers = {}
    if "retry_after" in extra:
        headers["Retry-After"] = str(extra.pop("retry_after"))
    body["error"].update(extra)
    return JSONResponse(body, status_code=status, headers=headers)


def create_app(config_path: str) -> FastAPI:
    cfg = GatewayConfig.load(config_path)
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=900.0, write=60.0, pool=5.0))
    omni = OmniClient(client)
    sched = Scheduler(cfg, omni)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        sched.start()
        log.info("hai-omni-gateway %s 启动，%d 张卡 / %d 个模型", __version__, len(cfg.cards), len(cfg.models))
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
            "cards": {name: {"awake": card.awake_model} for name, card in sched.cards.items()},
        }

    @app.get("/v1/models")
    async def list_models() -> dict[str, Any]:
        data = []
        for name, m in cfg.models.items():
            card = sched.card_of(name)
            data.append({
                "id": name,
                "object": "model",
                "owned_by": "hai-omni-gateway",
                "meta": {"omnigateway": {"card": m.card, "status": card.state(name),
                                          "inflight": card.inflight(name)}},
            })
        return {"object": "list", "data": data}

    # ------------------------------------------------------------ 透传辅助
    async def _passthrough(request: Request, model: str, path: str) -> Response:
        m = cfg.models[model]
        try:
            await sched.acquire(model)
        except WakeFailed as e:
            log.warning("唤醒 %s 失败: %s", model, e)
            return _error(503, f"模型 {model} 唤醒失败，请稍后重试", "wake_failed", retry_after=10)
        try:
            body = await request.body()
            headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
            upstream = await client.send(
                client.build_request(request.method, m.base_url + path, content=body, headers=headers),
                stream=True,
            )
            resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}

            async def body_iter() -> AsyncIterator[bytes]:
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    await upstream.aclose()
                    await sched.release(model)

            return StreamingResponse(
                body_iter(),
                status_code=upstream.status_code,
                headers=resp_headers,
                media_type=upstream.headers.get("content-type"),
            )
        except Exception as e:
            await sched.release(model)
            log.exception("转发失败 model=%s", model)
            return _error(502, f"上游转发失败: {type(e).__name__}: {e}", "upstream_error")

    def _model_from_body(raw: bytes, request: Request) -> str | None:
        if request.query_params.get("model"):
            return request.query_params["model"]
        try:
            return json.loads(raw or b"{}").get("model")
        except Exception:
            return None

    @app.post("/v1/audio/speech")
    async def speech(request: Request) -> Response:
        raw = await request.body()
        model = _model_from_body(raw, request)
        if not model:
            return _error(400, "缺少 model 字段", "invalid_request_error")
        if model not in cfg.models:
            return _error(404, f"未知模型 {model}；可用：{sorted(cfg.models)}", "invalid_request_error")
        return await _passthrough(request, model, "/v1/audio/speech")

    @app.post("/v1/audio/voices")
    async def voices_upload(request: Request) -> Response:
        # multipart 表单里通常带 model；没有就落到卡上第一个模型
        model = request.query_params.get("model") or next(iter(cfg.models), None)
        if not model or model not in cfg.models:
            return _error(400, "缺少或未知的 model", "invalid_request_error")
        return await _passthrough(request, model, "/v1/audio/voices")

    @app.delete("/v1/audio/voices/{name}")
    async def voices_delete(name: str, request: Request) -> Response:
        model = request.query_params.get("model") or next(iter(cfg.models), None)
        if not model or model not in cfg.models:
            return _error(400, "缺少或未知的 model", "invalid_request_error")
        return await _passthrough(request, model, f"/v1/audio/voices/{name}")

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
