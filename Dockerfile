# ---- 测试阶段：验收脚本用它跑容器内单测，运行镜像里不带 pytest / tests
FROM python:3.12-slim AS test
WORKDIR /app
COPY requirements.txt requirements-cd2.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-cd2.txt pytest
COPY app ./app
COPY tests ./tests
CMD ["python", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]

# ---- 运行镜像
# 两个构建开关，按实际用哪个存储后端裁剪镜像：
#   WITH_RCLONE=0  不下载 rclone 二进制（改用 OpenList / CloudDrive2 时）
#   WITH_CD2=0     不装 grpcio / protobuf（只用 rclone / OpenList 时，约省 25MB）
FROM python:3.12-slim AS runtime

ARG RCLONE_VERSION=v1.75.2
ARG WITH_RCLONE=1
ARG WITH_CD2=1

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl unzip tzdata; \
    if [ "$WITH_RCLONE" = "1" ]; then \
        curl -fsSL "https://downloads.rclone.org/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-amd64.zip" -o /tmp/rclone.zip; \
        unzip -q /tmp/rclone.zip -d /tmp; \
        mv /tmp/rclone-*/rclone /usr/local/bin/rclone; \
        rm -rf /tmp/rclone.zip /tmp/rclone-*; \
        rclone version | head -1; \
    else \
        echo "skip rclone (WITH_RCLONE=$WITH_RCLONE)"; \
    fi; \
    apt-get purge -y unzip; \
    apt-get autoremove -y; \
    apt-get install -y --no-install-recommends ffmpeg; \
    rm -rf /var/lib/apt/lists/*; \
    ffmpeg -version | head -1; \
    ffprobe -version | head -1

WORKDIR /app

COPY requirements.txt requirements-cd2.txt ./
RUN set -eux; \
    pip install --no-cache-dir -r requirements.txt; \
    if [ "$WITH_CD2" = "1" ]; then pip install --no-cache-dir -r requirements-cd2.txt; \
    else echo "skip grpcio (WITH_CD2=$WITH_CD2)"; fi

COPY app ./app

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
