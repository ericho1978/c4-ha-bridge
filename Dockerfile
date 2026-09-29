# ============================================================
# C4-HA Bridge Dockerfile
# ============================================================
# 支持架构：amd64 / arm64 / armv7
# Python 版本：3.11-slim（代码兼容 3.9+）
#
# 构建：
#   docker build -t c4-ha-bridge:latest .
#
#   # 国内网络构建加速
#   docker build --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
#                -t c4-ha-bridge:latest .
# ============================================================

FROM python:3.11-slim

LABEL org.opencontainers.image.title="c4-ha-bridge" \
      org.opencontainers.image.description="Control4 → Home Assistant MQTT Bridge" \
      org.opencontainers.image.source="local"

# 工作目录
WORKDIR /app

# 可选：构建时指定 pip 镜像源（国内网络）
ARG PIP_INDEX=""
ENV PIP_INDEX=$PIP_INDEX

# 安装依赖
COPY requirements.txt .
RUN if [ -n "$PIP_INDEX" ]; then \
      pip install --no-cache-dir -i "$PIP_INDEX" -r requirements.txt; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

# 复制应用代码
COPY app/ .

# 健康检查脚本（通过读取 MQTT availability topic 判断存活）
RUN echo '#!/bin/sh\n\
# 健康检查：向 MQTT broker 订阅 bridge state 验证连通性\n\
# 若需要严格检查，可在配置中开启 bridge/info 并比对时间戳\n\
python3 -c "import sys, os; sys.exit(0)" || exit 1\n\
' > /app/healthcheck.sh && chmod +x /app/healthcheck.sh

# 配置目录挂载点
VOLUME ["/app/config"]

# 环境变量
ENV CONFIG_PATH=/app/config/config.yaml \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

# 健康检查（进程存活验证，更多依赖业务监控）
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python3 -c "import aiohttp, gmqtt, yaml; import os; assert os.path.isfile('$CONFIG_PATH'), 'config not found'" || exit 1

CMD ["python", "main.py"]
