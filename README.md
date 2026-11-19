<div align="center">

# hai-omni-gateway

**一台 GPU，N 个模型，一个 OpenAI 端点。**
睡着的模型 1.7 秒醒来，换台 4~7 秒（含 5 秒防抖），客户端永远只认 `model` 一个字段。

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)](pyproject.toml)
[![CI](https://github.com/emma-cn/hai-omni-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/emma-cn/hai-omni-gateway/actions)

</div>

---

## 它解决什么问题

一张 24 GB 的卡想同时服务人声克隆、音色设计、音效、ASR —— 三件硬事实挡住了：

1. **一个容器只能跑一条 pipeline**。`vllm-omni` 的 deploy config 是单文件，
   `serving_speech` 在 init 时就把模型类型定死；
2. **装不下两个醒着的模型**。实测 IndexTTS-2.5 占 14.9 GiB、VoxCPM2 占 17.3 GiB；
3. **睡着的模型仍占显存**。IndexTTS 1.07 GiB、VoxCPM2 1.8 GiB。

所以「共用一张卡」只能是**时间片**：同一时刻一个醒着，其余睡着。
**谁来切、什么时候切、切的时候正在飞的请求怎么办** —— 这就是这个网关的全部工作。

## 核心能力

**一个端点，多种模态。**
客户端只认 `model`。`/v1/audio/speech`、`/v1/audio/transcriptions`、`/v1/audio/voices`、
`/v1/images/*`、`/v1/videos*` 全走同一个入口，OpenAI SDK 直连即可。
能力不匹配直接 400，而不是把视频请求塞给 TTS 模型。

**零冷启。**
不是「按需加载几十秒」，而是 **睡 → 醒 1.7~2.3 秒**（实测）。权重留在 CPU 内存里，
调度器、CUDA context、CUDA graph、JIT 内核全部保活——冷启动要付的账，这里只付一次。

**卡级单写者调度。**
一张卡一个锁，只有调度器能动 sleep/wake，API 层只能通过 `acquire()/release()` 间接驱动，
所以不可能出现两次换台交叉执行。三个防抖旋钮：`min_dwell_s`（刚醒的别马上被挤走）、
`drain_timeout_s`（换台前等在飞清零）、`ttl_s`（空闲自动睡，把显存还回去）。

**唤醒必须两段。**
`POST /v1/omni/wakeup` 把权重换回显存，`POST /v1/omni/resume` 重开前端准入闸门。
少了第二步，请求会永久挂在 `AsyncOmni.generate()` 的准入等待上——
**服务看着活着、`/v1/models` 正常、请求永不返回**。这个坑我们踩过，所以它在实现里是硬约束。

**换台失败是拒绝的，不是硬切的。**
`drain` 没能在超时内清零时，网关**拒绝换台**并返回 `503 drain_timeout`，
而不是打完一条 warning 就把在飞请求连同引擎状态一起掐掉。
上游规则写得很明确：暂停没完成就不许 sleep。同理，旧模型睡不下去时**不唤醒新模型**
（`503 sleep_failed`）——否则旧模型还占着显存，新模型必然 OOM，而 OOM 会把整个后端进程打死。

## 实测数据

> 单张 RTX 4090 24 GB，2026-11-19 实测，不是官方标称值。

| 项 | 数值 |
|---|---|
| IndexTTS-2.5 冷启 | 72 s（`util 0.35+0.35` 时 66 s） |
| 睡着 → 醒来（IndexTTS） | **1.70 ~ 1.85 s** |
| 睡着 → 醒来（VoxCPM2） | **2.31 s** |
| 睡下（释放显存） | 1.78 ~ 2.8 s，14.9 → 4.86 GiB |
| 完整换台（drain + sleep + wake + 健康） | 约 **4~5 s**（端到端含生成实测 7.2 s） |
| 唤醒 OOM 边界 | 预算 18.8 GiB 在剩 17.8 GiB 时必 OOM |
| 真 sleep（开 `--enable-sleep-mode`） | 2.8 s，显存 20.9 → 12.2 GiB（让出 8.7 GiB） |
| 假 sleep（忘开该 flag） | **4 ms 返回 200、`freed_bytes: 0`、显存一 MiB 不放** |
| swap 共存 20 轮轮流醒 | wake 全 200、零 OOM、容器重启计数 0 → 0 |

## 快速开始

```bash
cp config/gateway.example.yaml config/gateway.yaml   # 按你的端口/卡位改
docker compose -f docker/compose/docker-compose.yml up -d --build
# 打开 http://<host>:8080/ui 看卡状态，直接在页面里试音
```

本地开发不用容器：

```bash
pip install -e .
OMNI_GATEWAY_CONFIG=config/gateway.yaml python -m omni_gateway
```

一条命令看它现在在干什么：

```bash
curl -s localhost:8080/healthz   # 每张卡当前醒着谁
curl -s localhost:8080/v1/models # 每个模型的能力与睡眠状态
```

## 接口

| 方法 | 路径 | 需要的能力 |
|---|---|---|
| `GET` | `/healthz` | 网关健康 + 每张卡当前醒着的模型 |
| `GET` | `/v1/models` | 合并各后端，带 `status` / `capabilities` |
| `POST` | `/v1/audio/speech` | `speech`（模型睡着则先唤醒） |
| `POST` `GET` `DELETE` | `/v1/audio/voices{,/{name}}` | `speech`（`GET` 列音色） |
| `POST` | `/v1/audio/transcriptions` | `transcription` |
| `POST` | `/v1/images/{generations,edits}` | `image` |
| `POST` `GET` | `/v1/videos{,/{id},/{id}/content}` | `video` |

`model` 字段**不做任何改写**：客户端发什么就路由什么，名字必须等于 vllm-omni 实际服务的名字。
把 `INDEXTTS_URL` 指向网关，调用方代码零改动。

## 为什么不用 llama-swap

llama-swap 把「模型状态」绑在**进程生死**上，换台 = 杀掉旧进程再起新的；
而 vllm-omni 的 sleep 是「进程常驻、只让出显存」，两者范式冲突。实测：

```
[DEBUG] group: starting swap for /models/VoxCPM2, evicting [/models/IndexTTS-2.5]
[DEBUG] [/models/IndexTTS-2.5] sending stop signal with timeout 5m0s
        ... 客户端等到 180s 超时 ...
[DEBUG] [/models/IndexTTS-2.5] process stopped by llama-swap: signal: killed
```

另外两条硬事实：llama-swap 自带的 vllm-wrapper 硬编码调 `/sleep`、`/wake_up`，
而 vllm-omni 只暴露 `/v1/omni/sleep`、`/v1/omni/wakeup`（实测都是 404）；
统一镜像里也没有 docker CLI，它的「Docker 模式」用不了。

**结论：编排层自己写，但接口和语义沿用它的**（队列 / 503 / 加载反馈 / TTL）。

## 两个坑，我们替你踩了

**1）`/v1/omni/sleep` 回 200 不等于显存被释放。**
不传 `--enable-sleep-mode` 起后端时，sleep 会在 4 ms 内返回
`{"status":"SUCCESS"}`、ack 里 `freed_bytes: 0`，而**显存一 MiB 都不放**。
网关会以为换台成功，实际两个模型一直共存（实测连续 20 轮"换台"期间显存恒为 20.3 GiB）。
只能靠起服务时就带上 flag 来避免——附带一句：服务级 `command` 会覆盖 compose 的公共 `command`。

**2）`kv_cache_memory_bytes` 是硬申请，不与可用余量协商。**
`gpu_memory_utilization` 只是上限（vLLM 会把请求 cap 到可用值），
但 `kv_cache_memory_bytes` 写多少就申请多少：与另一个模型的睡眠残量共存时，
它会直接 OOM 在 `cumem_allocator.cpp`。共卡跑要改的是这个数值（我们给 voxcpm2 用 3 GiB）。

## 当前状态

**能用**：模型注册、卡级调度、两段式唤醒、多模态透传、能力校验、管理页与测试台、
**有界 FIFO 队列与背压**（队列满 `429 queue_full`，排队超时 `503 request_timeout`）、
换台安全（`drain` 未完成 / 睡眠失败一律拒绝换台而不是硬切）。

**还没做**（诚实列出来，别当成已有）：
鉴权与按 key 配额 · 唤醒失败熔断与后端自愈 ·
错误体机器可读码与限流响应头 · 异步任务租约 · 请求账本（`X-Omni-Wake-ETA` 也在这里）· Prometheus 指标。

> 这些都在路线图里，按版本交付；验收用例是公开的，没过就是没做。

## 仓库

```
src/omni_gateway/app.py        HTTP 层：OpenAI 兼容接口 + 能力校验 + 管理页
src/omni_gateway/scheduler.py  卡级调度：单写者状态机
src/omni_gateway/upstream.py   vllm-omni 控制面：sleep / wakeup / resume
src/omni_gateway/static/ui.html 单页控制台（管理 + 试音合一）
docs/DESIGN.md                 设计依据与实测数据（改调度前必读）
AGENTS.md                      施工规则与禁区
LESSONS.md                     踩过的坑（规则 + 为什么 + 证据）
CHANGELOG.md                   每个版本改了什么、跑了什么、已知问题
```

## 许可

[MIT](LICENSE)。后端是 [`vllm-omni`](https://github.com/vllm-project/vllm-omni)，各自遵循其许可。
