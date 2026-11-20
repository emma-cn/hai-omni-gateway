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

## [0.1.15] - 2026-11-20

### Added
- **`asleep` 与 `absent` 分开（产品要求）**：后台探针每 5s 打一次后端 `/health`，
  `/healthz` 与 `/admin/state` 暴露 `backend_alive`，`/v1/models` 里不可达的模型
  状态报 `absent` 而不是 `asleep`。
  理由：睡眠 = 等 1~2 秒能唤醒；absent = 容器不在/端口不通，永远叫不醒。
  伪装成"睡眠"会让用户点唤醒后白等两分钟。**睡着的后端 `/health` 仍回 200**，
  所以探针不会把睡着误判成不存在。

### Changed
- 管理页模型状态三态显示：醒着 / 睡眠 / **后端不存在**（红色），hover 有说明

### 实测
- pytest：124 passed, 4 xfailed（新增 3 条：可达不误报 / 不可达报 absent / 睡着不报 absent）

## [0.1.14] - 2026-11-20

### Fixed
- **A-602 归因失效（产品实测抓出）**：原来只有一个 `gen_ms`，「发出请求 → 首字节」的等待
  全被贴成"生成"。8 并发长文本时 `gen p95 = 44.3s` 而 `queue_wait` 最大 0.02ms，
  p95/p50 差 62 倍 —— 等于没有归因。
  现在拆成四段，严格不重叠：`queue_wait_ms`（网关等名额）→ `wake_ms`（换台搬显存）
  → **`upstream_wait_ms`（首字节之前，含 vLLM 内部 max_num_seqs 排队与抢占）**
  → `gen_ms`（首字节之后）
- 账本增加 `upstream_waiting` / `upstream_running`：请求发出时上游自己的队列深度

### Added
- **上游排队探针**：每 5s 抓一次 vLLM 的 `/metrics`（`num_requests_waiting/running`），
  结果进 `/metrics`（`omni_upstream_waiting` / `omni_upstream_running`）与账本。
  这是「多少时间是等 max_num_seqs」唯一可证的数据源，不必让后端改代码
- 间隔可配 `upstream_probe_interval_s`（0=关）

### Changed
- **`GET /v1/audio/voices` 不再强制 model**（发现类端点）：不带 model 时汇总全部语音模型的
  音色，每条标 `model`，并回 `sources` 说明哪个模型没取到（后端睡着/挂了不整体失败）。
  "model 必填"是防**生成**时静默落到别的模型，不该管到列表接口。

### 实测（13.1，VoxCPM2，修复后同一手法）
```
8 并发短文本 : total p95 = 0.99s   (upstream_wait p95=0.99s, gen p95=1.4ms)
8 并发长文本 : total p95 = 13.87s  (upstream_wait p95=13.81s, gen p95=62.8ms)
```
结论：等待**全在上游**，网关侧 queue_wait/wake 都是 0。`gen_ms` 现在是首字节后的时间，
对非流式音频只有毫秒级 —— 说明抖动与网关无关。

## [0.1.13] - 2026-11-20

### Fixed
- **#13 管理页在鉴权上线后不可用**：`/ui` 页面壳改为公开（本身不含任何数据），
  页头加 Admin token 输入框、测试面板加可选 API key，凭据存 localStorage，
  所有请求带 `Authorization: Bearer`；401/403 时输入框高亮并提示原因
- **废弃 `?token=`**：URL 里的 token 会进访问日志、浏览器历史、Referer，
  一次误发等于泄露。凭据只走 header。

### Added
- **`GET /metrics`（A-1104，Prometheus 文本格式）**：`omni_up` / `omni_models{model,card}` /
  `omni_inflight{model}` / `omni_ledger_written_total` / `omni_ledger_dropped_total` /
  `omni_ledger_disabled` / `omni_segment_ms{segment,quantile}`（p50/p95，最近窗口）
- **凭据口径统一**：`/admin/*` 与 `/metrics` 都要 admin token；
  `/ui` 只有壳，公开。文档写清，避免"一个要一个不要"

### 关于 #13 报告里的一处矛盾（需产品/测试确认）
原报告要求「`/ui` 保持 401」+「页面里加 token 输入框」。这两条无法同时成立——
401 的页面浏览器渲染不出来，输入框永远没人能填。本版按「壳公开、数据面全要 token」实现，
`/ui` 无 token 现在是 **200**（页面里显示"需要 Admin token"并高亮输入框）。

### 实测
- pytest：118 passed, 4 xfailed（新增 4 条：壳公开/HTML 含凭据支持/admin 与 metrics 收 token/query token 被拒）

## [0.1.12] - 2026-11-20

