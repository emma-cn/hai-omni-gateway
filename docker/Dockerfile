# 网关镜像：纯 CPU，不需要 GPU
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    OMNI_GATEWAY_CONFIG=/app/config/gateway.yaml

WORKDIR /app

# 发版要求：/healthz 的 build 必须等于镜像 tag 后缀，所以由构建参数注入，
# 而不是靠代码里手写版本号（手写的版本号一定会和镜像对不上）。
ARG OMNI_BUILD=dev
ENV OMNI_BUILD=${OMNI_BUILD}

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config ./config
COPY scripts ./scripts

EXPOSE 8080
CMD ["python", "-m", "omni_gateway"]
