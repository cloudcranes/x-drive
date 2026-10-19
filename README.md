# x-drive

给 Telegram bot 发一条 x.com / twitter.com 链接，它解析推文里的图片和视频，存进 Google Drive，回一条 Drive 文件夹链接。

解析交给 [`wujunwei928/parse-video-py`](https://github.com/wujunwei928/parse-video-py) 的官方镜像；上传交给容器内的 `rclone`。本仓库只有编排和 Telegram 这一层。

```
TG ──▶ bot ──▶ parse:8000 (解析) ──▶ 下载 ──▶ rclone copy ──▶ gdrive:/X-Drive/...
        └────────────────────────────────────────── 回执 ──▶ TG
```

## 前置

- Docker（含 compose v2）
- 一个 rclone 的 `gdrive` remote（OAuth 已授权，能 `rclone lsd gdrive:`）
- BotFather 给的 bot token

## 步骤

### 1. Docker 权限

当前账号必须在 `docker` 组，或用 ACL 放开 socket：

```bash
sudo usermod -aG docker "$USER"     # 需要重新登录
# 或临时：
sudo chmod 666 /var/run/docker.sock
```

### 2. rclone remote

```bash
rclone config      # n → 新建，名字填 gdrive，type 选 drive，走 OAuth
rclone lsd gdrive: # 能列目录即成功
```

无浏览器的机器：在本地电脑跑 `rclone authorize "drive"`，把输出的 token 粘贴到服务器的 `rclone config` 里。

### 3. 配置

```bash
cp .env.example .env
$EDITOR .env
```

必填两项：

- `TELEGRAM_BOT_TOKEN`
- `RCLONE_CONFIG_HOST`：宿主机 `rclone.conf` 的绝对路径（默认在 `~/.config/rclone/rclone.conf`）

`ALLOWED_USER_IDS` 先留空启动，给 bot 发任意消息，它会回你的 user id；把 id 填进去再重启。

### 4. 启动

```bash
docker compose up -d --build
docker compose ps            # 两个服务都应变 healthy
docker compose logs -f bot
```

### 5. 验证

```bash
docker compose exec bot python -m app.main --check
```

输出 parse 服务与 rclone remote 都 OK 后，在 TG 里发一条带图片或视频的 x 链接。成功回执形如：

```
✅ 已存 Drive · 3 图 1 视频
推文标题…
📁 https://drive.google.com/drive/folders/xxxx
```

Drive 里的结构：

```
X-Drive/<作者uid>_<作者名>/<YYYY-MM>/<推文id>/
    01.jpg  02.jpg  03.mp4  meta.json
```

再发同一条链接会直接复用已有链接，不重复上传。

## 排障

| 现象 | 处理 |
|---|---|
| `parse` 一直 unhealthy | `docker compose logs parse`；该镜像首次启动要装依赖，`start_period` 已给 40s |
| 回执「解析失败：…」 | 推文已删/受保护，或 X 限流；可设 `PARSE_VIDEO_PROXY` 走代理后重启 |
| `rclone 上传失败` | `docker compose exec bot rclone lsd gdrive:` 手测；检查挂载的 `rclone.conf` 与 OAuth 是否过期 |
| 群里发链接没反应 | BotFather 里 `/setprivacy` 设为 Disable，否则群消息里的链接收不到 |
| 回执「未授权」 | 把回执里的 user id 加进 `ALLOWED_USER_IDS` 并 `docker compose up -d` |
| 下载慢/超时 | 调大 `MAX_FILE_MB` 之外无他法；视频直链有时效，必须即到即下 |

## 已知限制

- **图文混排的推文会丢图**：`parse-video-py` 的 X 解析器只取第一条视频的最高码率 mp4，遇到视频就不再取图片；多视频也只取第一个。需要完整媒体时，得在 `app/main.py` 里加一步 fallback（直接调 Twitter syndication 接口，token 算法见参考仓库），或改上游。
- 依赖 X 的非公开接口，可能被限流或风控；不做登录态抓取。
- 不处理 HLS(`.m3u8`) 直播流，不转码。
- 单个 Google 账号上传上限 750GB/日。
- 只处理单条消息里的第一个链接；不做队列持久化，进程重启会丢进行中的任务（已入库的不受影响）。

## 开发

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests -q
```

`app/core.py` 是纯函数层（URL 提取、路径、命名、回执），`app/main.py` 是 Telegram 编排与 rclone 调用，`tests/test_flow.py` 用 stub 覆盖了下载/上传/去重/部分失败四条路径。
