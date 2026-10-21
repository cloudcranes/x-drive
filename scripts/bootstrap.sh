#!/usr/bin/env bash
# 目标机上一次性部署 x-drive。用法：
#   tar czf - --exclude=.env --exclude=.venv --exclude=data --exclude=downloads . \
#     | ssh root@HOST 'mkdir -p /opt/x-drive && tar xz -C /opt/x-drive'
#   ssh root@HOST 'bash /opt/x-drive/scripts/bootstrap.sh'
#
# 需要事先放好两样东西（脚本会检查）：
#   /root/.config/rclone/rclone.conf   已授权的 gdrive remote
#   /opt/x-drive/.env                  TELEGRAM_BOT_TOKEN + ALLOWED_USER_IDS
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

RCLONE_CONF="${RCLONE_CONFIG_HOST:-/root/.config/rclone/rclone.conf}"
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

if [ ! -s "$RCLONE_CONF" ]; then
  echo "缺少 rclone 配置：$RCLONE_CONF" >&2
  echo "把 rclone.conf 放到该路径，或设置 RCLONE_CONFIG_HOST 指向它" >&2
  exit 1
fi

if [ ! -s "$ENV_FILE" ]; then
  cp .env.example "$ENV_FILE"
  echo "已从 .env.example 生成 $ENV_FILE，请填入 TELEGRAM_BOT_TOKEN 与 ALLOWED_USER_IDS 后重跑" >&2
  exit 1
fi

if grep -q '^TELEGRAM_BOT_TOKEN=$' "$ENV_FILE"; then
  echo "$ENV_FILE 里 TELEGRAM_BOT_TOKEN 为空" >&2
  exit 1
fi

sed -i "s|^RCLONE_CONFIG_HOST=.*|RCLONE_CONFIG_HOST=$RCLONE_CONF|" "$ENV_FILE"
grep -q '^RCLONE_CONFIG_HOST=' "$ENV_FILE" || echo "RCLONE_CONFIG_HOST=$RCLONE_CONF" >> "$ENV_FILE"

mkdir -p data downloads

# aria2 只在 /config 为空时生成默认配置，之后一直沿用挂载目录里的 aria2.conf。
# 先起一次让它生成，再把实测有效的参数写进去，最后重启让参数生效。
if [ ! -f aria2-config/aria2.conf ]; then
  echo "== 首次启动 aria2 以生成默认配置"
  docker compose up -d aria2
  for _ in $(seq 1 30); do
    [ -f aria2-config/aria2.conf ] && break
    sleep 2
  done
fi
bash scripts/tune-aria2.sh

docker compose up -d --build
sleep 10
docker compose ps
docker compose exec -T bot python -m app.main --check
