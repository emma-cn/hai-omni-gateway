# 发版（RELEASING）

照 magpie 的做法：**版本号连续累加、只增不减；每个 tag 都要过一次绿灯；Release 正文写"改了什么 / 跑了什么 / 没试什么"**。

## 版本号

```
v0.1.<build>      build 从 1 开始，只看历史最大值 +1（scripts/next_version.sh 算）
v0.1.<build>-maint 当天没有任何验收用例 FAIL→PASS，仅维护性改动：不对外宣布
```

- **永不回退、永不复用**。发出去过的号，即使 tag 被删掉也不再使用——否则客户端日志里的版本号会指向两个不同的东西。
- 版本号与镜像 tag、`/healthz` 的 `build` 字段三者**必须一致**，且都由同一个 tag 推导（见 `release.yml` 的断言）。

## 一次发版

```bash
# 1) 确认工作区干净、CI 绿
git status --short

# 2) 先干跑：算下一个号 + 查 CI 状态（不推 tag）
GITHUB_TOKEN=... scripts/release.sh --dry-run

# 3) 正式发版（推 tag → release.yml 自动跑全量验证 + 建 Release）
GITHUB_TOKEN=... scripts/release.sh
```

`release.yml` 在 tag 上做四件事：敏感闸门（含历史）→ lint/冒烟/测试 → 构建镜像并断言 `OMNI_BUILD` == tag → 建 Release（正文取自 `CHANGELOG.md` 对应段落）。

**tag 必须用个人 token 在本地推**：GitHub 上用 `GITHUB_TOKEN` 打的 tag 不会触发其它 workflow（防递归），release.yml 就跑不起来。

## 回滚

```bash
git push origin :refs/tags/v0.1.<build>     # 删远端 tag（该号作废，不复用）
gh release delete v0.1.<build> --yes        # 删 Release
```

镜像层面的回滚：重新部署上一个 tag 构建出的镜像即可（镜像 tag == 版本号，不需要找 digest）。

## CHANGELOG 纪律

发版前把 `## [Unreleased]` 改成：

```markdown
## [0.1.<build>] - YYYY-MM-DD

### Added / Changed / Fixed
- ...

### 实测（跑了什么）
- 命令 + 结论

### Known issues
- 没解决、但已知道的问题（别藏）
```

`scripts/release_notes.py` 会抓这一段作为 Release 正文；抓不到会显式标注"取自 Unreleased"，避免 Release 页面写出虚假内容。
