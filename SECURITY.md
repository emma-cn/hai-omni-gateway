# 安全策略

## 报告问题

**不要用公开 issue 报告安全问题。** 请用 GitHub 的
[Private vulnerability reporting](https://docs.github.com/zh/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
（仓库 Security 标签页 → Report a vulnerability），或在你自己可控的环境里复现后，把最小复现步骤
私下发给维护者。

我们会在 72 小时内确认收到，并在修复可用后同步披露。

## 已知的部署前提（不是漏洞，但会出事）

本网关**设计为跑在可信网络内、且默认没有鉴权**（见 CHANGELOG 的 Known issues）。因此：

- **不要**把它直接暴露到公网；先等鉴权与配额（`Unreleased` 里的 P0）落地，并在前面放一层网关/反代。
- `/v1/omni/{sleep,wakeup,resume}` 这类控制面接口会**直接改变显存占用**，一旦能被任意客户端调用，
  就是一次可用性事故（一个请求就能把模型睡下）。
- 配置文件里不要写密钥；上游若开启 `--api-key`，通过环境变量注入，不要提交到仓库。

## 支持范围

只对 `main` 分支的最新发布版提供安全修复。
