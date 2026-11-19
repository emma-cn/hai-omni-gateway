1|# Lessons from merged work
2|
3|> 这份文件的规矩照抄 [magpie](https://github.com/yetone/magpie) 的做法：
4|> 每条教训 = **规则 + 为什么 + 证据**，带 `seen` 计数；同一条被 **3 个不同日子**看到 → 升级进
5|> [`docs/code-standards.md`](docs/code-standards.md)。
6|> 每条规则都来自**真实踩过**的事，不是想象的最佳实践。
7|
8|## 验收与测试
9|
10|**验收脚本自己也要被验收：断言要匹配语义，不能匹配子串。**
11|- 为什么：假绿比漏测更危险——它让"已交付"变成谎言。
12|- 证据：`accept.sh` 用 `grep 'lease'` 判"租约是否实现"，匹到了 `release()`，于是
13|  `lease_s` 只在 `/v1/models` 里展示也被判 PASS。改成 `grep -E 'lease_s|lease_until|Lease'`
14|  后立刻变 FAIL（真实状态：未实现）。
15|- seen: 1
16|
17|**能用行为验的，不要用 grep 源码验。**
18|- 为什么：grep 只能证明"字面出现过"，不能证明"真的生效"。
19|- 证据：`queue_limit` / `request_timeout_s` 在配置里声明、在代码里 0 引用——grep 配置会"有"，
20|  grep 使用处才是"无"。同一类错误还有"文档承诺了 `X-Omni-Wake-ETA`、代码里不存在"。
21|- seen: 1
22|
23|**时间断言两边都留余量。**
24|- 为什么：换台耗时在空卡与满卡上差一倍以上，写 `<5s` 会假红。
25|- 证据：换台实测一次 7.22s（含生成），README 的口径是"4~5s 换台"。断言应写区间。
26|- seen: 1
27|
28|## 配置与依赖
29|
30|**配置项声明了就必须有引用，否则启动即报错。**
31|- 为什么：空挂的配置项是最容易被误认为"已实现"的东西（用户和 review 都会当真）。
32|- 证据：`queue_limit`、`request_timeout_s` 声明在 `config/gateway.yaml`、代码 0 引用；
33|  `lease_s` 只在 `/v1/models` 里展示。
34|- seen: 1
35|
36|**影响"能不能叫醒"的补丁必须入库，并在构建时断言它真的在。**
37|- 为什么：干净 clone 构建出来的镜像若缺 `/v1/omni/resume`，睡一次之后所有合成请求会永久挂在
38|  准入等待（实测 90s cancelled、引擎零日志）。
39|- 证据：`docker/patches/omni_resume_route.py` 曾长期 untracked（`git ls-files` = 0）；
40|  Dockerfile 里当时只有 `ast.parse` 语法检查，不校验路由存在。
41|- seen: 1
42|
43|**依赖上游行为时要写清版本，并在升级时重跑那条断言。**
44|- 为什么：上游语义会变。vLLM 0.34 把 `gpu_memory_utilization` 的默认值改成 1.0（CUDA/ROCm +
45|  extensible KV cache），显式给小于 1 的值变成"硬上限并从 KV cache 扣内存"；我们钉的是 0.32.0，
46|  这条变化尚未生效——但升级时必须重评。
47|- 证据：vLLM 0.34 官方 optimization 文档；本仓基镜像 `vllm/vllm-omni:v0.32.0`。
48|- seen: 1
49|
50|**提交信息与文档里的 IP 示例，一律用 RFC 5737 保留段。**
51|- 为什么：敏感闸门会扫 git 历史，历史一旦公开不可回收；在提交信息里写真实内网地址做示例，
52|  等于把自己卡死在"要么放宽闸门、要么重写历史"的二选一上。
53|- 证据：`chore: 治理件入库…` 的提交信息里拿真实内网段地址作举例，CI 的 `--history` 扫描
54|  当场红；改用 `192.0.2.1`（TEST-NET-1，RFC 5737 保留段）后绿。
55|- seen: 1
56|
57|**扫描脚本枚举不到文件时必须失败，不许打印"通过"。**
- 为什么：闸门最危险的失败方式是**假绿**——它比不扫更糟，因为团队会以为已经守住了。
- 证据：`scripts/secret_scan.sh` 用 `mapfile` 收集文件列表，macOS 自带 bash 3.2 没有该内建，
  于是 `FILES` 为空、`grep` 无参数、输出 "✅ 闸门通过"；而同一份脚本在 Linux CI 上是真扫的。
  改成可移植的 `while read` 填数组，并在枚举为空时 **exit 1**。
- seen: 1

## 调度正确性
58|
59|**上游说"暂停没完成就不许 sleep"，那就一条 warning 都不能放行。**
60|- 为什么：在飞请求被掐掉、引擎状态与网关记账错位，事后没人能复原。
61|- 证据：`scheduler.py::_switch_to()` 在 drain 超时后 `log.warning("... 继续换台")` 并继续换台；
62|  vLLM-Omni 官方文档原文：「A timeout or error from `pause_generation` means the pause did **not**
63|  complete … **Do not sleep or mutate weights after a failed pause**」。验收项 A-204 钉住它。
64|- seen: 1
65|
66|**失败要能归因，不能只有一个 in-flight 计数。**
67|- 为什么：p95 抖了以后分不清是"排队"、"唤醒"还是"生成慢"，就没法优化。
68|- 证据：magpie 的 `usage.jsonl` 每行带 `ttft_ms` / `flow_ms` / `ms`；本仓当前无任何请求账本。
69|  验收项 A-601/A-602。
70|- seen: 1
71|
72|## 工具链
73|
74|**shell 里变量后面跟全角字符，必须写 `${VAR}`。**
75|- 为什么：bash 会把全角字符当成变量名的一部分，报 `unbound variable`，看起来像脚本写错行。
76|- 证据：`release-gate.sh` 里 `"http=$A_STATE（应 401）"` → `A_STATE（: unbound variable`。
77|- seen: 1
78|
79|**把 Markdown 灌进 KDocs 要用 `.docx`，不要用 `.md`。**
80|- 为什么：KDocs 的 md 导入会把表格退化成 tab 拼接的纯文本（`<table>` = 0）。
81|- 证据：同一份 PRD，`.md` 导入 12,727 字但 0 张表；`.docx` 导入 13,213 字且表格是真表格
82|  （抽查某表 4 行 12 格）。另注：macOS `textutil -convert docx` 产出的 docx 里 `<w:tbl>` 也是 0。
83|- seen: 1