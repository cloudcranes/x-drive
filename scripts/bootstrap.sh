#!/usr/bin/env bash
# 目标机上一次性部署 x-drive。用法：
#   tar czf - --exclude=.env --exclude=.venv --exclude=data --exclude=downloads . \
#     | ssh root@HOST 'mkdir -p /opt/x-drive && tar xz -C /opt/x-drive'
#   ssh root@HOST 'bash /opt/x-drive/scripts/bootstrap.sh'
#
# 需要事先配置 /opt/x-drive/.env：
#   TELEGRAM_BOT_TOKEN + ALLOWED_USER_IDS + CD2_URL/CD2_USER/CD2_PASSWORD
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

ENV_FILE="$APP_DIR/.env"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "== 安装 docker"
  curl -fsSL https://get.docker.com | sh
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "缺少 docker compose 插件，请先安装 docker-compose-plugin" >&2
  exit 1
fi

if [ ! -s "$ENV_FILE" ]; then
  cp .env.example "$ENV_FILE"
  echo "已从 .env.example 生成 $ENV_FILE，请填入 TELEGRAM_BOT_TOKEN、ALLOWED_USER_IDS 与 CD2 地址和账号后重跑" >&2
  exit 1
fi

if grep -q '^TELEGRAM_BOT_TOKEN=$' "$ENV_FILE"; then
  echo "$ENV_FILE 里 TELEGRAM_BOT_TOKEN 为空" >&2
  exit 1
fi

mkdir -p data work
# 工作区要可写：容器里的 STATE_DIR=/work 直接挂这里
chmod 700 data work 2>/dev/null || true

docker compose up -d --build
sleep 10
docker compose ps
docker compose exec -T web python -m app.allinone --check
