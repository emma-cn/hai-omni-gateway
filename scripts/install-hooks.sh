#!/usr/bin/env bash
# 安装 git 钩子：pre-commit 跑敏感闸门。
#
# 为什么必须放在 pre-commit 而不只是 CI：
#   CI 在 push **之后**才跑，等它变红时内网 IP / 域名已经进了远端历史——
#   公开仓的提交收不回来，只能重写历史。钩子在本地就把这一刀挡住。
#
# 用法：bash scripts/install-hooks.sh    （每个 clone 执行一次）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

[ -d .git ] || { echo "不是 git 仓库根目录"; exit 1; }
mkdir -p .git/hooks

cat > .git/hooks/pre-commit <<'HOOK'
#!/usr/bin/env bash
# 敏感信息闸门（自动生成，源脚本 scripts/secret_scan.sh）
set -uo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT" || exit 1
if ! bash scripts/secret_scan.sh; then
  echo
  echo "提交被拦：上面命中的内容一旦推到公开仓就收不回来。"
  echo "  示例值请用 127.0.0.1 / 192.0.2.x（RFC 5737）与 example.invalid。"
  echo "  确需绕过：git commit --no-verify（不推荐，且 CI 还会再拦一次）"
  exit 1
fi
exit 0
HOOK

chmod +x .git/hooks/pre-commit
echo "已安装 .git/hooks/pre-commit（每次 commit 前扫工作树，含 untracked）"