### Added
- **请求账本（A-601/602）**：每请求一行 JSONL（`ledger.path`，默认 `/var/log/omni/requests.jsonl`），
  按大小轮转（`max_bytes` / `keep`）。字段：`request_id / key / model / path / status / error /
  queue_wait_ms / wake_ms / gen_ms / total_ms / bytes_out`
- **三段耗时分开记**：排队（等执行名额）/ 唤醒（换台+搬显存）/ 生成（模型在算），
  合成一个数字时 p95 一抖就无法归因
- `GET /admin/ledger?limit=N`：最近 N 条 + 三段 p50/p95/max 聚合（无需把 JSONL 拉回本地算）
- 失败请求也进账本（含 `missing_model` / `model_not_found` / `capability_unsupported` / 403），
  只统计成功请求的 p95 会系统性偏低
- `deploy-dev.sh` 把账本目录挂到宿主机 `./logs`，容器重建不丢历史

### Fixed
- **`queue_wait_ms` 曾把唤醒时间算进去**（实测 1209.791 vs wake 1209.772，两段几乎相等，
  归因完全失效）——排队计时在拿到执行名额时截止
- **`admin_token_env` 是空挂配置**：鉴权只读 `cfg.admin_token`，env 注入的 token 一律 401。
  改用 `resolved_admin_token()`；同时清掉 dev 配置里硬编码的 admin token（回滚用 `.bak-*`）
- 账本初始化失败（如 `/var/log` 无权限）不再阻止服务启动：账本是观测设施，
  为记日志而起不来服务比丢日志更糟

### 实测（13.1 真机，VoxCPM2 + IndexTTS）
```
/models/VoxCPM2      200  q_wait=0.016  wake=1215.065  gen=709.023  total=1924.153   ← 冷启
/models/VoxCPM2      200  q_wait=0.008  wake=   0.000  gen=326.588  total= 326.652   ← 稳态
/models/IndexTTS-2.5 400  q_wait=0.011  wake=5801.590  gen=  1.523  total=5803.160   ← 换台
(none)               400  q_wait=0.000  wake=   0.000  gen=  0.000  total=  0.021 missing_model
聚合 count=6 dropped=0
  queue_wait_ms p50=0.006  p95=0.011
  wake_ms       p50=0.0    p95=5801.59
  gen_ms        p50=263.3  p95=343.44
```

## [0.1.11] - 2026-11-20

### Changed
- **429 的 `code` 对齐 OpenAI 官方 `rate_limit_exceeded`**（原为自造的 `rate_limited`）。
  兼容层按 `code` 做退避/降级，自造词会逼客户端写特例。
  我们自己的细分原因放到独立字段 `reason`：`rpm`（该 key 超 `rpm`）或 `queue_full`（排队达上限）。
- `docs/ERRORS.md` 新增《与 OpenAI 官方码的对齐约定》表：会程序化分支的码对齐官方；
  标出三处**有意偏离**（`unauthorized` / `missing_model` / `capability_unsupported`）及理由。

### 实测
- pytest：108 passed, 4 xfailed（用例断言 429 的 `code == "rate_limit_exceeded"` 且 `reason == "rpm"`）

## [0.1.10] - 2026-11-20

### Added
- **可验证的限额路径（A-404b）**：dev 上新增一把 `rpm: 5` 的测试 key，
  通过 `key_env: OMNI_KEY_RPMTEST` 从 `env/dev/gateway.env`（600，已 gitignore）注入；
  `deploy-dev.sh` 支持 `--env-file` 注入密钥，密钥不再写进 `gateway.yaml`
- 429 响应现在也带 `x-ratelimit-{limit,remaining,reset}-requests`（连同 `Retry-After`），
  客户端据此知道额度上限与恢复时间

### 实测（走非 loopback 地址，key rpm=5，连打 6 次）
```
第1..5次  200  limit=5  remaining=4,3,2,1,0  reset=59
第6次     429  retry-after=60  limit=5  remaining=0  reset=59
          {"error":{"type":"rate_limited","code":"rate_limited","message":"超出该 key 的速率上限（5 rpm）"}}
```

## [0.1.9] - 2026-11-20

### Fixed
- **`x-ratelimit-*` 不再发 `0`（A-404 判据改严后暴露）**：不限额时**整组不发**（缺头 = 不限），
  只有配置了 `rpm` 的 key 才发真实 `limit/remaining/reset`。
  理由：OpenAI 语义里 `limit: 0` = "你没有额度"，按 `remaining` 退避的客户端会立刻限流自己
  —— 那组头不传递信息，只传递恐慌。`x-request-id` 仍然永远带。

### 实测
- pytest：108 passed, 4 xfailed（用例改为"不限额不发 + 有限额发真实值且 remaining ≤ limit"）

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
