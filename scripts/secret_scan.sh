#!/usr/bin/env bash
# ============================================================================
# 敏感信息闸门：公开仓一次手滑就不可逆，所以用机器把关，不靠自觉。
#
# 用法：
#   scripts/secret_scan.sh            # 扫工作区（tracked + 未忽略的 untracked）
#   scripts/secret_scan.sh --history  # 额外扫 git 历史（转 public 前必须跑一次）
#
# 命中任何一条 → 退出码 1。只依赖 grep/awk，无第三方包。
# .github/ 与脚本自身不参与扫描（避免规则自匹配）。
# ============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
HISTORY=0; [ "${1:-}" = "--history" ] && HISTORY=1

FAIL=0

# 白名单地址：loopback / 通配 / 文档保留段（RFC 5737，可安全出现在示例里）
allow_ip() {
  case "$1" in
    127.*|0.0.0.0|255.255.255.255|192.0.2.*|198.51.100.*|203.0.113.*) return 0 ;;
    *) return 1 ;;
  esac
}

# 扫描范围：tracked + 未被 .gitignore 忽略的 untracked
# 注意：不要用 mapfile（macOS 自带 bash 3.2 没有它，会静默枚举不到文件 → 假绿）。
# 这里用 while read 逐行填数组，并且枚举为空时 **fail closed**。
FILES=()
while IFS= read -r f; do
  [ -n "$f" ] && [ -f "$f" ] && FILES+=("$f")
done < <(
  git ls-files -co --exclude-standard 2>/dev/null \
    | grep -vE '^(\.github/|scripts/secret_scan\.sh$)'
)

if [ "${#FILES[@]}" -eq 0 ]; then
  # 扫不到文件就等于没扫 —— 这种"绿"是假绿，必须当成失败
  echo "❌ 闸门失败：没有枚举到任何文件（不在 git 仓库根，或 git 不可用）。"
  echo "   假绿比漏测更危险，所以这里直接失败，而不是打印通过。"
  exit 1
fi
echo "[信息] 扫描 ${#FILES[@]} 个文件（tracked + untracked）"

run_grep() {   # run_grep <说明> <扩展正则>
  local label="$1" rx="$2" out
  out="$(grep -InE "$rx" "${FILES[@]}" 2>/dev/null || true)"
  if [ -n "$out" ]; then
    printf '[命中] %s\n' "$label"
    printf '%s\n' "$out" | sed 's/^/        /'
    FAIL=$((FAIL + $(printf '%s\n' "$out" | wc -l)))
  fi
}

# 1) IPv4：逐个判白名单
while IFS= read -r hit; do
  [ -z "$hit" ] && continue
  ip="$(printf '%s' "$hit" | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -1)"
  allow_ip "$ip" && continue
  printf '[命中] 非白名单 IP %s\n        %s\n' "$ip" "$hit"
  FAIL=$((FAIL+1))
done < <(grep -InoE '([0-9]{1,3}\.){3}[0-9]{1,3}' "${FILES[@]}" 2>/dev/null || true)

# 2) 密钥前缀 / 私钥块
run_grep '疑似密钥' '(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)'

# 3) 内网域名 / 私有后缀
run_grep '内网域名' '(imar\.xin|migudm\.cn|\.internal\b|\.corp\b|\.lan\b|\.localdomain\b)'

# 4) 开发机绝对路径
run_grep '绝对路径' '(^|[[:space:]"'"'"'(=:])(/home/[a-z][A-Za-z0-9._-]*/|/Users/[A-Za-z][A-Za-z0-9._-]*/|/root/|/mnt/[a-z]|/nas/)'

if [ "$HISTORY" = 1 ]; then
  echo "[信息] 扫 git 历史（转 public 前必须跑一次）"
  # 历史扫描用**更窄的口径**：只认"真实使用形态"——带协议的 URL、密钥、绝对路径、IP。
  # 为什么不沿用工作树口径：提交信息里为了说明规则，会写到域名/网段的字面量（"把 X 域名
  # 换成 Y"），这类散文提及不是泄露，若一律拦截会逼人下次不敢写复盘，反而更危险。
  hist="$(
    git log -p --all 2>/dev/null | grep -InE \
      '(https?|wss?)://[A-Za-z0-9._-]*(imar\.xin|migudm\.cn)|([0-9]{1,3}\.){3}[0-9]{1,3}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|/home/[a-z][A-Za-z0-9._-]*/|/Users/[A-Za-z][A-Za-z0-9._-]*/' \
      || true
  )"
  if [ -n "$hist" ]; then
    hist="$(printf '%s\n' "$hist" | grep -vE '(127\.0\.0\.1|0\.0\.0\.0|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)' || true)"
  fi
  if [ -n "$hist" ]; then
    echo "[命中] git 历史"
    printf '%s\n' "$hist" | head -30 | sed 's/^/        /'
    FAIL=$((FAIL + $(printf '%s\n' "$hist" | wc -l)))
  fi
fi

echo
if [ "$FAIL" -gt 0 ]; then
  echo "❌ 闸门失败：$FAIL 处命中。请先泛化（示例用 127.0.0.1 / 192.0.2.x）或删除。"
  exit 1
fi
echo "✅ 闸门通过：未发现内网 IP / 内网域名 / 密钥前缀 / 绝对路径。"
