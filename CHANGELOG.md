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
