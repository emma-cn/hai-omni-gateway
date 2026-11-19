#!/usr/bin/env bash
# ============================================================================
# 一键发版：验证绿 → 算下一个版本号 → 打 tag 并推送 → 由 release.yml 完成镜像+Release
#
# 用法：
#   GITHUB_TOKEN=... scripts/release.sh            # 正式发版
#   GITHUB_TOKEN=... scripts/release.sh --dry-run   # 只算号、查 CI，不推 tag
#
# 为什么不让 CI 自己打 tag：GitHub 用 GITHUB_TOKEN 打的 tag **不会触发**别的 workflow
# （防递归），于是 release.yml 不会被跑到。所以 tag 必须在本地用个人 token 推。
#
# 为什么 tag 前要查 CI：发出去的版本号不能回收；CI 红着打 tag = 发一个坏版本出去。
# ============================================================================
set -uo pipefail

REPO="${REPO:-emma-cn/hai-omni-gateway}"
API="https://api.github.com/repos/$REPO"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1

[ -z "${GITHUB_TOKEN:-}" ] && { echo "缺 GITHUB_TOKEN（需要有 repo 写权限）"; exit 2; }
H_AUTH="Authorization: Bearer $GITHUB_TOKEN"

cd "$(dirname "$0")/.." || exit 1
[ -n "$(git status --porcelain)" ] && { echo "工作区不干净，先提交再发版"; git status --short; exit 1; }

BRANCH="$(git branch --show-current)"
SHA="$(git rev-parse HEAD)"

echo "== 1) 查 $BRANCH @ ${SHA:0:8} 的 CI =="
STATE="$(curl -s -H "$H_AUTH" "$API/commits/$SHA/status" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state","unknown"))')"
RUNS="$(curl -s -H "$H_AUTH" "$API/actions/runs?head_sha=$SHA&per_page=20" \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(1 for r in d.get("workflow_runs",[]) if r["conclusion"]=="success"), sum(1 for r in d.get("workflow_runs",[]) if r["status"]!="completed"))')"
OK_RUNS="${RUNS% *}"; PENDING="${RUNS#* }"
echo "   combined status=$STATE  成功run=$OK_RUNS  未完成run=$PENDING"
if [ "$OK_RUNS" = "0" ] || [ "$PENDING" != "0" ]; then
  echo "❌ 该 commit 没有绿的 CI（或仍有 run 在跑），拒绝打 tag。等绿了再来。"
  exit 1
fi

NEXT="$(bash scripts/next_version.sh)"
VER="${NEXT#v}"
echo "== 2) 下一个版本号：$NEXT =="

# 版本号单一真源：src/omni_gateway/__init__.py 的 __version__（pyproject 用 dynamic 读它）。
# 历史上漏改过一次，导致 /healthz 报 version=0.1.0 / build=0.1.1 —— 现在由脚本保证同步。
VFILE="src/omni_gateway/__init__.py"
python3 - "$VFILE" "$VER" <<'PY'
import re, sys, pathlib
path, ver = sys.argv[1], sys.argv[2]
p = pathlib.Path(path)
s = p.read_text(encoding="utf-8")
new, n = re.subn(r'__version__ = "[^"]*"', f'__version__ = "{ver}"', s, count=1)
assert n == 1, f"{path} 里没找到 __version__ 赋值"
p.write_text(new, encoding="utf-8")
print(f"[bump] {path}: __version__ = {ver}")
PY

if [ "$DRY" = 1 ]; then
  echo "（--dry-run：不推 tag）"
  exit 0
fi

echo "== 3) 提交版本号变更 + 打 tag 并推送（触发 release.yml 全量验证 + 建 Release）=="
if ! git diff --quiet -- "$VFILE"; then
  bash scripts/secret_scan.sh >/dev/null || { echo "闸门红，拒绝发版"; exit 1; }
  git add "$VFILE"
  git commit -q -m "chore(release): 版本号 ${VER}"
  git push origin "$BRANCH" || { echo "提交推送失败"; exit 1; }
fi
git tag -a "$NEXT" -m "$NEXT"
git push origin "$NEXT" || { git tag -d "$NEXT"; echo "推送失败，已删除本地 tag"; exit 1; }

echo
echo "已推送 ${NEXT}。跟踪进度："
echo "  https://github.com/$REPO/actions/workflows/release.yml"
echo "回滚（发布包有问题时）："
echo "  git push origin :refs/tags/${NEXT}      # 删远端 tag（版本号作废，不复用）"
echo "  gh release delete ${NEXT} --yes         # 删 Release"
