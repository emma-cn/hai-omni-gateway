# hai-omni-gateway

> **唯一真源**：<https://github.com/emma-cn/hai-omni-gateway>。别处的副本不作数。

vllm-omni **多模态**（音频 / 图像 / 视频）服务的统一入口网关。

对外提供**标准 OpenAI 兼容接口**（`/v1/audio/speech`、`/v1/audio/transcriptions`、
`/v1/audio/voices`、`/v1/images/generations`、`/v1/images/edits`、`/v1/videos*`），
对内负责三件事：**路由**、**卡级调度（sleep/wake 换台）**、**请求队列与背压**。

```
客户端 / aipai / 业务方
      │  OpenAI 兼容请求（model 字段路由 + capability 校验）
      ▼
hai-omni-gateway  :8080
      │  ① 鉴权/配额（TODO）  ② 队列  ③ 按卡调度：谁该醒
      ▼
vllm-omni 实例（一个容器 = 一条 pipeline）
      indextts :60589 / voxcpm2 :60592 / sfx :60594 / asr :60596
      MiniMax-Music3 / MiniMax-H3（待接入，镜像已带 deploy config）
```

## 为什么不用 llama-swap

llama-swap 的模型生命周期是「**cmd 进程的生死 = 模型是否在显存里**」，换台 = 杀掉旧进程再起新的。
而 vllm-omni 的 sleep 是「**进程常驻，只把权重换出显存**」。两者范式冲突，实测表现：

```
[DEBUG] group: starting swap for /models/VoxCPM2, evicting [/models/IndexTTS-2.5]
[DEBUG] [/models/IndexTTS-2.5] sending stop signal with timeout 5m0s
        ... 客户端等到 180s 超时 ...
[DEBUG] [/models/IndexTTS-2.5] process stopped by llama-swap: signal: killed
```

另外两个硬事实：
- llama-swap 自带的 `vllm-wrapper` 硬编码调 vLLM 原生的 `/sleep`、`/wake_up`，而 vllm-omni
  只暴露 `/v1/omni/sleep`、`/v1/omni/wakeup`（实测两者都是 **404**）。
- 统一镜像里没有 docker CLI，llama-swap 原生的「Docker 模式」也用不了。

结论：编排层自己写，但接口和语义沿用它的（队列 / 503 / 加载反馈 / TTL）。

## 实测数据（2026-11-19，单张 RTX 4090 24GB 开发机）

| 项 | 数值 |
|---|---|
| indextts 冷启 | 72s（`util 0.35+0.35` 时 66s） |
| indextts 从睡到醒（L1） | **1.70 ~ 1.85s** |
| indextts 睡下 | 1.78 ~ 1.95s，14.9 → 4.86 GiB（睡眠残量 **1.07 GiB**） |
| voxcpm2 从睡到醒 | **2.31s** |
| voxcpm2 睡下 | 2.7 ~ 2.8s，17.3 → 5.5 GiB（残量 **1.8 GiB**） |
| 唤醒 OOM 边界 | indextts `0.4+0.4`（预算 18.8 GiB）在剩 17.8 GiB 时必 OOM；降到 `0.35+0.35`（16.5 GiB）正常 |
| 唤醒 OOM 后果 | **整个 API server 进程退出**，请求挂死（不是快速 500）→ 网关必须熔断 + 有超时 |
| 睡眠后请求 | 老版本会**永久挂住**：`wakeup` 之后必须补一发 `POST /v1/omni/resume`，否则前端准入闸门关着 |

## 关键设计

1. **一个容器一条 pipeline**：vllm-omni 的 deploy config 是单文件，`serving_speech` 在 init 时把模型类型定死，
   所以多模型只能多进程。
2. **一张卡同一时刻只醒一个**：实测 indextts 14.9 GiB、voxcpm2 17.3 GiB，两个醒着的必然 OOM。
3. **换台 = 等 drain → sleep(1.8~2.8s) → wakeup+resume(1.3~1.9s) → 等健康**，约 **4~5 秒**。
4. **三个防抖参数**：`min_dwell_s`（醒着至少驻留）、`ttl_s`（空闲自动睡）、`concurrency`（对齐引擎 `max_num_seqs`）。

详见 [docs/DESIGN.md](docs/DESIGN.md)。

## 接口

| 方法 | 路径 | 需要的能力 |
|---|---|---|
| GET | `/healthz` | 网关自身健康 + 每张卡当前醒着的模型 |
| GET | `/v1/models[?capability=speech]` | 合并各后端，带 `meta.omnigateway.status` / `capabilities` |
| POST | `/v1/audio/speech` | `speech`（透传；模型睡着则先唤醒） |
| POST | `/v1/audio/voices`、`DELETE /v1/audio/voices/{name}` | `speech` |
| POST | `/v1/audio/transcriptions` | `transcription` |
| POST | `/v1/images/generations`、`/v1/images/edits` | `image` |
| POST/GET | `/v1/videos`、`GET /v1/videos/{id}`、`/v1/videos/{id}/content` | `video` |

**能力校验**：模型没声明对应 `capabilities` 就返回 400 `unsupported_capability`，
避免"把视频请求转发给 TTS 模型"这种低级事故。

**model 字段不做改写**：客户端发什么就路由什么，`model` 必须等于 vllm-omni 实际服务的名字
（例如 `/models/IndexTTS-2.5`）。对 aipai 来说就是「把 `INDEXTTS_URL` 指向网关」，代码零改动。

