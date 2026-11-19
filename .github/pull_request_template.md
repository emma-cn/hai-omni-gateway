## 改了什么

<!-- 一句话。关联 issue：#123 -->

## 为什么

<!-- 现状证据：哪个行为/契约不对，怎么发现的。 -->

## 跑了什么

<!-- 必填。贴命令与结论；没验的部分写"未验证"，不要留空。 -->

```bash
ruff check src
python -c "from omni_gateway.config import GatewayConfig; GatewayConfig.load('config/gateway.example.yaml')"
```

## 影响面

- [ ] 改了错误体 / 响应头 → 所有端点都过了一遍
- [ ] 改了配置键 → README + 示例配置 + ui.html 三处同步
- [ ] 改了调度 → 读并更新了 `docs/DESIGN.md`
- [ ] 需要回滚：说明怎么退（镜像 tag / 配置项）

## 已知未做

<!-- 明确列出本次没解决的，别让人以为已覆盖。 -->
