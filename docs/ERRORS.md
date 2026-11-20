# 错误码契约

对外只承诺这一张表。**`code` 是机器码，不是 HTTP 状态**——客户端应该按 `code` 分支，
`status` 只用来给人类看。

## 失败分类（failures）

| `code` | HTTP | 含义 | 客户端该做什么 |
|---|---|---|---|
| `backend_unavailable` | 502 | 后端连不上 / 进程已死 | 退避后重试；后端会自行重启，网关在冷却期内不会反复唤醒它 |
| `wake_oom` | 503 | 唤醒时显存不足（含"唤醒命令成功但引擎没起来"） | 按 `Retry-After` 重试 |
| `bad_request` | 400 | 上游拒绝参数 | **不要重试**：改请求 |
| `wake_failed` | 503 | 其他唤醒失败（未分类） | 按 `Retry-After` 重试 |
| `sleep_failed` | 503 | 旧模型睡不下去，已中止换台 | 按 `Retry-After` 重试（此时**不会**硬换台） |
| `drain_timeout` | 503 | 换台前在飞请求没能在超时内清零，已拒绝换台 | 按 `Retry-After` 重试 |
| `queue_full` | 429 | 排队人数达到 `queue_limit` | 退避；不要继续排队 |
| `request_timeout` | 503 | 排队超过 `request_timeout_s` 仍没拿到执行名额 | 可安全重试（请求根本没发到上游） |
| `upstream_error` | 502 | 转发给上游时出现未预期的传输错误 | 按 5xx 处理 |
| `model_not_found` | 404 | `model` 不在配置里 | 不要重试；检查 `model` 字段 |
| `capability_unsupported` | 400 | 该模型没有声明这个接口需要的能力 | 不要重试；换个模型或换接口 |
| `missing_model` | 400 | 请求里没有 `model` | 补上 `model`（开放产品**不猜**候选） |
| `not_found` | 404 | 没有这个端点（响应体会一并列出端点族，供客户端自解） | 检查路径或版本 |
| `unauthorized` | 401 | 缺 key / key 无效或过期 / 运维面缺 admin token | 补正确凭据；**不要重试** |
| `forbidden` | 403 | key 有效，但该 key 不被允许用这个模型或能力 | 换模型/能力，或申请授权 |
| `rate_limited` | 429 | 超出该 key 的 `rpm` | 按 `Retry-After` 退避 |

响应体形状（除二进制音频流本身）：

```json
{"error": {"message": "人话描述", "type": "机器码", "code": "同 type"}}
```

> 历史包袱：`type` 与 `code` 目前同值（早期 `code` 塞的是 HTTP 数字，已废弃）。
> 客户端应只依赖 `type`/`code` 的字符串值，不要依赖 HTTP 状态做分支。

## 限流与追踪响应头（所有响应都带，含错误响应）

| 头 | 含义 |
|---|---|
| `x-request-id` | **永远带**。客户端传了就沿用（便于跨服务串联），没传则服务端生成 |
| `x-ratelimit-limit-requests` | 该 key 每分钟请求上限。**只在真的有限额时才发**；缺头 = 不限 |
| `x-ratelimit-remaining-requests` | 当前窗口剩余，恒 `≤ limit` |
| `x-ratelimit-reset-requests` | 距离窗口重置的秒数 |

**为什么不发 `limit: 0`**：OpenAI 语义里 0 表示"你没有额度"，按 `remaining` 做退避的
客户端会立刻限流自己。缺头（=不限）比假头安全，OpenAI 本身也不强制发这组头。

## `Retry-After`

凡 429 / 502 / 503 都带 `Retry-After`（秒）。**没有 `Retry-After` 的 5xx 视为不可重试**。

## 控制面（`/admin/*`）与后端状态

| 端点 | 说明 |
|---|---|
| `GET /admin/state` | 每张卡当前醒着的模型、在飞计数、最近事件环 |
| `POST /admin/wake` `{"model": "..."}` | 换台到该模型（会先睡同卡其它模型） |
| `POST /admin/sleep` `{"model": "..."}` | 睡下指定模型（不看本地状态，幂等补发） |
| `POST /admin/reconcile` | 把网关状态与后端真实状态对齐（读后端 `GET /v1/omni/status`） |

后端控制面（**vllm-omni 实例自身**，非网关）：

| 端点 | 说明 |
|---|---|
| `POST /v1/omni/sleep` `{"stage_ids": [...], "level": 1}` | 释放显存；**需要后端带 `--enable-sleep-mode`**，否则返回 200 但一 MiB 不放 |
| `POST /v1/omni/wakeup` | 权重搬回显存 |
| `POST /v1/omni/resume` | 重开前端准入闸门（上游缺失，由本仓补丁提供） |
| `GET /v1/omni/status` | 每 stage 睡眠状态 + 整卡显存（上游缺失，由本仓补丁提供） |

## 已知的"假成功"（复测必看）

1. **`sleep` 返回 200 不等于显存释放**：后端缺 `--enable-sleep-mode` 时 `freed_bytes: 0` 且显存不动。
   验证方法：`nvidia-smi` 前后对比，或读 `GET /v1/omni/status` 的 `vram.used_mb`。
2. **`freed_bytes` 不可作为判据**：带 flag 时它是 0，但不带 flag 时显存也不释放——两个方向都会误导。
   唯一可信判据是 `vram.used_mb` 的变化。
