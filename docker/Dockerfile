# 网关镜像：纯 CPU，不需要 GPU
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    OMNI_GATEWAY_CONFIG=/app/config/gateway.yaml

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config ./config
COPY scripts ./scripts

EXPOSE 8080
CMD ["python", "-m", "omni_gateway"]
