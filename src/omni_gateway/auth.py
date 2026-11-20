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
PUBLIC_PATHS = frozenset({
    "/healthz",
    "/docs",
    "/openapi.json",
    "/redoc",
    # /ui 是**页面壳**：只有一个 token 输入框和几处 fetch，本身不含任何数据。
    # 如果把它一起挡住，浏览器根本打不开页面，"在页面里填 token"就无从谈起
    # （issue #13 的原始矛盾）。所以壳公开、数据面全部要 token。
    "/ui",
})

# 运维面：需要 admin token（或 loopback）
# 运维面：数据接口 + 指标。`/ui` 不在其中（见 PUBLIC_PATHS 的说明）。
ADMIN_PREFIXES = ("/admin", "/metrics")


class AuthError(Exception):
    """鉴权失败。`status` 是给客户端看的 HTTP 码，`code` 是机器码。"""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        rate: tuple[int, int, int] | None = None,
        reason: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        # 细分原因（如 rpm / queue_full），作为额外字段返回，不进 code
        self.reason = reason
        # (limit, remaining, reset) —— 429 时带上，否则客户端不知道何时能重试
        self.rate = rate


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
    def effective_client_ip(self, peer: str | None, xff: str | None) -> str:
        """推算出"真实客户端地址"，只信自己代理写的那一段。

        规则（这是 X-Forwarded-For 的标准用法，取错一段就是漏洞）：
          1. 只有 **peer 本身在 trusted_proxies 里**（即请求确实来自我们自己的反代）
             才看 XFF；否则 XFF 完全忽略，直接用 peer。
          2. 看 XFF 时**从右往左**走，跳过可信代理，第一个不可信地址才是真实客户端。
             因为 nginx 的 `$proxy_add_x_forwarded_for` 是**追加**在右边，
             最左边那段是客户端自己填的、可以随便伪造。

        历史 bug（issue #12）：早前取 `xff.split(",")[0]`（最左段）+ `trusted_proxies`
        默认含 127.0.0.1，于是任意客户端带 `X-Forwarded-For: 127.0.0.1` 就能免 key 通过。
        """
        peer = (peer or "").strip()
        trusted = set(getattr(self.cfg, "trusted_proxies", None) or ())
        if not trusted or peer not in trusted:
            return peer
        hops = [h.strip() for h in (xff or "").split(",") if h.strip()]
        if not hops:
            return peer
        for hop in reversed(hops):
            if hop in trusted:
                continue
            return hop
        # 整条链都是可信代理（极端情况）：退回最左段
        return hops[0]

    def client_is_loopback(self, peer: str | None, xff: str | None) -> bool:
        """判定"这个请求是不是真的来自本机"。"""
        client = self.effective_client_ip(peer, xff)
        return client in set(getattr(self.cfg, "loopback_peers", None) or ("127.0.0.1", "::1", "localhost"))

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

        # 2) 运维面：默认**永远**要 admin token（loopback 也不例外，除非显式放开）
        if is_admin_path:
            if loopback and getattr(self.cfg, "admin_loopback_bypass", False):
                return Identity(name="admin-loopback", is_admin=True, is_loopback=True)
            # 必须用 resolved_admin_token()：它会把 admin_token_env 指向的环境变量读进来。
            # 直接读 self.cfg.admin_token 会让 `admin_token_env` 变成空挂配置
            # （实测：env 里给了 OMNI_ADMIN_TOKEN，请求仍被判 401）。
            admin_token = self.cfg.resolved_admin_token()
            if not admin_token:
                raise AuthError(
                    401, "unauthorized",
                    "运维面需要 admin token，但服务端未配置（服务以 fail-closed 方式拒绝）",
                )
            if presented and presented == admin_token:
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
            # code 对齐 OpenAI 官方（`rate_limit_exceeded`）：按 code 做退避/降级的
            # 兼容层认的就是这个字面量，自造词会逼客户端写特例。
            # 我们自己的细分原因放 `reason`，不丢信息也不破坏兼容。
            raise AuthError(
                429,
                "rate_limit_exceeded",
                f"超出该 key 的速率上限（{key.rpm} rpm）",
                rate=(key.rpm, 0, bucket.retry_after(now)),
                reason="rpm",
            )
        return Identity(name=key.name, key=key)

    # ------------------------------------------------------------------ 配额
    def retry_after(self, name: str) -> int:
        bucket = self._buckets.get(name)
        return bucket.retry_after(self._now()) if bucket else 1

    def rate_snapshot(self, identity: Identity | None) -> tuple[int, int, int]:
        """返回 (limit, remaining, reset_seconds) 供 x-ratelimit-* 响应头使用。

        limit=0 表示**不限**（loopback 或无 key 的内部调用）。
        为什么把"不限"编码成 0 而不是省略头：客户端拿到缺失的头只能当"不知道"，
        而 0 有明确含义（见 docs/ERRORS.md），可据此决定要不要退避。
        """
        key = getattr(identity, "key", None)
        if key is None or key.rpm <= 0:
            return 0, 0, 0
        bucket = self._buckets.get(key.name)
        if bucket is None:
            return key.rpm, key.rpm, 0
        now = self._now()
        window_start = now - 60.0
        bucket.hits = [t for t in bucket.hits if t >= window_start]
        remaining = max(0, key.rpm - len(bucket.hits))
        reset = bucket.retry_after(now) if bucket.hits else 0
        return key.rpm, remaining, reset


def loopback_bypass(cfg) -> bool:
    return bool(getattr(cfg, "loopback_bypass", True))
