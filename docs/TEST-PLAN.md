# hai-omni-gateway 测试计划（按产品 / 版本)

> 这份文件是**长期维护的测试计划**：定义产品分几块、每个版本测什么、什么条件算过。
> 每个版本的执行记录放 `docs/test-records/<版本>.md`。
> 缺陷一律提到 Gitea issue（`emma-cn/hai-omni-gateway`（GitHub）），标题带级别前缀 `[P0]`…`[P3]`。

## 1. 产品与版本

| 产品 | 仓 | 发布形态 | 当前版本 | 版本号来源 |
|---|---|---|---|---|
| hai-omni-gateway（vllm-omni 统一入口网关） | `github.com/emma-cn/hai-omni-gateway` | 单容器，host 网络，`:8080` | **v0.1.0**（`dedbe1c`） | `pyproject.toml` `version` |

**版本记录规则**：每个版本一个执行记录文件 `docs/test-records/v<版本>.md`，含本次测的提交号、
用例执行结果、缺陷单号、发布结论。版本号与 `pyproject.toml` 一致，不写 `v` 前缀差异。

## 2. 测试范围（按模块切）

网关只有四块真逻辑，用例按这四块组织，避免"一个文件测所有"：

| 模块 | 关注点 | 自动化位置 |
|---|---|---|
| **M1 配置** | 默认值、缺省回退、未知字段必须报错（拼错的 `lease_s` 不能静默失效） | `tests/test_config.py` |
| **M2 接口与路由** | 探活、`/v1/models`、`model` 解析（body/query/唯一候选）、能力校验、透传无损、逐跳头 | `tests/test_api.py` |
| **M3 卡级调度** | 换台顺序（先睡后醒）、drain、`min_dwell`、并发闸门、ttl 空闲回收、启动对账、失败清理状态 | `tests/test_scheduler.py` |
| **M4 多模态与长任务** | 视频异步三段式、图像/ASR/voices、跨卡隔离、长任务租约 | `tests/test_multimodal.py` |
| **M5 管理面** | `/admin/state|wake|sleep`、事件环、无鉴权基线 | `tests/test_admin.py` |
| **M6 验收标准** | README 四条验收承诺 + 背压，一条一用例 | `tests/test_acceptance.py` |
| **E2E** | 真机：openai SDK、sha256 透传、唤醒/换台计时、并发 | `tests/e2e_live.py` |

### 测试层与门槛

| 层 | 触发时机 | 是否占 GPU | 门槛 |
|---|---|---|---|
| L1 单元/接口（假后端） | 每次改代码 / 提 PR | 否 | **全绿**，`xfail` 数量不得增加 |
| L2 真机 E2E | 每次发版前、每次动 compose/镜像/配置后 | 是（1~2 分钟） | 6 项全 PASS |
| L3 事故复盘 | 线上异常后 24h 内 | 否 | 复盘进当版本记录 |

L1 用 `httpx.MockTransport` 把网关的出站客户端整个换掉，**一个包都不发到真后端** —— dev 机 的
`:60589` 真躺着 后端 A，用例绝不能把它睡掉。

## 3. 版本准出标准（v0.1.x 系列）

发版前必须同时满足：

1. `PYTHONPATH=src pytest tests -q` → 全绿，且 `xfailed` 条数与上一版本持平或更少；
2. `tests/e2e_live.py --yes` 的 E1~E7 全 PASS（含 sha256 透传一致、并发 4 条全 200、只唤醒一次）；
3. **P0 缺陷 = 0**；
4. P1 缺陷：0，或有明确规避方案并写进 README；
5. 本次新增/变更的能力，README 的「接口」「验收标准」「当前进度」三节同步更新
   （文档与实现的偏差本身就是缺陷，见 issue #7）。

## 4. 用例矩阵（v0.1.0 实测：88 条）

