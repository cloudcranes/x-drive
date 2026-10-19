## 用预构建镜像部署（不本机构建）

GitHub Actions 在推 `main` 或 `v*` tag 时构建并推送 `ghcr.io/cloudcranes/x-drive`：

```bash
# 首次需要登录（镜像默认 private；在 Packages 里改 Public 后可跳过）
echo "$GHCR_TOKEN" | docker login ghcr.io -u cloudcranes --password-stdin

cd /opt/x-drive
sed -i 's|^# BOT_IMAGE=.*|BOT_IMAGE=ghcr.io/cloudcranes/x-drive:latest|' .env
docker compose pull && docker compose up -d
```

标签规则：`latest`（main）、`main`、`v1.2.3` / `1.2`（tag）、`sha-xxxxxxx`（每次提交）。

不想用远端镜像时，删掉 `.env` 里的 `BOT_IMAGE` 就会回退到本机 `docker compose up --build`。

## 展示页
