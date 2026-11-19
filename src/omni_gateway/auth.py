"""鉴权与配额：分级的 key 校验、按 key 的 rpm 限流、来源判定。

设计取舍（都来自踩过的坑，不是理论）：

1. **默认 fail closed**：非 loopback 监听 + 没配任何 key/admin token → 拒绝启动。
   理由：一个"忘记配 key"的部署，最坏的失败方式不是 500，而是"看起来正常、谁都能用"。
   宁可起不来，也不要静默裸奔。

2. **loopback 免 key 必须只信真 loopback**：前面有 nginx/反代时，所有请求的
   peer 地址都是 127.0.0.1 —— 直接拿 `request.client.host` 判免 key 等于全网放行。
   所以只有「peer 在 trusted_proxies 里」才采纳 `X-Forwarded-For` 的第一个地址，
   否则用 peer 地址本身。

3. **鉴权必须在路由参数校验之前**：A-103 曾因为"未知模型 404"先于"无凭据 401"返回，
   等于把"某个模型名存不存在"泄露给未鉴权客户端。所以这里做成 ASGI 级中间件
   （在 FastAPI 路由匹配之前执行），而不是依赖注入。

4. **两种写法都收**：`Authorization: Bearer <key>` 与 `x-api-key: <key>`。
   前者是 OpenAI 生态惯例，后者是老项目/浏览器调试方便。

5. **错的 key 一律 401，且不返回任何模型/配置信息**（A-104）。区分"key 不存在"
   与"key 过期"只会帮攻击者探测，对外统一成 `unauthorized`。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import KeyConfig

# 公开路径：健康检查与文档。这些不带任何业务信息，探活用。
PUBLIC_PATHS = frozenset({"/healthz", "/docs", "/openapi.json", "/redoc"})

# 运维面：需要 admin token（或 loopback）
ADMIN_PREFIXES = ("/admin", "/ui", "/metrics")


class AuthError(Exception):
    """鉴权失败。`status` 是给客户端看的 HTTP 码，`code` 是机器码。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class Identity:
    """一次请求解析出来的调用方身份。"""

    name: str
    key: KeyConfig | None = None
    is_admin: bool = False
    is_loopback: bool = False

    def allows_model(self, model: str) -> bool:
        if self.key is None or not self.key.models:
            return True
        return model in self.key.models

    def allows_capability(self, capability: str) -> bool:
        if self.key is None or not self.key.capabilities:
            return True
        return capability in self.key.capabilities


@dataclass
class _Bucket:
    """一个 key 的滑动窗口记账。"""

    hits: list[float] = field(default_factory=list)

    def allow(self, rpm: int, now: float) -> bool:
        if rpm <= 0:
            return True
        window_start = now - 60.0
        self.hits = [t for t in self.hits if t >= window_start]
        if len(self.hits) >= rpm:
            return False
        self.hits.append(now)
        return True

    def retry_after(self, now: float) -> int:
        if not self.hits:
            return 1
        return max(1, int(60 - (now - self.hits[0])))


class Authenticator:
    """把一次请求的 (peer, headers) 解析成 Identity。"""

    def __init__(self, auth_cfg, *, now=time.monotonic) -> None:
        self.cfg = auth_cfg
        self._now = now
        self._buckets: dict[str, _Bucket] = {}

    # ------------------------------------------------------------- 来源判定
    def client_is_loopback(self, peer: str | None, xff: str | None) -> bool:
        """判定"这个请求是不是真的来自本机"。

        只有 peer 属于 trusted_proxies 时才采纳 X-Forwarded-For——
        否则任何客户端都能伪造 `X-Forwarded-For: 127.0.0.1` 绕过鉴权。
        """
        peer = (peer or "").strip()
        if peer in self.cfg.trusted_proxies and xff:
            peer = xff.split(",")[0].strip()
        return peer in set(getattr(self.cfg, "loopback_peers", None) or ("127.0.0.1", "::1", "localhost"))

    # ------------------------------------------------------------- key 提取
    @staticmethod
    def extract_key(headers) -> str | None:
        auth = headers.get("authorization") or headers.get("Authorization")
        if auth:
            parts = auth.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                return parts[1].strip()
            # 兼容直接放裸 key 的老客户端
            if len(parts) == 1:
                return parts[0].strip()
        raw = headers.get("x-api-key") or headers.get("X-Api-Key")
        return raw.strip() if raw else None

    # -------------------------------------------------------------- 主入口
    def authenticate(self, path: str, peer: str | None, headers) -> Identity:
        loopback = self.client_is_loopback(peer, headers.get("x-forwarded-for"))
        is_admin_path = path.startswith(ADMIN_PREFIXES)
        presented = self.extract_key(headers)

        # 1) 公开路径
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return Identity(name="public", is_loopback=loopback)

        # 2) 运维面：admin token 或 loopback
        if is_admin_path:
            if loopback and loopback_bypass(self.cfg):
                return Identity(name="admin-loopback", is_admin=True, is_loopback=True)
            if not self.cfg.admin_token:
                raise AuthError(
                    401, "unauthorized",
                    "运维面需要 admin token，但服务端未配置（服务以 fail-closed 方式拒绝）",
                )
            if presented and presented == self.cfg.admin_token:
                return Identity(name="admin-token", is_admin=True)
            raise AuthError(401, "unauthorized", "运维面需要 admin token")

        # 3) 业务面
        if loopback and loopback_bypass(self.cfg):
            return Identity(name="loopback", is_loopback=True)

        if not self.cfg.keys:
            # 非 loopback 且没有配置任何 key：fail closed
            raise AuthError(401, "unauthorized", "服务未配置任何 API key（fail-closed）")

        if not presented:
            raise AuthError(401, "unauthorized", "缺少 API key（Authorization: Bearer 或 x-api-key）")

        key = self.cfg.keys_by_value.get(presented)
        # 不区分"不存在"与"过期"：都返回同一个 401，避免帮攻击者探测
        if key is None or key.is_expired():
            raise AuthError(401, "unauthorized", "API key 无效或已过期")

        bucket = self._buckets.setdefault(key.name, _Bucket())
        now = self._now()
        if not bucket.allow(key.rpm, now):
            raise AuthError(
                429, "rate_limited", f"超出该 key 的速率上限（{key.rpm} rpm）"
            )
        return Identity(name=key.name, key=key)

    # ------------------------------------------------------------------ 配额
    def retry_after(self, name: str) -> int:
        bucket = self._buckets.get(name)
        return bucket.retry_after(self._now()) if bucket else 1


def loopback_bypass(cfg) -> bool:
    return bool(getattr(cfg, "loopback_bypass", True))
