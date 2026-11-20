#!/usr/bin/env bash
# ============================================================================
# 部署到本机 dev 环境：重建镜像 → 重启容器 → 断言 /healthz 的 build 等于本次 tag → 健康检查
#
# 为什么要一条命令：`src/` 是 **COPY 进镜像**的（不是挂载），所以只 `docker restart`
# 跑的仍是旧代码。产品侧的行为类验收会因此判 FAIL，而且很难看出原因
# （源码类判定照样 PASS，只有行为类红）——踩过一次，别再踩。
#
# 用法：
#   scripts/deploy-dev.sh              # 版本号取 git describe（无 tag 用 short sha）
#   scripts/deploy-dev.sh 0.1.3        # 显式指定
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VER="${1:-$(git describe --tags --always --dirty 2>/dev/null | sed 's/^v//')}"

# 防"同号不同内容"：dev 镜像绝不占用正式的 x.y.z 号。
# 正式号只能由 scripts/release.sh + release.yml 产出（tag 指向 bump 提交，
# 而 dev 构建通常发生在 bump 之前 —— 两者内容不同却同号，事后无法分辨）。
case "$VER" in
  [0-9]*.[0-9]*.[0-9]*) VER="${VER}-dev-$(git rev-parse --short HEAD)" ;;
esac
IMAGE="hai-omni-gateway:${VER}"
NAME="hai-omni-gateway"
CONFIG="${OMNI_GATEWAY_CONFIG:-$ROOT/config/gateway.yaml}"

echo "== 1) 构建 $IMAGE =="
docker build -f docker/Dockerfile --build-arg OMNI_BUILD="$VER" -t "$IMAGE" .

echo "== 2) 重启容器 =="
docker rm -f "$NAME" >/dev/null 2>&1 || true
# 可选 env 文件：key/token 这类东西不该写进 gateway.yaml（配置会被拷来拷去），
# 用 docker --env-file 注入，文件本身 600 且被 .gitignore 忽略。
ENVVOL=""
ENVFILE="${OMNI_ENV_FILE:-$ROOT/env/dev/gateway.env}"
if [ -f "$ENVFILE" ]; then
  ENVVOL="--env-file $ENVFILE"
  echo "   注入 env 文件: $ENVFILE"
fi
# shellcheck disable=SC2086
docker run -d --name "$NAME" --restart unless-stopped --network host $ENVVOL \
  -e LOG_LEVEL="${LOG_LEVEL:-INFO}" -e OMNI_GATEWAY_CONFIG=/app/config/gateway.yaml \
  -v "$CONFIG:/app/config/gateway.yaml:ro" \
  "$IMAGE" >/dev/null

echo "== 3) 等健康 =="
for i in $(seq 1 30); do
  if curl -sf -m 3 http://127.0.0.1:8080/healthz >/tmp/.hz 2>/dev/null; then break; fi
  sleep 1
done
cat /tmp/.hz; echo

echo "== 4) 断言 build == 本次 tag =="
GOT="$(python3 -c 'import json;print(json.load(open("/tmp/.hz"))["build"])')"
if [ "$GOT" != "$VER" ]; then
  echo "!! /healthz build=$GOT 与期望 $VER 不符：容器跑的不是这次构建的镜像"
  exit 1
fi

echo "== 5) 关键行为自检（避免交付了源码、跑的却是旧镜像）=="
python3 - "$VER" <<'PY'
import json, sys, urllib.request
ver = sys.argv[1]
def get(p):
    with urllib.request.urlopen("http://127.0.0.1:8080" + p, timeout=5) as r:
        return r.status, json.loads(r.read() or b"{}")
st, models = get("/v1/models")
names = [m.get("name") or m.get("id") for m in models.get("data", [])]
print(f"  /v1/models {st}: {names}")
print(f"  build = {ver}")
PY

echo "✅ 部署完成：$IMAGE（build=$VER）"
