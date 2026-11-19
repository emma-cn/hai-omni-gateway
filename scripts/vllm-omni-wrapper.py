#!/usr/bin/env python3
"""vllm-omni-wrapper —— vllm-wrapper 的 vllm-omni 版等价物

为什么需要它：
  llama-swap 自带的 vllm-wrapper 硬编码调 vLLM 原生的 /sleep 和 /wake_up，
  而 vllm-omni 只暴露 /v1/omni/sleep 和 /v1/omni/wakeup（实测 /sleep=404），
  且 vllm-omni 睡醒后必须再调 /v1/omni/resume，否则前端准入闸门关着，
  请求会永久挂住。

接口（与 vllm-wrapper 对齐，llama-swap 只认这个形态）：
  serve  <-- 作为模型的 cmd：唤醒 + resume + 等健康 + 在 --listen 上做反向代理（长驻）
  sleep  <-- 作为模型的 cmdStop：调 /v1/omni/sleep 让后端睡下，然后退出

关键点：
  * serve 必须长驻（llama-swap 用 cmd 进程的生死判断模型是否已加载）
  * serve 监听 ${PORT}，llama-swap 的 proxy 指向它，请求经它转发给 vLLM
  * 唤醒要两段：/v1/omni/wakeup 把权重换回显存，/v1/omni/resume 重开前端准入闸门
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ---------------------------------------------------------------- HTTP 小工具
def _post_json(url: str, payload: dict, timeout: float = 120.0) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:  # 连接失败等
        return 0, f"{type(e).__name__}: {e}"


def _get(url: str, timeout: float = 5.0) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _parse_stage_ids(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip() != ""]


# ---------------------------------------------------------------- 反向代理
class _Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = ""          # 由 make_proxy 注入，形如 http://127.0.0.1:60589
    quiet = True

    def log_message(self, fmt, *args):  # 默认日志太吵
        if not self.quiet:
            sys.stderr.write("[proxy] " + (fmt % args) + "\n")

    def _forward(self, method: str):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        url = self.upstream + self.path
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "connection", "content-length")}
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                payload = r.read()
                status = r.status
                rh = r.headers
        except urllib.error.HTTPError as e:
            payload = e.read()
            status = e.code
            rh = e.headers
        except Exception as e:
            msg = f"upstream error: {type(e).__name__}: {e}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        self.send_response(status)
        for k, v in rh.items():
            if k.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        # 强制短连接：llama-swap 卸载模型前要等在途请求清零，keep-alive 的空闲
        # 连接会让它以为还有在途流量，于是一直拖到 5 分钟超时（实测 180s 才换台）。
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):    self._forward("GET")
    def do_POST(self):   self._forward("POST")
    def do_PUT(self):    self._forward("PUT")
    def do_DELETE(self): self._forward("DELETE")
    def do_PATCH(self):  self._forward("PATCH")


def make_proxy(upstream: str, host: str, port: int) -> ThreadingHTTPServer:
    cls = type("Proxy", (_Proxy,), {"upstream": upstream})
    return ThreadingHTTPServer((host, port), cls)


# ---------------------------------------------------------------- serve / sleep
def cmd_serve(a) -> int:
    stages = _parse_stage_ids(a.stage_ids)
    base = a.vllm_url.rstrip("/")

    print(f"[wrapper] wake: POST {base}/v1/omni/wakeup stage_ids={stages}", flush=True)
    code, txt = _post_json(f"{base}/v1/omni/wakeup", {"stage_ids": stages})
    print(f"[wrapper]   -> http={code} body={txt[:200]}", flush=True)
    if code not in (200, 201):
        print("[wrapper] wake failed", file=sys.stderr, flush=True)
        return 1

    print(f"[wrapper] resume: POST {base}/v1/omni/resume stage_ids={stages}", flush=True)
    code, txt = _post_json(f"{base}/v1/omni/resume", {"stage_ids": stages})
    print(f"[wrapper]   -> http={code} body={txt[:200]}", flush=True)
    if code not in (200, 201):
        print("[wrapper] resume failed", file=sys.stderr, flush=True)
        return 1

    # 等健康
    deadline = time.time() + a.wait_timeout
    while time.time() < deadline:
        c = _get(base + a.health_path)
        if c == 200:
            print(f"[wrapper] health ok after wake ({base}{a.health_path})", flush=True)
            break
        time.sleep(1)
    else:
        print("[wrapper] health check timed out", file=sys.stderr, flush=True)
        return 1

    host, _, port = a.listen.rpartition(":")
    srv = make_proxy(base, host or "127.0.0.1", int(port))
    print(f"[wrapper] proxying {a.listen} -> {base}", flush=True)

    def _stop(signum, frame):
        # llama-swap 卸载模型 = 给 cmd 进程发 SIGTERM，然后最多等 5 分钟才 SIGKILL。
        # 所以这里必须"立刻退出"，绝不能走优雅关闭（会被 keep-alive 连接卡住，
        # 实测会把一次换台拖到客户端超时）。顺手做一次 best-effort 的睡眠，
        # 保证即使 llama-swap 没调 cmdStop，权重也会还回去。
        print(f"[wrapper] signal {signum}: best-effort sleep then hard exit", flush=True)
        try:
            _post_json(f"{base}/v1/omni/sleep",
                       {"stage_ids": stages, "level": a.level}, timeout=15)
            print("[wrapper] sleep ok before exit", flush=True)
        except Exception as e:
            print(f"[wrapper] sleep on exit failed: {e}", file=sys.stderr, flush=True)
        os._exit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    srv.serve_forever()
    return 0


def cmd_sleep(a) -> int:
    stages = _parse_stage_ids(a.stage_ids)
    base = a.vllm_url.rstrip("/")
    print(f"[wrapper] sleep: POST {base}/v1/omni/sleep stage_ids={stages} level={a.level}", flush=True)
    code, txt = _post_json(f"{base}/v1/omni/sleep",
                           {"stage_ids": stages, "level": a.level}, timeout=180)
    print(f"[wrapper]   -> http={code} body={txt[:200]}", flush=True)
    return 0 if code in (200, 201) else 1


def main() -> int:
    p = argparse.ArgumentParser(prog="vllm-omni-wrapper")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="wake+resume the omni server, then proxy (long-lived)")
    s.add_argument("--vllm-url", required=True)
    s.add_argument("--listen", required=True, help="host:port to proxy on, e.g. :5801")
    s.add_argument("--stage-ids", default="0")
    s.add_argument("--health-path", default="/health")
    s.add_argument("--wait-timeout", type=float, default=300.0)
    s.add_argument("--level", type=int, default=1, help="sleep level used on SIGTERM exit")

    t = sub.add_parser("sleep", help="put the omni server to sleep")
    t.add_argument("--vllm-url", required=True)
    t.add_argument("--stage-ids", default="0")
    t.add_argument("--level", type=int, default=1)
    t.add_argument("--stop-pid", default=None, help="accepted for CLI compatibility")

    a = p.parse_args()
    return cmd_serve(a) if a.cmd == "serve" else cmd_sleep(a)


if __name__ == "__main__":
    sys.exit(main())
