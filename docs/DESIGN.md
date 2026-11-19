# 设计：卡级调度与请求队列

## 1. 问题定义

vllm-omni 的硬约束：
- **一个容器只能跑一条 pipeline**（deploy config 单文件，`serving_speech` 在 init 时定死模型类型）。
- 一张 24GB 卡装不下两个醒着的大模型：实测 indextts 14.9 GiB、voxcpm2 17.3 GiB。
- 睡着的服务仍占显存：indextts 1.07 GiB、voxcpm2 1.8 GiB（CUDA context + 池外分配）。

所以「共用卡」只能是**时间片**：同一时刻一个醒着，其余睡着。

## 2. 状态机（每张卡一个，单写者）

```
            acquire(model)                    sleep 成功
  IDLE/AWAKE ──────────────► SWITCHING ──────────────────► AWAKE(model)
      ▲                          │                              │
      │  drain 超时/唤醒失败       │ ttl 到期 & 无在飞            │
      └──────────────────────────┴──────────────────────────────┘
```

- **单写者**：只有 `Card._switch_to()` / `Card.sleep_if_idle()` 能动 sleep/wake，二者都在 `Card._lock` 内。
  API 层只能通过 `acquire()` / `release()` 间接驱动 → 不可能出现两个换台交叉执行。
- **drain**：换台前先让当前模型的在飞请求清零（上限 `drain_timeout_s`）。
  **不能 abort**——`sleep(mode="abort")` 会掐掉用户请求，公网服务不可接受。
- **min_dwell**：刚醒的模型至少驻留 `min_dwell_s` 才允许被挤掉。按换台成本 4~5s 计，
  20s 驻留能把换台开销压在 25% 以内，并避免 A/B 交替导致的抖动。

## 3. 唤醒必须两段（这是最容易踩的坑）

```
POST /v1/omni/wakeup   {"stage_ids":[0,1]}      # 权重从 CPU 内存换回显存，1.3~1.9s
POST /v1/omni/resume   {"stage_ids":[0,1]}      # 重开前端准入闸门，~3ms
GET  /health                                    # 等到 200
```

少了 `resume`，`AsyncOmni.generate()` 会永久卡在
`await self._pause_cond.wait_for(lambda: not self._paused and not self._sleeping_tags)`
（`entrypoints/async_omni.py:212`），表现为「服务活着、`/v1/models` 正常、请求永不返回」。
原因是 `sleep()` 会置 `_paused=True` + `_hold_admission_until_resume=True`，
而 `wake_up()` 在 `:1443` 明确「AR/mixed 引擎不重开闸门」，只有 `resume_generation()` 能重开。

## 4. 队列与背压

| 层 | 规则 | 超限返回 |
|---|---|---|
| 每模型并发 | `concurrency` = 引擎 `max_num_seqs`（voxcpm2 8 / indextts 4） | 排队 |
| 每模型队列 | `queue_limit`（默认 200） | **429 + Retry-After** |
| 唤醒预算 | `wake_timeout_s`（默认 60s） | **503 + Retry-After** |

**为什么并发要卡在 `max_num_seqs`**：vLLM 内部的 `waiting` 队列是不设上限的，
灌超过 KV 容量会触发**抢占（preemption）**——请求被踢回 waiting 重算，
现象是「并发加了一倍、吞吐没变、p99 翻几倍」。实测数字：

| 服务 | `max_num_seqs` | `GPU KV cache size` | 按 4k/请求的最大并发 |
|---|---|---|---|
| voxcpm2 | 8 | 174,752 tokens | 42.66× |
| indextts stage0 | 4 | 38,480 tokens | 15.03×（@2560） |

所以先撞的总是 `max_num_seqs`；但 indextts 的 **stage1 没有打 KV 数字**，
长文本并发时可能是 KV 先满，需要单独实测。

## 5. 待做：按 token 预算准入

按条数准入不准。更接近真实容量的做法：

```
cost   = 预估输入 token + 预估输出 token（TTS 用 音频时长 × 码率 估）
允许入队  ⇔  inflight_tokens[model] + cost ≤ token_budget[model]
```

`token_budget` 可从 vLLM 启动日志的两行自动采集：
`GPU KV cache size: N tokens` 与 `Maximum concurrency for M tokens per request: X`。

## 6. 观测（待接 Prometheus）

- 每模型队列深度、等待 p50/p95
- 换台次数/分钟（>1 次/20s 说明在抖）
- 429 / 503 比例（按 key）
- 唤醒失败次数（**唤醒 OOM 会打死整个 server 进程**，需要熔断 + 自动重启容器）

## 7. 多模态：能力路由与长任务租约

网关按 **endpoint 需要的能力** 匹配模型的 `capabilities`（`speech` / `transcription` / `image` / `video`），
能力不匹配直接 400，避免把视频请求转发给 TTS 模型。

**长任务（视频 / 配乐）改变了调度语义**：

| | 短任务 | 长任务 |
|---|---|---|
| 卡占用 | 请求在飞期间占用，返回即释放 | 用 `lease_s` 锁住卡，期间禁止换台 |
| 上游形态 | 同步返回产物 | 异步：POST 返回 id → GET 轮询 → `/content` 取产物 |
| 网关处理 | 唤醒 → 转发 → `release()` | 拿到 id 后立刻 `release()`（**占位**释放），但保留 **租约**直到任务结束 |

关键区别：**"HTTP 请求在飞"和"卡被占用"是两件事**。视频 POST 几十毫秒就返回 job id，
如果按"请求结束就释放卡"来记账，下一个模型会立刻把正在跑视频任务的卡换掉——所以必须有
独立的租约记账（`lease_s` + 任务轮询），而不是复用 in-flight 计数。

实现状态：`lease_s` 字段与异步路径的占位释放已就位；**任务级租约记账与轮询待实现**
（需要跟踪 `GET /v1/videos/{id}` 的 status，任务终结后释放租约）。

## 8. 多卡规划（node3）

| 模型类型 | 典型占用 | 建议 |
|---|---|---|
| TTS（indextts / voxcpm2 / sfx） | 14~20 GiB | 可共卡轮换（1 醒 + N 睡），换台 4~5s |
| ASR（Step-Audio-2-mini） | ~18 GiB | 基本独占一张 |
| 配乐（minimax_music3） | 视 craft 而定，可能 2 卡 | `lease_s: 600`、`ttl_s: 0`、独占 |
| 视频（minimax_h3_disaggregated） | PD 分离，可能多卡 | `lease_s: 1800`、`ttl_s: 0`、独占卡组 |

原则：**短任务共卡轮换，长任务独占不睡**。把长任务塞进轮换池等于让一次视频生成和所有 TTS 请求互相打架。
