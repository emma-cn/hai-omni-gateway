# hai-omni-gateway 测试报告（2026-11-19，dev @ 开发机）

测试负责人：测试（`test-01`）。仓库 `~/workspace/hound/hai-omni-gateway`（master `d954f68` + 工作区未提交改动）。

## 1. 结论

| 层 | 内容 | 结果 |
|---|---|---|
| L1 单元/接口（假后端，不碰 GPU） | 88 条用例，覆盖配置 / 路由 / 能力校验 / 透传 / 卡级调度 / 管理面 / 多模态 / README 验收标准 | **84 passed, 4 xfailed** |
| L2 真机 E2E | `tests/e2e_live.py`（openai SDK、sha256 透传、唤醒/换台/并发计时） | 脚本已就绪，**需 --yes 才动卡** |
| L3 事故复盘 | 17:18–17:19 真机 OOM，见 §4 | 已定位，两条真实缺陷触发 |

4 条 `xfail(strict=True)` = **已确认的实现缺口**，修好后会自动变成 XPASS 报警（提示把断言翻正）。

## 2. 怎么跑

```bash
# 开发机上，一次性环境
python3 -m venv ~/.venvs/omni-gw-test
~/.venvs/omni-gw-test/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ \
    "fastapi>=0.115" "httpx>=0.27" "pyyaml>=6.0" "pytest>=8.0" "pytest-asyncio>=0.24"

cd ~/workspace/hound/hai-omni-gateway
PYTHONPATH=src ~/.venvs/omni-gw-test/bin/python -m pytest tests -q
# -> 84 passed, 4 xfailed in ~4s

# 真机（会唤醒/睡下真实模型，占卡 1~2 分钟）
PYTHONPATH=src ~/.venvs/omni-gw-test/bin/python tests/e2e_live.py --yes
```

**测试不碰真后端**：`tests/conftest.py` 把网关的 `httpx.AsyncClient` 换成 `MockTransport`，
`http://omni-a:60589` 这类地址只在内存里路由。开发机的 60589 真的躺着 indextts，用例绝不会把它睡掉。

## 3. 已确认的 4 个缺口（xfail）

| # | 缺口 | 证据 | 影响 |
|---|---|---|---|
| A1 | **`queue_limit` 从未生效**：`config.py:53` 定义、`config.py:74` 解析，全程无人读取 → 没有 429，超并发只会无限排队 | `grep -rn queue_limit src/` 只有 config 两行；`tests/test_acceptance.py::test_acceptance_queue_limit_returns_429` | DESIGN §4 写的背压是空的；客户端超时前一直干等 |
| A2 | **`X-Omni-Wake-ETA` 不存在**：README 验收标准 3 承诺带此头 | `grep -rn X-Omni-Wake-ETA src/` 无结果 | 调用方无法区分"在唤醒"还是"卡住了" |
| M1 | **多视频模型时轮询断链**：`GET /v1/videos/{id}` 没有 `model` 字段 → `_dispatch` 候选 >1 → 400 | `app.py:196-202`；`tests/test_multimodal.py::test_video_get_two_video_models_current_400`（现状 400） | OpenAI 视频接口的轮询 GET 天生不带 model，接第二个视频模型即坏 |
| V1 | **长任务租约未实现**：`lease_s` 只入库+展示，没有任何记账/轮询释放 | `grep -rn lease src/` 全是读出来展示；DESIGN §7 自称"任务级租约记账与轮询待实现" | 视频/配乐跑到一半会被同卡短任务挤下去 → 重载几分钟到几十分钟 |

## 4. 真机事故复盘（2026-11-19 17:18–17:19，非本次测试触发）

网关日志与后端日志能拼出一条完整因果链：

```
17:18:52 switch /models/IndexTTS-2.5 from=/models/VoxCPM2     # voxcpm2 醒着
17:18:53 wake IndexTTS ok in 0.88s                            # 换台到 indextts
17:18:58 sleep IndexTTS -> RemoteProtocolError: Server disconnected without sending a response.
         -> card4: fail /models/IndexTTS-2.5 sleep: ...        # 睡眠失败，权重没让出
17:18:58 switch /models/VoxCPM2 from=/models/IndexTTS-2.5      # 照样继续换台
17:19:03 wakeup VoxCPM2 -> 500 CUDA Error: out of memory
         -> 503 给调用方
```

后端侧：`ls-voxcpm2` 在 `wake_up` OOM 后 `Shutting down` → 容器 `Exited (0)`。

**两个真实缺陷**：

1. **睡眠失败后继续唤醒（最致命）**。`scheduler.py:124-137`：`sleep()` 返回 `ok=False` 只记一条事件，
   接着无条件 `wake(target)`。旧模型权重还在显存里 → 新模型必 OOM。
   正确行为：睡失败就**中止换台**并抛 `WakeFailed`（保持 `_awake` 不变），让调用方拿到 503 重试。
2. **唤醒 OOM 会打死后端进程**（README 已记载），而网关没有熔断/自动重启 —— 后端死了只能人工
   `docker start`。

**当前残留风险（写这份报告时仍存在）**：`/admin/state` 报 `awake: null`，但 GPU 实际被 indextts 占着
14 GB（`nvidia-smi` 实测）。网关状态与真实显存已经不一致，**下一次任何请求的唤醒都会再 OOM**。
恢复手段只有两条：重启网关（启动对账会把所有后端睡一遍）或手工 `POST {backend}/v1/omni/sleep`。
`/admin/sleep` 救不了它 —— `force_sleep()` 在 `_awake != model` 时直接 return（`scheduler.py:174`），
而网关认为没有任何模型醒着。

## 5. 其他观察（非阻断）

- **管理面无鉴权**：`/admin/wake|sleep|state` 匿名可调（`tests/test_acceptance.py::test_acceptance_admin_surface_has_no_auth`）。README 把鉴权列为 TODO，但管理面能直接左右 GPU 占用，建议优先于"按 key 计费"。
- `GET /v1/audio/voices` 返回 405（只注册了 POST / DELETE）：上游若支持音色列表，这是缺口。
- `GET /metrics` 404（Prometheus 未接，README 已列为待做）。
- 客户端 192.0.2.10 一直在轮询 `/api/events` → 404：当前 `static/ui.html` 只轮询 `/admin/state`
  （`setInterval(load, 2000)`），说明**那是浏览器里缓存的旧版页面**，或另一个自制看板。看板不刷新时先怀疑这个。
- 事件环（`Card.events`，上限 200）是**进程内**的，重启即空；`/admin/state` 只回最新 50 条，没有持久化。

## 6. 建议的修复顺序

1. 睡眠失败即中止换台（§4 缺陷 1）—— 一行 `if` 的事，但挡的是"换台必 OOM"。
2. 唤醒 OOM 熔断 + 自动重启后端容器（否则一次 OOM 就把栈打成半死）。
3. 换取台后 `_awake=None` 的恢复手段：至少让 `/admin/sleep` 对"实际醒着但网关认为睡着"的模型生效
   （例如新增 `/admin/reconcile`，或在 `force_sleep` 里忽略 `_awake` 判断）。
4. `queue_limit` → 429 + `Retry-After`（A1）。
5. 长任务租约（V1）与轮询路由（M1）—— 这两个不修，视频/配乐接不进来。
