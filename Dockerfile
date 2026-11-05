# ---- 测试阶段：验收脚本用它跑容器内单测，运行镜像里不带 pytest / tests
FROM python:3.12-slim AS test
WORKDIR /app
COPY requirements.txt requirements-cd2.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-cd2.txt pytest
COPY app ./app
COPY tests ./tests
# 静态对账的用例要读这些文件（入口、健康检查、文档里的命令必须对得上），
# 少拷一个就会在容器里变成 FileNotFoundError，而不是真的验过。
COPY Dockerfile docker-compose.yml ./
COPY scripts ./scripts
COPY README.md ./
CMD ["python", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]

# ---- 运行镜像：CloudDrive2 为唯一存储后端
FROM python:3.12-slim AS runtime

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl tzdata ffmpeg; \
    rm -rf /var/lib/apt/lists/*; \
    ffmpeg -version | head -1; \
    ffprobe -version | head -1

WORKDIR /app

COPY requirements.txt requirements-cd2.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-cd2.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1
# 单容器：bot 长轮询（主线程）+ 网页看板（后台线程）。两个入口仍可单独跑：
# `python -m app.main` 只起 bot，`python -m app.web` 只起网页（测试与排障用）。
CMD ["python", "-m", "app.allinone"]
