# hai-omni-gateway

vllm-omni 音频栈（IndexTTS / VoxCPM2 / MOSS-SoundEffect / Step-Audio-ASR）的**统一入口网关**。

对外提供**标准 OpenAI 兼容接口**（`/v1/audio/speech`、`/v1/audio/voices`、`/v1/models`），
对内负责三件事：**路由**、**卡级调度（sleep/wake 换台）**、**请求队列与背压**。

```
客户端 / aipai
      │  OpenAI 兼容请求（model 字段路由）
      ▼
hai-omni-gateway  :8080
      │  ① 鉴权/配额（TODO）  ② 队列  ③ 按卡调度：谁该醒
      ▼
vllm-omni 实例（一个容器 = 一条 pipeline）
      indextts :60589 / voxcpm2 :60592 / sfx :60594 / asr :60596
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

## 实测数据（2026-11-19，13.1 单张 RTX 4090 24GB）

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

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 网关自身健康 + 每张卡当前醒着的模型 |
| GET | `/v1/models` | 合并各后端，带 `meta.omnigateway.status`（awake/asleep） |
| POST | `/v1/audio/speech` | 透传；目标模型睡着则先唤醒（排队，不挂死） |
| POST | `/v1/audio/voices` | 透传（上传命名音色） |
| DELETE | `/v1/audio/voices/{name}` | 透传 |

**model 字段不做改写**：客户端发什么就路由什么，`model` 必须等于 vllm-omni 实际服务的名字
（例如 `/models/IndexTTS-2.5`）。对 aipai 来说就是「把 `INDEXTTS_URL` 指向网关」，代码零改动。

## 运行

```bash
# 1) 配置
cp config/gateway.example.yaml config/gateway.yaml   # 按实际端口/卡位改

# 2) 本地跑
pip install -e .
OMNI_GATEWAY_CONFIG=config/gateway.yaml python -m omni_gateway

# 3) 或者用容器（host 网络，直连本机 vllm-omni 端口）
docker compose -f docker/compose/docker-compose.yml up -d
```

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