## 短任务 vs 长任务（这是接视频时最重要的区别）

| | 短任务（TTS / 音效 / 图） | 长任务（视频 / 配乐） |
|---|---|---|
| 典型耗时 | 0.3 ~ 3s | 几分钟 ~ 半小时 |
| 上游语义 | 同步返回产物 | 异步：POST 返回 id，GET 轮询，`/content` 取产物 |
| 卡占用 | 请求在飞期间占用，返回即释放 | 必须 `lease_s` **锁住卡**，期间禁止换台 |
| 换台 | 可以频繁切（4~5s 成本） | 绝不允许中途被挤下去（重载代价几分钟到几十分钟） |
| 网关做法 | 唤醒 → 转发 → 释放 | 唤醒 → 转发（拿 id 后立即释放**占位**，但**租约**仍在）→ 轮询期间该卡不换台 |

## 接入 MiniMax（镜像已带 deploy config，不用写模型代码）

vllm-omni 镜像里有 143 份 deploy config，MiniMax 相关的现成可用：

```
minimax_h3_disaggregated.yaml            MiniMax H3（PD 分离）
minimax_h3_disaggregated_turbo_nixl.yaml NIXL + turbo 变体
minimax_music3.yaml / minimax_music3_2gpu.yaml   配乐
qwen3_tts*.yaml（8 个变体，含 high_concurrency）
bagel / glm_image / hunyuan_image3 / ming_image  图像
causvid_wan / hunyuan_video_15 / wan2_2          视频
```

接入步骤（每个模型一条）：
1. `scripts/download_models.sh` 拉权重（照现有四个服务的写法加一条）
2. `config/<model>.yaml` 从镜像里的 deploy 模板复制出来，按卡位改 `devices` / `gpu_memory_utilization`
3. compose 里加一个服务（一个容器 = 一条 pipeline）
4. 网关 `config/gateway.yaml` 加一条模型：`capabilities`、`lease_s`、`ttl_s`、`concurrency`

注意 **H3 的 PD 分离变体**（`*_disaggregated*`）是两段部署、可能跨卡，`stage_ids` 要写 `[0,1]`，
且它基本吃满一张卡甚至多张——所以这类模型应该 `ttl_s: 0`（不自动睡）+ 独占卡。

## 运行（容器）

```bash
cp config/gateway.example.yaml config/gateway.yaml     # 按实际端口/卡位改
docker compose -f docker/compose/docker-compose.yml up -d --build
# 浏览器打开  http://<host>:8080/ui
```

`network_mode: host`：网关本身不占 GPU，但要直连本机的 vllm-omni 端口。
容器启动时会对账（把所有后端睡一遍再归零），因为网关是这张卡的唯一调度者——
不归零就会出现"以为都睡着、实际有模型醒着"，一唤醒就 OOM。

本地开发也可以直接跑：

```bash
pip install -e .
OMNI_GATEWAY_CONFIG=config/gateway.yaml python -m omni_gateway
```

## 管理页 `/ui`

单页控制台，管理 + 测试合一：

- **显卡卡片**：每张卡当前醒着谁、在飞的请求数、`min_dwell`
- **模型表**：状态（醒着/睡眠）、能力、在飞数、TTL，带「唤醒 / 睡眠」按钮
  （睡眠按钮在有在飞请求时会被禁用，避免掐掉用户请求）
- **事件流**：`wake / sleep / switch / idle_sleep / fail / reconcile`，带时间戳
- **测试区**：选模型 → 输文本 →（可选）上传参考音频或填容器内路径 → 合成 →
  在线试听 + 下载，并显示耗时；模型睡着时会自动唤醒
- 每 2 秒轮询 `/admin/state`，无需手动刷新

接口：`GET /admin/state`、`POST /admin/wake {model}`、`POST /admin/sleep {model}`。

## 验收标准

1. 官方 `openai` SDK 直连网关能出音频（标准调用 + `with_streaming_response`）。
2. 网关产出的 WAV 与直连后端的 WAV **md5 一致**（透传无损）。
3. 睡着状态下的请求：要么 N 秒内唤醒并正常返回（带 `X-Omni-Wake-ETA`），
   要么 503 + `Retry-After`——**不允许出现"挂 180 秒无响应"**。
4. 换台期间第二个模型的请求排队而非失败；换台完成后由**一次**唤醒服务队列里的多条请求。

## 当前进度

- [x] 配置 + 卡级单写者状态机（drain / min_dwell / ttl）
- [x] `wakeup + resume + 健康检查` 两段式唤醒
- [x] `/v1/audio/speech` 流式透传、`/v1/models` 带状态
- [ ] 鉴权 / 按 key 配额 / 计费
- [ ] 异步 job（`POST /v1/jobs` → 202 + `Location`）
- [ ] 按 token 预算的准入（初始值从 vLLM 启动日志的 `GPU KV cache size` 自动采集）
- [ ] Prometheus 指标 / 熔断（唤醒 OOM 时自动重启容器）

## 仓库结构

```
src/omni_gateway/     app.py 接口层 / scheduler.py 卡级调度 / upstream.py 后端控制 / config.py 配置
config/               gateway.example.yaml
docker/               Dockerfile + compose
scripts/              vllm-omni-wrapper.py（试验期产物，llama-swap 场景可用；本网关不需要）
```
