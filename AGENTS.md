# AGENTS.md —— 给在此仓库施工的 agent 的规则

这份文件的存在理由：本项目的代码由 agent 写、审、合并；把"怎么施工、验收在哪、哪里是禁区"
写清楚，比每次口头交代可靠。**动手前先读这份，再读 [`LESSONS.md`](LESSONS.md)。**

## 真源

**唯一真源是 GitHub：`github.com/emma-cn/hai-omni-gateway`。** 内网 Gitea 上的副本不再作为基准，
禁止"两边都推"。所有 issue / PR / CI / tag / release 都在 GitHub 上发生。

## 目录地图

```
src/omni_gateway/app.py        HTTP 层：OpenAI 兼容接口 + 能力校验 + 管理页 + 透传
src/omni_gateway/scheduler.py  卡级调度：单写者状态机（acquire/release/switch/idle-sleep）
src/omni_gateway/upstream.py   vllm-omni 控制面：sleep / wakeup / resume / 健康检查
src/omni_gateway/config.py     配置解析（gateway.yaml）
src/omni_gateway/static/ui.html  单页控制台（管理 + 测试合一）
docs/DESIGN.md                 设计依据与实测数据（改调度前必读）
config/gateway.example.yaml    配置样例（改动要同步 README 与 ui.html 的字段展示）
docker/                        Dockerfile + compose（network_mode: host）
```

## 施工规则

0. **装钩子**：`bash scripts/install-hooks.sh`（每 clone 一次）。敏感闸门在 pre-commit 跑，
   扫整个工作树含 untracked —— CI 只扫"已经进了远端"的内容，那时已经晚了。

1. **配置项不许空挂**：往 `config/gateway.example.yaml` 加一个键，必须同时有读取它的代码和一条
   验收断言；否则启动时报错，别静默忽略。
2. **改调度前先读 `docs/DESIGN.md`**：那里有"为什么必须两段唤醒"「drain 不能 abort」这类
   用实测换来的结论。**违反上游明令禁止的动作**（例：暂停失败后仍 sleep）一律视为 bug。
3. **默认值不覆盖用户显式值**：合并顺序 = 用户显式 > 环境变量 > 内置默认；启动打印生效值。
4. **破坏性可观测**：任何"没有回答"（`None` / 连接失败）都不许当成"值是空的"来用。
5. **不提交敏感信息**：内网 IP、主机名、绝对路径、密钥一律不进仓库；示例配置用 `127.0.0.1`。
6. **提交信息写清"跑了什么"**：跑了哪些命令、手 break 了什么、哪些**没**验。大改动必须附输出。
7. **每个 tag 等一次绿灯**：CI 没过不许打 tag；tag 不通过就不要发版。
8. **改完 `src/` 必须 `scripts/deploy-dev.sh` 才算交付**：`src/` 是 COPY 进镜像的，
   只 `docker restart` 跑的仍是旧代码——源码类验收 PASS、行为类验收 FAIL 会同时成立，最难查。
   部署脚本会断言 `/healthz` 的 `build` 等于本次 tag。
9. **版本号纪律**：正式号 `x.y.z` 只能由 `scripts/release.sh`（→ `release.yml`）产出；
   dev 构建不得占用正式号（`deploy-dev.sh` 会自动改写成 `x.y.z-dev-<sha>`）。
   已出现过"本地镜像 0.1.2 与 GitHub v0.1.2 内容不同却同号"，事后无法分辨。

## 验收

- **本仓库的 `tests/` 只是快速信号**（CI 跑，防回归）；**"DONE" 只认产品侧的
  `scripts/accept.sh`**（黑盒、对真环境、跨重启）。别把仓库内用例当成验收结论。
- 但仓库内用例**不许表达与产品契约相反的东西**：
  - 任何"现状如此"的基线用例，必须写成**目标断言** + `@pytest.mark.xfail(strict=True)`，
    `reason` 里必须能搜到需求 ID（`P0-x`）或验收 ID（`A-xxx`）；
  - 反例（历史教训）：`assert r.status_code == 200, "管理面竟然有鉴权？那就更新这条基线"`
    —— 把缺陷钉成期望值，等鉴权真落地时，这句模糊指引最可能被执行成"把鉴权回退掉"。

- 验收脚本在开发侧之外（产品经理持有），本仓库只需保证：
  `pip install -e ".[dev]" && ruff check src && python -c "from omni_gateway.config import GatewayConfig; GatewayConfig.load('config/gateway.example.yaml')"`
  能过。
- 新增接口必须同时在 `README.md` 的接口表、`/openapi.json` 示例、`static/ui.html` 三处出现；
  只改一处视为未完成（历史教训：文档承诺了 `X-Omni-Wake-ETA`、代码里没有）。

## 禁区

- 原则上不改 `vllm-omni` 上游镜像内部的代码，控制面只走 HTTP：
  `/v1/omni/{sleep,wakeup,resume}`。
  **唯一例外**：`docker/patches/` 下的"追加式补丁"。存在理由 —— 上游只暴露了
  `sleep`/`wakeup`，没暴露真正能重开准入闸门的 `resume_generation()`，导致睡一次之后
  所有合成请求永久挂在准入等待（实测 90s cancelled、引擎零日志）。这类补丁的硬要求：
  1. 补丁正文必须入库（不含补丁的镜像 = "能睡不能醒"）；
  2. 构建期必须断言路由**存在**（`grep -c "/v1/omni/resume" api_server.py ≥ 1`），
     只做 `ast.parse` 语法检查不算数；
  3. 注释里写清上游版本与移除条件，上游修好后立刻删掉该层。
- 不做多 provider 竞价路由、不做语义缓存（理由见 `docs/DESIGN.md` 的"不做什么"）。