| 模块 | 用例数 | 覆盖的关键断言 |
|---|---|---|
| M1 配置 | 10 | 空配置默认值；示例配置可直接加载；`base_url` 去尾斜杠；模型缺 `card` 时回退首卡；未知 `card` 自动建卡；未知字段 `TypeError`（防拼错静默失效） |
| M2 接口与路由 | 24 | `/healthz` `/` `/ui` `/openapi.json`；`/v1/models` 能力过滤 + `status/inflight`；`model` 四种来源与优先级；未知模型 404；候选>1 时 400 且列出候选；能力不匹配 400 `unsupported_capability`；**body 逐字节透传**；上游 4xx/5xx 原样透传；连接失败 502；逐跳头剥离；在飞计数（含异常路径）回落 |
| M3 卡级调度 | 20 | 首次 acquire 不睡；换台顺序 `sleep→wakeup→resume`；两段式唤醒；`min_dwell` 生效；drain 等在飞清零（不 abort）；drain 超时仍换台；wakeup/resume/health 三种失败都抛 `WakeFailed` 且清状态；失败后可自恢复；并发闸门排队不 5xx；**并发只唤醒一次**；ttl 空闲睡；`ttl=0` 不睡；在飞时不睡；睡眠失败不假装睡下；启动对账睡全量+归零；后端不可达不拖死启动；同卡永不双醒 |
| M4 多模态 | 14 | 视频 202 透传 + job id；**异步 POST 立即释放占位**；单视频模型轮询路由；`/content` 二进制透传；视频列表；多视频模型轮询（现状 400，已钉）；图像生成/编辑（multipart）；ASR；voices 增删 + 能力校验；跨卡互不影响 |
| M5 管理面 | 11 | `/admin/state` 结构；唤醒/睡下成功与失败；**在飞时拒绝睡下(409)**；未知模型 404；手动唤醒也先睡旧的；事件环上限 200 / 接口只回 50 条 / 最新在前；事件仅进程内 |
| M6 验收标准 | 9 | 验收2 字节一致（md5 的强版）；验收3 睡着→唤醒快速返回；`X-Omni-Wake-ETA`（缺）；唤醒失败 503+`Retry-After` 且不挂死；验收4 换台期间排队不失败；一次唤醒服务整队；`queue_limit`→429（缺）；背压现状基线；管理面匿名可调基线 |

`xfail(strict=True)` 的 4 条 = **已确认缺口**，修好后会 XPASS 报警，提示把断言翻正（不会悄悄放过）。

## 5. 缺陷分级与流转

| 级别 | 口径 | 处理 |
|---|---|---|
| P0 阻断 | 会打死服务 / 状态不可恢复 / 版本不能发 | 本次必修，修完回归 + 补用例 |
| P1 严重 | 主功能受损，或接新模型时必踩 | 本次必修或给规避 |
| P2 一般 | 文档承诺未兑现、体验受损、安全面 | 排期 |
| P3 轻微 | 缺口 / 待确认 | 进 backlog，标"待确认"的要给出结论 |

**提缺陷的硬要求**：可复现步骤 + 期望/实际 + 证据（日志原文或文件:行号）+ 关联用例 + 建议修法。
缺任何一项都退回重写（否则开发只能猜）。

**修完的闭环**：对应 `xfail` 用例改成正常断言（或删掉 xfail），在 issue 里回填 `commit` 与
`pytest` 结果，并把版本记录里该编号标为已验证。

## 6. 环境准备

```bash
# dev 机（Ubuntu，system python 3.14 缺 pytest，必须用 venv）
python3 -m venv ~/.venvs/omni-gw-test
~/.venvs/omni-gw-test/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ \
    "fastapi>=0.115" "httpx>=0.27" "pyyaml>=6.0" "pytest>=8.0" "pytest-asyncio>=0.24" openai

cd ~/workspace/hound/hai-omni-gateway
PYTHONPATH=src ~/.venvs/omni-gw-test/bin/python -m pytest tests -q     # L1
PYTHONPATH=src ~/.venvs/omni-gw-test/bin/python tests/e2e_live.py --yes # L2（占卡）
```