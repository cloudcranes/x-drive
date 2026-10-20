FROM python:3.12-slim

ARG RCLONE_VERSION=v1.75.2

# ffmpeg 单独放到最后装：早前和 unzip 装在一起，后面 apt autoremove 把它当孤儿依赖删了，
# 容器里没有 ffprobe，视频转码步骤报 [Errno 2] No such file or directory: 'ffprobe'
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl unzip tzdata; \
    curl -fsSL "https://downloads.rclone.org/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-amd64.zip" -o /tmp/rclone.zip; \
    unzip -q /tmp/rclone.zip -d /tmp; \
    mv /tmp/rclone-*/rclone /usr/local/bin/rclone; \
    rm -rf /tmp/rclone.zip /tmp/rclone-*; \
    apt-get purge -y unzip; \
    apt-get autoremove -y; \
    apt-get install -y --no-install-recommends ffmpeg aria2; \
    rm -rf /var/lib/apt/lists/*; \
    rclone version | head -1; \
    ffmpeg -version | head -1; \
    ffprobe -version | head -1; \
    aria2c --version | head -1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
