# Changelog

格式照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号 = `0.1.<build>`，**构建号连续累加、永不回退**（同 [magpie](https://github.com/yetone/magpie) 的做法）。

每条发布必须写清三件事：改了什么 / **跑了什么**（命令与结论）/ 已知问题。

## [Unreleased]

### 计划中（P0，开放产品上线前置）
- 鉴权与多租户：分级鉴权（loopback 免 key、非 loopback 强制）、`Authorization: Bearer` 与
  `x-api-key` 双写法、key 带能力/模型/额度、运维面独立 admin token
- 熔断与自愈：连续唤醒失败熔断、区分"显存不足 / 进程死 / 上游拒参数"、后端进程重启钩子
- 开放契约：错误体 `{message,type,param,code}`（`code` 为机器码）、全响应限流头、404 自描述、
  `/v1/models` 变能力目录
- 长任务租约：job 表 + `lease_s` 记账、`Idempotency-Key` 去重
- 请求账本：每请求一行 JSONL，**排队/唤醒/生成三段耗时分开记**

## 版本号使用规约（重要）

- **正式号 `x.y.z` 只能由 tag 产生**（`scripts/release.sh` → `release.yml`）。tag 指向
  `chore(release): 版本号 x.y.z` 那个提交，`/healthz` 的 `build` 必须等于该 tag。
- **dev 构建不得占用正式号**：`scripts/deploy-dev.sh` 会把形如 `x.y.z` 的输入自动改成
  `x.y.z-dev-<sha>`。起因：曾出现本地镜像 tag `0.1.2`（构建于 bump 提交之前）与
  GitHub 上的 `v0.1.2`（bump 提交）内容不同却同号，即"同号不同内容"。
- 已发布各号的内容：`v0.1.0 = 6f4131c7`、`v0.1.1 = a8bad3f5`、`v0.1.2 = d534ebca`、`v0.1.3 = 07a03783`。

## [0.1.8] - 2026-11-20

### Added
- **能力目录（P0-4 / A-403~A-408）**
  - `GET /v1/models` 变能力目录：`modalities`（由 capabilities 推导）、`languages`、
    `max_input_chars`、`sample_rates`、`formats`、`latency_class`、`declared`
  - `declared` 显式区分"没填"与"不支持"——能力目录一旦会撒谎，客户端就没法据此分支
  - 404 自描述：未知路径返回 `not_found` + 端点族清单（顺带收掉 issue #10 的 `/api/events`）
  - 所有响应带 `x-request-id`（客户端给了就沿用）、`x-ratelimit-{limit,remaining,reset}-requests`

### 说明
- 目录字段只输出**真的知道**的值：语种/采样率/格式未配置就是空数组 + `declared=false`，
  不编造"支持 N 种语言"这类数字
- `x-ratelimit-limit-requests: 0` 表示不限（loopback 或无 key 内部调用）

### 实测
- pytest：108 passed, 4 xfailed（新增 404 自描述 / 响应头 / 能力目录三条）

## [0.1.7] - 2026-11-20

### Security
- **修 `X-Forwarded-For` 绕过鉴权（issue #12，测试实测提出）**
  - 旧实现取 XFF 的**最左段**——而 nginx 的 `$proxy_add_x_forwarded_for` 是把真实客户端
    **追加在右边**，最左边那段是客户端自己填的、可任意伪造；
  - `trusted_proxies` 旧默认含 `127.0.0.1`，于是本机任何进程都能冒充"来自本机"。
  - 现在：① 只有 peer 本身在 `trusted_proxies` 里才看 XFF；② 看 XFF 时**从右往左**
    跳过可信代理，第一个不可信地址才算真实客户端；③ `trusted_proxies` **默认空**。

### 实测（真机，非 loopback 地址 + 伪造头）
```
LAN 直连无 key                   -> 401
LAN + X-Forwarded-For: 127.0.0.1 -> 401
LAN + XFF: 127.0.0.1, 127.0.0.1   -> 401
LAN + 正确 key（对照）            -> 200
loopback 直连（对照）             -> 200
```
- pytest：105 passed, 4 xfailed（新增 `test_a106_spoofed_xff_cannot_bypass_key`）

### 说明
- 若你确实在网关前放了反代，需要把反代地址填进 `auth.trusted_proxies`，
  否则所有请求都会被当成"来自反代地址本身"。

## [0.1.6] - 2026-11-20

### Changed
- **运维面默认不再信任 loopback**：`/admin/*`、`/ui`、`/metrics` 永远需要 admin token
  （`auth.admin_loopback_bypass` 默认 `false`）。理由：本机其它进程、SSH 隧道、浏览器插件
  都能发 loopback 请求，一个匿名 `POST /admin/sleep` 就能把模型睡掉。
  验收 A-102/A-103 正是从 127.0.0.1 打的，就是这个口径。
- `GET /ui?token=<admin token>` 支持浏览器场景（只对运维面生效）

### 实测
- 验收矩阵：**A-101 / A-102 / A-103 / A-104 全 PASS**；总 PASS 15（原 13）
- pytest：104 passed, 4 xfailed

## [0.1.5] - 2026-11-20

### Added
- **鉴权与多租户（P0-1 / A-101~A-104）**
  - 分级：loopback 免 key；非 loopback 必须有 key；运维面（`/admin/*`、`/ui`、`/metrics`）用独立 admin token
  - `Authorization: Bearer` 与 `x-api-key` 双写法
  - key 能力边界：`capabilities` / `models` / `rpm` / `expires_at`
  - **fail closed**：listen 非 loopback 且缺 key 或 admin token → **拒绝启动**
  - 鉴权在**路由匹配之前**执行（修 A-103：以前"未知模型 404"会先于"无凭据 401"，
    等于把模型名存在性泄露给未鉴权客户端）
  - 反代场景：只有 peer 属于 `trusted_proxies` 才采纳 `X-Forwarded-For`，
    否则任何人都能伪造 `X-Forwarded-For: 127.0.0.1` 绕过鉴权
- 错误码统一为**机器码**（`code` 不再塞 HTTP 数字），新增 `unauthorized` / `forbidden` / `rate_limited`
- 缺 `model` 一律 400 `missing_model`（**不再猜唯一候选**）；multipart 表单里的 `model` 也能取到

### 实测（真机，走非 loopback 的局域网地址）
- 无 key → 401；带正确 key → 200
- `/admin/state` 无凭据 → 401；带 admin token → 200
- `/admin/sleep` 无凭据 + 不存在的模型 → **401**（不是 404，确认鉴权先于参数校验）
- 错误 key → 401 且响应体里没有任何模型名
- loopback 免 key 仍可用（本机调试不受影响）

### 已知取舍
- `daily_chars` / `daily_audio_seconds` 类日额度未实现：要账本（P0-6）才能算准，
  先加字段只会变成空挂配置（AGENTS 规则 1）。

## [0.1.4] - 2026-11-20

### Added
- **`GET /v1/omni/status` 消费（A-702 / 免对账）**：启动对账改为先读后端权威状态
  （每 stage sleeping + 整卡显存），读到就**只读对齐**、不再"盲睡一遍"；
  后端是老镜像（404）时才回退盲睡。盲睡的代价是对其实醒着、还有在飞请求的模型发 sleep，
  等于掐掉用户请求。
- 对账发现同卡多个模型同时醒着时记 `fail` 事件并打 ERROR 日志（显存可能已超卖）

### 实测
- pytest：94 passed, 4 xfailed（新增两条：读状态不盲睡 / 老后端回退盲睡）

## [0.1.3] - 2026-11-20

### Added
- **失败三分类（P0-3 前置）**：`backend_unavailable` → 502、`wake_oom` → 503 + Retry-After、
  `bad_request` → 400。此前一律 `wake_failed` 503，把"进程已死"和"显存不够"混为一谈
- 后端"进程级不可用"15s 冷却：冷却期内直接快速失败，不再反复唤醒死进程

### 实测
- 真机：停掉 voxcpm2 后端后，第一个请求 **502 `backend_unavailable`（5.6 ms）**；
  第二个请求命中冷却 **502（0.5 ms）**，未再打后端；恢复后端 50s 后自动可用
- pytest：92 passed, 4 xfailed

## [0.1.2] - 2026-11-20

### Fixed
- `/admin/wake`、`/admin/sleep`、`/admin/reconcile` 在换台失败时抛 `SleepFailed` /
  `DrainTimeout` / `QueueFull` / `RequestTimeout` 会变成 **500**（未捕获）——
  500 意味着"网关有 bug、别重试"，与真实语义（稍后重试即可）相反。现统一 503 + `Retry-After`
- 空闲回收失败后每 5s 重试一次，把后端打成 `ReadError` 风暴、日志刷屏并掩盖真因；
  现在失败后 30s 内不再重试

### 实测（配合后端仓 B4）
- 后端 `--enable-sleep-mode` 缺失时，`/v1/omni/sleep` 回 200 + `freed_bytes=0` 但**显存不释放**，
  网关会误以为换台成功（实测 20 轮"换台"显存恒为 20.3 GiB）。补齐 flag 后同一条 sleep
  由 4ms 变 2.8s、显存 20905 → 12159 MiB
- swap 共存档 20 轮轮流醒：wake 全 200、零 OOM、容器重启计数 0→0

## [0.1.1] - 2026-11-20

### Added
- **有界 FIFO 队列（P0-2 / A-201~A-203）**：`Card.acquire()` 不再 20ms 忙等轮询，改为
  `asyncio.Condition` + 每模型 ticket 队列，先到先得（防长任务饿死）
- 队列满 → `429 queue_full` + `Retry-After: 2`；排队超过 `request_timeout_s` →
  `503 request_timeout` + `Retry-After: 5`（此前都是无限等，真实超时只有全局 read=1800s）
- `request_timeout_s` 现在也作用到**在飞请求**（httpx 逐请求超时，connect 固定 5s）

### Changed
- `queue_limit` 语义写进示例配置：**允许排队（等待）的请求数上限**，`0` = 不排队；
  进系统总量 = 模型 `concurrency` + `queue_limit`（与验收 A-203 的 4+16=20 口径一致）
- `acquire()` 返回排队等待毫秒数，为 P0-6 账本的 `queue_wait_ms` 备好埋点

### Fixed
- 取消/异常路径不再泄漏在飞名额（原先排队请求被取消后名额不减，最终把该模型堵死）

### 实测
- `ruff check src scripts`：All checks passed
- `pytest`：88 passed, 4 xfailed（新增 `test_queue_full_raises_when_limit_reached`、
  `test_queue_wait_times_out`、`test_queue_is_fifo`；两条 429 验收用例去掉 xfail 后真绿）
- 未在真后端压测 32 并发（需实卡，留给产品侧 `accept.sh --heavy` 复核）

## [0.1.0] - 2026-11-20

### Added
- OpenAI 兼容入口：`/v1/audio/speech`、`/v1/audio/transcriptions`、`/v1/audio/voices`、
  `/v1/images/*`、`/v1/videos*`；按 endpoint 的能力匹配模型 `capabilities`
- 卡级单写者调度：一张卡同时只醒一个模型，`min_dwell_s` / `drain_timeout_s` / `ttl_s` / `concurrency`
- 两段式唤醒 `wakeup + resume + 健康检查`（缺 `resume` 会让请求永久挂在准入等待）
- 管理页 `/ui` 与 `/admin/{state,wake,sleep}`
- 启动对账：把所有后端睡一遍再归零，避免"以为都睡着、实际有模型醒着"导致唤醒 OOM
- 调度正确性：drain 未在 `drain_timeout_s` 内清零时**拒绝换台**，返回 503 `drain_timeout` + `Retry-After`
  （上游规则：暂停未完成不许 sleep；旧实现打完 warning 继续换台，会掐掉在飞请求）
- 工程治理：`AGENTS.md` / `LESSONS.md` / `SECURITY.md` / PR 与 issue 模板
- 敏感信息闸门 `scripts/secret_scan.sh`：内网 IP、内网域名、密钥前缀、绝对路径命中即红，支持 `--history`
- CI 三段：闸门 → lint+冒烟（py3.11/3.12）→ 镜像构建
- 发版机制：`v0.1.<build>` 连续累加、tag 上重跑全量验证、镜像 tag == `/healthz` 的 `build` 字段、
  Release 正文取自本 CHANGELOG（`scripts/release.sh` / `release.yml` / `RELEASING.md`）

### Known issues
- 无鉴权：`/v1/*`、`/admin/*`、`/ui` 均无凭据可访问
- `queue_limit`、`request_timeout_s` 声明但未生效；`lease_s` 仅展示、无记账
- `GET /v1/audio/voices` 未透传（上游直连 200，网关 405）
- `X-Omni-Wake-ETA` 在 README 承诺、代码未实现
