# x-drive

给 Telegram bot 发**磁力链接或直链**，它按**番号**建目录下载并上传到 Google Drive。

```
你: SSIS-123 magnet:?xt=urn:btih:...
bot: ⏳ 已收到 番号 SSIS-123…
     ✅ SSIS-123 · 2.35GB
     📁 https://drive.google.com/drive/folders/xxxx

Drive: X-Drive/SSIS-123/SSIS-123.mp4
```

## 性能基线（实测，2026-10）

改性能前先看这些数字，避免在已经到顶的地方反复折腾。

### 下载端

| 项目 | 实测 | 说明 |
|---|---|---|
| 服务器出网带宽 | ~26 MB/s（单连接 HTTP） | ovh 100MB 测试文件实测 26227467 B/s |
| BT 实际下载 | ~32 MB/s（150 秒下完 4.87GB） | 已接近/超过单连接带宽，说明带宽是瓶颈 |
| aria2 参数 | split=64, max-connection-per-server=32, bt-max-peers=128 | 已相当激进，无需再调 |
| tracker 列表 | 128 条，每日自动更新 | `UPDATE_TRACKERS=true`，`/config/tracker.log` 可查 |
| 做种 | seed-ratio=1.0, seed-time=0 | 上传量达到下载量即停，不占长期硬盘 |

**结论：下载端已经吃满带宽，没有优化空间。** 觉得慢多半是种子本身 peer 少。

### 上传端

| 配置 | 300MB 单文件中位耗时 |
|---|---|
| transfers=2, chunk=64M（旧） | 52s |
| **transfers=4, chunk=64M（现用）** | **28s** |
| transfers=8, chunk=64M | 48s |
| chunk=128M | 69s |

网络抖动很大（同配置单次 12–55s），必须多次测量取中位数。参数可用
`UPLOAD_TRANSFERS` / `UPLOAD_CHUNK` 覆盖。

### 本地计算

状态读写、番号识别、目录扫描都在毫秒级（见 `tests/bench.py`），不是瓶颈。

## 工作方式

1. 从消息里抽出磁力/直链（可混在文字里；一条消息多个链接会逐个处理）
   - 磁力会先经 whatslink.info 查元信息：文件名、体积、文件数、封面截图
   - 拿到体积后先判配额，超了直接拒绝，不会白下一晚
2. 识别番号：优先取消息里写的 `SSIS-123`，其次从文件名推断
   - 支持 `ABC-123`、`FC2-PPV-1234567`、`HEYZO-1234`
   - 自动跳过 `1080p`、`x264`、`WEB-DL` 这类干扰项
   - 识别不到就放 `unclassified-<时间戳>` 目录
3. 下载：磁力交给 `p3terx/aria2-pro` 容器（JSON-RPC 下发任务、轮询进度），直链走 httpx 流式
4. 整理：BT 常见的多层目录摊平，单文件重命名为 `<番号>.<ext>`，多文件加 `-1 -2` 前缀
5. 上传：`rclone copy` 到 `X-Drive/<番号>/`，超过 `DAILY_UPLOAD_GB` 直接拒收
6. 同一番号已存在时返回旧链接，不重复下载（`/retry` 可强制重下）

## 机器人命令

| 命令 | 作用 |
|---|---|
| 发磁力/直链 | 下载 → 按番号归档 → 存 Drive |
| `/retry` | 重试最近一条失败任务；也可 `/retry <磁力>` |
| `/list` | 列出已归档番号与 Drive 链接 |
| `/del <番号>` | 显示将删除的目录，加 `--yes` 执行（进 Drive 回收站，可恢复） |
| `/help` | 命令说明 |

## 前置

- Docker（含 compose v2）
- 一个 rclone 的 `gdrive` remote（OAuth 已授权，能 `rclone lsd gdrive:`）
- BotFather 给的 bot token

## 部署

```bash
git clone https://github.com/cloudcranes/x-drive /opt/x-drive
cd /opt/x-drive
cp .env.example .env && $EDITOR .env      # 填 token、ALLOWED_USER_IDS、RCLONE_CONFIG_HOST
docker compose pull && docker compose up -d
docker compose exec bot python -m app.main --check
```

镜像由 GitHub Actions 构建推送到 `ghcr.io/cloudcranes/x-drive`，内含 `rclone`。

