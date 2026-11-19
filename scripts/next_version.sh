#!/usr/bin/env bash
# ============================================================================
# 计算下一个发布版本号（magpie 式：0.1.<build>，构建号只增不减）
#
# 用法：
#   scripts/next_version.sh          # 例：v0.1.4  →  输出 v0.1.5
#   scripts/next_version.sh --show   # 只显示当前最大构建号
#
# 规则：
#   - 只看 vMAJOR.MINOR.BUILD 形式的 tag；构建号 = 历史最大值 + 1
#   - **永不回退、永不复用**：删过的 tag 号也不再用（发出去过就是发出去了）
#   - 打 tag 前必须先有绿 CI（release.yml 会在 tag 上重跑一遍全量验证）
# ============================================================================
set -uo pipefail

PREFIX="v0.1."

latest="$(
  git tag -l "${PREFIX}*" 2>/dev/null \
    | sed "s/^${PREFIX}//" \
    | grep -E '^[0-9]+$' \
    | sort -n \
    | tail -1
)"
# 没有任何历史 tag 时，首个版本就是 v0.1.0（而不是 v0.1.1）
if [ -z "${latest}" ]; then
  if [ "${1:-}" = "--show" ]; then echo "（无历史 tag）"; else echo "${PREFIX}0"; fi
  exit 0
fi

if [ "${1:-}" = "--show" ]; then
  echo "${PREFIX}${latest}"
  exit 0
fi

next=$((latest + 1))
echo "${PREFIX}${next}"