磁力下载由单独的 `aria2` 服务负责（[p3terx/aria2-pro](https://p3terx.com/archives/docker-aria2-pro.html)）：
bot 只通过 JSON-RPC 下发任务，两边共用 `./downloads` 卷。要点：

- `ARIA2_RPC_SECRET` 必须设置，否则容器拒绝连接
- `LISTEN_PORT=6888` 需要在防火墙/路由器放行 TCP+UDP，否则 BT 只能被动连接，速度很慢
- `aria2-config/` 挂 `/config`，容器自己维护配置与 tracker 列表（`UPDATE_TRACKERS=true`）
- **下载管理已集成进看板**（`http://<host>:8080`）：右上「+ 新建下载」粘贴磁力/直链即可提交
- 点「预览信息」会先用 [whatslink.info](https://whatslink.info) 查文件名、体积、文件数与封面截图，确认无误再提交
- 提交后页面显示实时进度、速度与 Drive 链接；不需要单独的 AriaNg
不想用远端镜像就删掉 `.env` 里的 `BOT_IMAGE`，改用本机 `docker compose up -d --build`。

## 配置

见 `.env.example`，重点几项：

| 变量 | 说明 |
|---|---|
| `ALLOWED_USER_IDS` | 逗号分隔白名单，留空则拒绝所有人 |
| `MAX_FILE_MB` | 单个直链大小上限 |
| `DAILY_UPLOAD_GB` | 单日上传上限，0 = 不限 |
| `MAGNET_TIMEOUT` | 磁力下载超时（秒），默认 7200 |
| `PENDING_DIR` | 未完成任务的落盘目录，重启后自动补跑 |
| `METRICS_PATH` | 成功/失败分类计数 |

## 看板

`web` 服务把 `data/state.json` 渲染成番号卡片：系列与月份快捷筛选、搜索番号、单/多文件过滤，每 30 秒增量刷新（不打断正在进行的操作）。

![看板](docs/preview.jpg)

登录用 `WEB_AUTH=用户名:密码`，成功后下发 `HttpOnly` 签名 cookie（7 天），右上角可退出。未登录访问页面返回登录页，访问 `/api/*` 返回 401 JSON；`Authorization: Basic` 仍支持，方便脚本调用。

## 运行指标

- `bot` 每次任务结束按结果分类（`ok` / `partial` / `no_media` / `upload_fail` / `error`）写 `/data/metrics.json`
- `GET /api/metrics`：总数、成功率、最近成功时间、最近失败原因、待补跑数
- `GET /api/health`：`state.json` 是否可解析 + 待办数 + 归档计数（容器 healthcheck 用）

## 排障

| 现象 | 处理 |
|---|---|
| 磁力一直没进度 | 公网做种少；`aria2c` 无进度 3 分钟自动退出并留待办 |
| 回执「没找到可以下载的链接」 | 消息里必须有 `magnet:` 或 `http(s)://` |
| 番号识别错 | 在消息里显式写一次正确番号，程序优先用消息里的 |
| 上传失败 | `docker compose exec bot rclone lsd gdrive:` 手测；检查 `rclone.conf` 与 OAuth |
| 回执「未授权」 | 把 user id 加进 `ALLOWED_USER_IDS` 并 `docker compose up -d` |

## 已知限制

- 不做资源搜索：只下载你自己提供的磁力或直链
- 磁力速度取决于公网做种；未做内网穿透时无法接受外部连接
- 不转码：保留原始文件，播放兼容性取决于源
- 单个 Google 账号上传上限 750GB/日
- 同一番号视为同一部作品，重复下载需 `/retry`
- 进程重启会丢进行中的任务（已入库的不受影响），未完成的会按待办补跑

## 开发

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests -q
```

- `app/codes.py`：番号识别与目录名归一
- `app/fetcher.py`：磁力（aria2c）与直链下载、目录摊平
- `app/main.py`：TG 编排、去重、配额、待办补跑
- `app/web.py` + `app/webui.html`：零依赖看板
- `app/metrics.py`：结果分类计数
- `app/whatslink.py`：磁力元信息查询（文件名/体积/截图），失败带重试
- `app/pipeline.py`：下载 → 整理 → 命名，bot 与 web 共用同一套逻辑

共 31 个测试，`python -m pytest tests -q` 约 7 秒跑完。
