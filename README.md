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
| 下载器 | 迅雷容器（自带 P2P + 超级通道加速） | 参数不可调，加速开关在面板里 |
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

1. 从消息里抽出磁力/直链（可混在文字里）。**一条消息多个链接会逐个处理**，
   并且每个链接配自己的番号：`SSIS-123 magnet:a ABP-456 magnet:b` →
   两个番号各归其后那个链接（见 `codes.pair`）
   - 磁力会先经 whatslink.info 查元信息：文件名、体积、文件数、封面截图
   - 拿到体积后先判配额，超了直接拒绝，不会白下一晚
2. 识别番号：优先取消息里写的 `SSIS-123`，其次从文件名推断
   - 支持 `ABC-123`、`FC2-PPV-1234567`、`HEYZO-1234`
   - 自动跳过 `1080p`、`x264`、`WEB-DL` 这类干扰项
   - 识别不到就放 `unclassified-<时间戳>` 目录
3. 目录：迅雷按任务名建 `<TMP_ROOT>/<键>`，本程序在同一目录放 `.x-drive-owner` 标记；
   没有标记的目录一律不接管、不清理。整理在工作区 `STATE_DIR/state/<键>` 里做，
   下载原件只读 —— 失败时原件与工作区都保留，成功上传后才删除。
4. 磁盘闸门：下载前算「剩余 − 已放行额度 − `DISK_RESERVE_GB`」（体积查不到时按同系列历史中位数估），
   放不下就排队等（不硬塞把盘写满，写满会让迅雷报错并留一堆残片）
4. 下载：磁力和直链统一投给外部的**迅雷容器**（`cnk3x/xunlei`）；
   迅雷没有查询接口，进度按落盘字节估算，连续 `STALL_TIMEOUT` 秒无新字节判死种
5. 整理：BT 常见的多层目录摊平，视频优先；用 ffprobe 量时长挑主体，
   有正片（≥20 分钟）时丢掉 5 分钟以下的预告/样片，量不到时长就按体积、一个都不丢；
   只有图片的图包照常按图片归档，
   单文件重命名为 `<番号>.<ext>`，多文件加 `-1 -2` 前缀；
   种子夹带的 `.url/.txt` 广告与小样本一并清掉，不跟着上 Drive
6. 上传：`rclone copy` 到 `X-Drive/<番号>/`，超过 `DAILY_UPLOAD_GB` 直接拒收
7. 同一番号已存在时返回旧链接，不重复下载（`/retry` 可强制重下）
8. 失败自动重试（指数退避，按网络/死种/配额/配置分级），致命错误不重试
9. 任务状态写 SQLite（`JOBS_DB`）：同番号并发提交由唯一索引挡下；重启后自动对账，
   把未完成任务重新入队并复用原迅雷任务，而不是从头再下

## 机器人命令

| 命令 | 作用 |
|---|---|
| 发磁力/直链 | 下载 → 按番号归档 → 存 Drive |
| 发纯番号 | 自动搜磁力并下载（字幕/高清/体积优先） |
| `/find <番号>` | 只列候选磁力，不下载 |
| `/retry` | 重试最近一条失败任务；也可 `/retry <磁力>` |
| `/list` | 列出已归档番号与 Drive 链接 |
| `/del <番号>` | 显示将删除的目录，加 `--yes` 执行（进 Drive 回收站，可恢复） |
| `/help` | 命令说明 |

## 前置

- Docker（含 compose v2）
- 一个 rclone 的 `gdrive` remote（OAuth 已授权，能 `rclone lsd gdrive:`）
- BotFather 给的 bot token

## 部署

部署脚本会检查三样东西：rclone 配置、`.env` 里的 `XUNLEI_DOWNLOADS_HOST` 指向的目录存在、
本机 2345 的迅雷面板可达。`data/`（状态、任务表、预览缓存）与 `work/`（整理工作区）都会建好。

```bash
git clone https://github.com/cloudcranes/x-drive /opt/x-drive
cd /opt/x-drive
cp .env.example .env && $EDITOR .env      # 填 token、ALLOWED_USER_IDS、RCLONE_CONFIG_HOST
docker compose pull && docker compose up -d
docker compose exec bot python -m app.main --check
```

镜像由 GitHub Actions 构建推送到 `ghcr.io/cloudcranes/x-drive`，内含 `rclone`。

### 容器内验收（部署前必跑）

宿主机能连面板，不代表容器能连。容器里的 `127.0.0.1` 是容器自己，`.env` 里把
`XUNLEI_URL` 写成 `http://127.0.0.1:2345` 就会出现「任务投递成功但永远不动」。
所以 `XUNLEI_URL` 默认留空，由 compose 填 `host.docker.internal:2345`。

```bash
bash scripts/verify.sh        # 需在有 docker 权限的账号下运行
```

它会依次验证：配置可解析、镜像可构建、容器内自检（telegram / 迅雷 / 下载目录 / 工作区 / rclone）、
**容器写进 `/downloads` 的文件在宿主机同一路径可见**（证明两边是同一份挂载）、
以及从容器内真正调通迅雷面板。任一项失败即非零退出。

`python -m app.main --check` 现在会额外报告「下载目录」与「工作区」的可写性 ——
这两项是容器部署最常见的两个坑（没挂载、权限不对）。

磁力/直链下载交给**外部的迅雷容器**（`cnk3x/xunlei`，`network_mode: host`，面板 2345）。
本程序不管理它，只投任务、看落盘。要点：

- `XUNLEI_PAN_AUTH`：一般不用填。面板首页 HTML 里的 `function uiauth(){return "<JWT>"}`
  就是 token，程序每次取首页自取并缓存一分钟（过期自动续）。只有面板低于 3.21 才需要手填兜底
- `XUNLEI_PARENT_ID`：一般不用填。程序在真实 space 里按 `RealPath == /downloads/` 自己找
  「下载到」目录；没有这个默认目录时才需要手填
- 真实 space（`device_id#<hex>`）不用配，运行时由 `device/info/watch` 的 `target` 取；
  用占位 `device_id#` 投任务会进云盘阴影空间，接口回 200 但下载器永不认领（实测踩过）
- `XUNLEI_DOWNLOADS_HOST`：迅雷容器的 `/downloads` 挂载点在本机的路径，本程序把它挂成自己的 `/downloads`
- 任务名 = 目录名：迅雷按任务名在下载目录下建同名子目录，本程序据此认领产物
- `sub_file_index` 是**选中区间**不是序号：单文件 `--1,`、多文件 `0-<n-1>`；传 `0` 只会下第一个文件
  （实测只落了一个 `.srt` 字幕，视频一字节没下）
- 进度/速度/错误码取自 `drive/v1/tasks`（`progress`、`params.speed`、`params.error`）；
  面板报完成后仍要等落盘安静 `XUNLEI_SETTLE` 秒，避免 P2P 滞后误判
- 停止任务走 `method/patch/drive/v1/task`（`spec.phase=pause`），删除走
  `method/delete/drive/v1/tasks`；普通 `DELETE` 会被判 402
- **下载管理已集成进看板**（`http://<host>:8080`）：右上「+ 新建下载」粘贴磁力/直链即可提交
- 点「预览信息」会先用 [whatslink.info](https://whatslink.info) 查文件名、体积、文件数与封面截图，确认无误再提交
- 候选行另有「预览」：只查**选中的那一条**磁力，按 infohash 缓存（成功 7 天、失败 10 分钟），
  搜索时不批量打外部接口
- 提交后页面显示落盘字节、估算速度与 Drive 链接；迅雷自己的面板管队列
不想用远端镜像就删掉 `.env` 里的 `BOT_IMAGE`，改用本机 `docker compose up -d --build`。

## 配置

见 `.env.example`，重点几项：

| 变量 | 说明 |
|---|---|
| `ALLOWED_USER_IDS` | 逗号分隔白名单，留空则拒绝所有人 |
| `MAX_FILE_MB` | 单个直链大小上限 |
| `DAILY_UPLOAD_GB` | 单日上传上限，0 = 不限 |
| `XUNLEI_URL` | 迅雷面板地址；容器内要打宿主机地址 |
| `XUNLEI_PAN_AUTH` | 面板 token，从 DevTools 抄，约 3 个月过期 |
| `XUNLEI_PARENT_ID` | 可选，迅雷「下载到」目录 id；留空则自动识别 |
| `XUNLEI_DOWNLOADS_HOST` | 迅雷 `/downloads` 挂载点在本机的路径 |
| `XUNLEI_POLL` / `XUNLEI_SETTLE` | 落盘轮询间隔 / 静默多久算写完（秒） |
| `STATE_DIR` | 整理工作区（与迅雷下载目录分开，失败不动原件） |
| `JOBS_DB` | 任务表路径（默认 `data/jobs.db`，SQLite） |
| `PREVIEW_DB` | 磁力预览缓存（默认 `data/preview.json`） |
| `PANEL_CLEAN_DAYS` | 面板失败任务记录保留天数，0 = 不自动清（默认 0） |
| `MAGNET_TIMEOUT` | 单个任务总超时（秒），默认 7200 |
| `STALL_TIMEOUT` | 连续多久无新字节判死种（秒），默认 900，0 = 不检测 |
| `CONCURRENCY` | bot 同时处理的任务数 |
| `PENDING_DIR` | 未完成任务的落盘目录，重启后自动补跑 |
| `METRICS_PATH` | 成功/失败分类计数 |
| `DISK_RESERVE_GB` | 磁盘闸门：永远预留不参与分配的空间，默认 5 |
| `DISK_CHECK_INTERVAL` | 排队任务复查空间的间隔（秒），默认 30 |
| `DISK_QUEUE_TIMEOUT` | 排队最久等多久（秒），0 = 一直等 |
| `UPLOAD_TRANSFERS` / `UPLOAD_CHUNK` | 上传并发与分片，默认 4 / 64M（实测最优） |
| `RETRIES` / `RETRY_BASE_DELAY` | 失败重试次数与首次退避（指数增长，封顶 60s）；按失败性质分级：网络给满次数、死种只给一次且退避 ×6、配额留待办等明天、配置错立刻报 |
| `SUBS_INTERVAL` | 订阅轮询间隔（秒），默认 3600，最小 300 |
| `CLEANUP_MAX_GB` | 归档回收：总容量上限，0 = 关闭（默认关） |
| `CLEANUP_KEEP_RECENT` | 归档回收：永远保留最近几个，默认 10 |
| `CLEANUP_OLDER_DAYS` | 归档回收：只考虑这么久的归档 |

## 看板

`web` 服务把 `data/state.json` 渲染成番号卡片，每 30 秒增量刷新（不打断正在进行的操作）。

- 筛选：系列 / 演员 / 片商三个维度 + 月份，搜索框（`/` 聚焦），视频/图片过滤，四种排序
- 卡片带封面与元数据（标题、日期、时长、片商、类别、演员），来自 javdb / javbus 缓存
- 下载队列面板：实时进度、速度、停止、清空已完成；空间不够自动排队；
  另列迅雷面板里未结束的任务（`/api/xunlei`），可直接暂停/继续/删除（`/api/xunlei/control`），
  不必再开一个 2345 页面
- 新建下载：「预览信息」先查体积/文件数/封面，确认再提交；可勾选按番号去重
- 订阅面板：按演员或番号系列订阅，设置每轮条数与单片体积上限
- 归档改名：卡片上「改名」，Drive 目录 `X-Drive/<旧>` → `<新>` 并同步归档记录（不重传文件）
- 智能整理（工具栏「规范名」）：先把认得出的未分类归档收编（查元数据后改名），
  再按规范批量修正（先出清单，撞车的整组跳过）；命名规范为 `YYYY-MM-DD 番号 标题`
- 清理失败任务：侧栏底部按钮，删 7 天前的失败任务记录（不动文件）；`PANEL_CLEAN_DAYS>0` 时每日自动跑
- 归档删除：显示将删除的内容，需二次确认，进 Drive 回收站可恢复

![看板](docs/preview.jpg)

登录用 `WEB_AUTH=用户名:密码`，成功后下发 `HttpOnly` 签名 cookie（7 天），右上角可退出。未登录访问页面返回登录页，访问 `/api/*` 返回 401 JSON；`Authorization: Basic` 仍支持，方便脚本调用。

## 元数据

归档后按番号抓标题、日期、时长、导演、片商、评分、类别、演员与封面，展示在看板卡片上。

- 优先用 javbus 的 JSON 接口（字段更全），挂了自动回退解析 javdb 页面
- 抓到的结果缓存到 `/data/meta.json`（`META_PATH`），一次抓取终身复用，命中缓存零网络请求
- 请求之间留 `META_MIN_INTERVAL`（默认 1.5s）间隔，避免把站点打疼
- **番号必须精确匹配**：搜 `SSIS-123` 时站点会顺带返回 `SSIS-023`、`SSIS-103`，
  宁可这个番号不显示信息，也不能把别的片子挂上去
- 元数据是锦上添花：任何失败都返回空，绝不影响下载上传
- 老归档可用 `GET /api/meta/backfill?limit=50` 批量补抓

## 刮削与元数据附件（MetaTube）

接的是**你自己部署的** MetaTube 后端，本程序不重部署、不改它的配置。

实测接口契约（v1.4.0-c625846）：

| 请求 | 结果 | 鉴权 |
|---|---|---|
| `GET /` | `{"app":"metatube","version":...}` | 不需要 |
| `GET /v1/providers` | 启用的刮削源（含 JavBus 等 29 个） | 不需要 |
| `GET /v1/movies/search?q=<番号>` | **完整影片对象**（标题/封面/日期/简介…） | 需要，`Authorization: Bearer <TOKEN>` ✅ 实测 200 |
| `GET /v1/movies/search?q=<番号>&provider=JavBus` | 同上 | 同上 ✅ 实测 200 |
| `GET /v1/movies/<id>/<provider>` | v1.4.0 实测 `404 provider not found` | 不可用 |

路由是**查询串形式**；路径式 `/v1/movies/search/<q>/<provider>` 实测 404。
搜索接口本身返回完整字段，所以不需要再打详情接口。

鉴权踩过的坑：服务端容器的 `TOKEN` 环境变量才是有效密钥。之前用的值与该变量
不符，12 种带法全都 401（`/` 与 `/v1/providers` 无需鉴权所以看起来「服务是好的」）。
排查时看 `docker inspect <容器> --format '{{range .Config.Env}}{{println .}}{{end}}'`。

上传前按番号刮一次，生成四件附件跟着视频一起进归档：

```text
CODE-001/
├── CODE001.mp4
├── movie.nfo          # 只写有值的字段，XML 已转义
├── metadata.json      # 完整字段 + 来源 + 格式版本 + 锁定字段
├── poster.jpg         # 成功取到封面时才生成
└── manifest.json      # 文件名、大小、媒体探测；不写自身哈希
```

- **先验证番号再匹配**：服务返回的番号规范化后必须等于请求的番号，
  标题像不算数；对不上就丢弃，宁可留空也不把别人的元数据写进归档
- 缺字段留空，不编造；部分返回**只合并不覆盖**（缓存同款规则）
- 刮削失败**不阻断视频上传**，任务里记 `scrape_pending`，在 `/api/scrape/status`
  的 `pending` 列表里可见
- 封面下载限 12MB，超限放弃，不为一张图吃内存
- 手工改过的字段会被**自动锁定**，之后重新刮削不覆盖它
- `POST /api/scrape/preview {code}`：差异预览，只列会变的字段，**不动文件不改记录**
- `POST /api/scrape/sync {code, fields, resync, confirm}`：补刮 / 手改 /
  只把附件推到 Drive（`copyto` 单文件，不重传视频）
- token 只在后端用：不进前端、日志、`metadata.json` 或备份包

配置见 `.env.example` 的「刮削」一节。**注意**：实测本机直连该后端 TCP 都不通，
必须配 `METATUBE_PROXY` 才能到。

## 通知

复用 Telegram（`TELEGRAM_BOT_TOKEN` + 新增 `TELEGRAM_CHAT_ID`）：

- 归档完成、最终失败、额度等待、长期无资源、每日汇总
- 按事件键**去重**（`NOTIFY_DEDUPE_WINDOW`，默认 1800s）：重试刷屏会把用户训练成忽略通知
- 发送失败只记日志，绝不影响任务结果
- `GET /api/digest` 手动触发当日汇总（同一天只真正发一次）

## 归档校验、重复检查与备份

- `GET /api/archive/verify?code=`（不给 code 就批量，`?limit=50`）：
  本地清单 vs 远端 `lsjson`，结论分「校验通过 / 缺文件 / 大小异常 / 无清单 / 检查失败」。
  **默认只报告**：不会自动补传，也不会删远端
- `GET /api/archive/dups`：三层重复检查 —— 同番号多条记录、不同番号同 infohash、
  同番号多版本。同样只报告
- `POST /api/backup`：用 sqlite `backup` API 做一致快照（不是拷文件，WAL 下会缺页），
  默认写 `/data/backup-<时间>.db`。凭据都在环境变量里，备份包天然不含 token
- `POST /api/restore/check {source}`：只读检查备份里有什么、哪些任务当时在跑
- `POST /api/restore/apply {source, confirm}`：恢复前**先自动备份当前库**，
  恢复后请先对账（任务列表 vs 迅雷面板）再让调度继续

## 订阅自动化

盯住某个**演员**或**番号系列**，出新片自动提交下载。默认走 **JavBus API**
（`/api/movies?filterType=star|series`）：列表给番号与日期，详情给 gid/uc，磁力接口给体积与字幕/高清标记。

- 订阅存「来源 + ID」：JavBus 的 `filterValue` 只认站内 ID（演员如 `w5a`），不认名字；
  旧 JavDB 订阅保留 `source=javdb`，不会拿 JavDB 的 ID 去打 JavBus
- 每轮翻 2 页并做**重叠回查**：游标当天及之后的片子再确认一次，
  晚出磁力或详情页临时失败的作品不会被永久漏掉。
  积压超过两页时**游标不动**（`caught_up=false`），下一轮接着往回翻，
  直到真的翻到旧游标边界才前移 —— 以前翻两页就宣布追平，中间那段永久漏订。
- 订阅游标与发现待办都存进任务库（`/data/jobs.db` 的 `state` 表），
  **同一个事务提交**：待办没写进去，游标就不许前移。老 `subs.json` / `backlog.json`
  首次运行会自动导入一次。

看板右上「订阅」面板配置，后台每 `SUBS_INTERVAL` 秒轮询一次，也可「立即检查全部」手动触发。

### 待办中心与愿望单

`GET /api/subs` 的 `backlog` 字段列出每条待办，并解释「为什么还没下」：

| 状态 | 含义 | 界面说明 |
|---|---|---|
| `pending` | 等资源或退避中 | 还有几分钟再查 / 下一轮就试 |
| `queued` | 已提交下载 | 关联任务当前阶段与错误 |
| `skipped` | 按策略或人工跳过 | 不再重试 |
| `done` | 已归档 | 闭环结束 |

- `POST /api/backlog/retry {code}`：清掉退避与已试候选，立刻重试
- `POST /api/backlog/ignore {code}`：主动放弃，标 `skipped`
- `POST /api/wish/add {codes, max_size_gb}`：**番号愿望单**，只按番号，
  与订阅共用待办、调度与去重，不需要先订阅演员
- 提交后**失败会回流**：任务失败/取消/记录消失时自动放回 `pending` 并退避重试，
  归档成功后转 `done` —— 以前标成 `queued` 就再没人管，那一部永久漏掉

候选规则是**先过滤再排序**：体积上限、已试过的 infohash 先剔掉，再从剩下的里
按「有字幕 → 高清 → 体积大」挑。首选超限就整部放弃是以前的 bug。
同一番号最多换 3 条候选（`backlog.MAX_TRIES`），按 infohash 记，避免反复撞同一条死种。

订阅面板会顺带列出「看得出你常看」的演员/系列（按已归档元数据统计），
有 javdb ID 的可一键订阅，解析不到时可手填 ID/链接（填过即缓存）；
ID 由 `meta.resolve_id` 用名字搜站内页解析，
结果落 `IDS_PATH`（默认 `/data/ids.json`）缓存 —— 解析不到就显示「缺 ID」，不猜。

三重安全线，缺一不可：

- **游标**：只抓订阅之后出的片，不回填几百部历史
- **去重**：已归档、正在下载的番号一律跳过
- **上限**：每轮最多几条 + 单片体积上限，防止一次轮询吃光磁盘和 Drive 配额

## 归档回收

只做决策、不碰文件（`app/cleanup.py`），删除动作由 web 执行：

- 永远保留最近 `CLEANUP_KEEP_RECENT` 个（默认 10），避免误配一条清空
- 可按总容量上限（`CLEANUP_MAX_GB`）或天数（`CLEANUP_OLDER_DAYS`）淘汰最旧的
- 正在下载/上传中的番号绝不入选
- **默认关闭且默认 dry-run**：`/api/cleanup/run` 不带 `confirm=true` 只返回计划清单

## 运行指标

- `bot` 每次任务结束按结果分类（`ok` / `partial` / `no_media` / `upload_fail` / `error`）写 `/data/metrics.json`
- `GET /api/metrics`：总数、成功率、最近成功时间、最近失败原因、待补跑数
- `GET /api/health`：`state.json` 是否可解析 + 待办数 + 归档计数（容器 healthcheck 用）

## 排障

| 现象 | 处理 |
|---|---|
| 磁力一直没进度 | 先自动「暂停→继续」重连一次，仍不动才判死种并留待办（重试只会拿回同一条任务） |
| 报「不在 TMP_ROOT 下」 | 迅雷下载目录与本程序挂载点不一致，改 `XUNLEI_DOWNLOADS_HOST` 或 `TMP_ROOT` |
| 投递成功但一直是 0% | 检查 `XUNLEI_PARENT_ID` 是否是真实 space 下的目录；占位 space 会进阴影空间 |
| 只下来一个字幕/小文件 | 投递参数的 `sub_file_index` 被写成单个序号了，应为 `0-<n-1>` |
| 回执「没找到可以下载的链接」 | 消息里必须有 `magnet:` 或 `http(s)://` |
| 番号识别错 | 在消息里显式写一次正确番号，程序优先用消息里的 |
| 上传失败 | `docker compose exec bot rclone lsd gdrive:` 手测；检查 `rclone.conf` 与 OAuth |
| 回执「未授权」 | 把 user id 加进 `ALLOWED_USER_IDS` 并 `docker compose up -d` |

## 按番号搜磁力

只按番号查，不做关键词乱搜；多源、先到先得，`SOURCES` 控制启用与顺序：

| 源 | 端点 | 特点 |
|---|---|---|
| javbus | `/api/magnets/{code}` | JSON，有字幕/高清标记；**无 FC2 条目** |
| sukebei.nyaa.si | `/?page=rss&q=CODE` | RSS，给 infoHash + **做种数**（磁力自拼 tracker） |
| bt4gprx.com | `/search?q=CODE&page=rss` | RSS，内联 magnet，最省事 |

- FC2 番号（`FC2-PPV-…`）javbus 必然 404，路由自动跳过，只问 sukebei / bt4g
- 排序：有字幕 → 高清 → 做种多 → 体积大；同一 infoHash 去重（不分大小写）
- 单源失败只记日志，继续下一个；全失败返回空，不抛

接入点：

- bot：只发番号（如 `SSIS-123`）→ 自动搜磁力，挑排序第一条下载，回执注明来源
- bot：`/find SSIS-123` → 只列前 5 条候选（体积/字幕/高清 + 链接），选一条转发即下载
- 看板：新建面板「搜磁力」→ 顶部先显示资源信息（封面 / 标题 / 日期 / 时长 / 片商 / 演员 / 类别），
  点卡片打开**番号详情页**（封面大图 + 全部字段 + 预览图墙 + javbus 原页链接），
  再选候选点「用这条」填进输入框，仍需手动点「开始下载」（不自动提交）
- FC2 这类 javbus 没有条目的番号会返回 0 条，属正常

## 已知限制

- 搜索范围：javbus 有磁力条目的番号，以及 sukebei / bt4g 收录的番号（FC2 走后两者）
- 资源信息来自 javbus 元数据；FC2 这类查不到的番号只显示标题与体积（元数据缺失时不编造）
- 磁力速度取决于公网做种；未做内网穿透时无法接受外部连接
- 不转码：保留原始文件，播放兼容性取决于源
- 单个 Google 账号上传上限 750GB/日
- 同一番号视为同一部作品，重复下载需 `/retry`
- 重启后**任务不再丢失**：状态在 `/data/jobs.db`，恢复时会重新过磁盘闸门；
  自己的迅雷任务靠任务 id / infohash 认回并续接，别人的同种子任务一律拒绝接管
- **MetaTube 已实测打通**（2026-10-23）：`SSIS-123`、`ABP-777` 均返回正确标题、
  日期、封面与 provider=JavBus；`FC2-PPV-1234567` 无结果属正常（JavBus 无 FC2 条目）
- 搜索接口不返回 `runtime` / `actors` / `genres` 时，这些字段留空（不编造）；
  NFO 里对应行就不出现
- 若后端在境外且本机直连超时，需配置 `METATUBE_PROXY` 走代理；本环境实测
  直连该主机的 HTTP 端口与 SSH 端口均 TCP 超时，经本地代理才通
- 容器内验收脚本已改为独立测试镜像，但本轮**未执行**（会构建镜像、需要 docker）

## 开发

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests -q
```

| 模块 | 职责 |
|---|---|
| `app/codes.py` | 番号识别、规范名（`canonical`）、番号与链接配对、目录名归一 |
| `app/core.py` | 状态读写（带 `flock` 的原子合并）、URL 抽取、大小格式化 |
| `app/fetcher.py` | 投递迅雷、盯落盘判完成、视频筛选、BT 多级目录摊平 |
| `app/xunlei.py` | 迅雷面板内部接口（token 自取、space 解析、投递、查进度、暂停/删除） |
| `app/pipeline.py` | 下载 → 整理 → 命名 → 清广告，bot 与 web 共用 |
| `app/diskspace.py` | 磁盘空间闸门与额度计算 |
| `app/retry.py` | 指数退避重试、致命错误判定 |
| `app/metrics.py` | 结果分类计数与各阶段耗时 |
| `app/whatslink.py` | 磁力元信息（文件名/体积/截图），失败带重试 |
| `app/javbus.py` | javbus JSON 元数据源 + 按番号搜磁力（`search_magnets`） |
| `app/sources.py` | 多源磁力搜索：javbus / sukebei / bt4g，路由、去重、排序 |
| `app/meta.py` | 元数据抓取 + 磁盘缓存（javbus → javdb 回退）+ 名字→ID 解析 |
| `app/subscribe.py` | 订阅解析（演员页/系列页/磁力列表） |
| `app/cleanup.py` | 归档回收的纯决策逻辑 |
| `app/main.py` | TG bot 编排、去重、配额、待办补跑 |
| `app/web.py` + `webui.html` | 零依赖看板 + 后台任务 |

共 266 个测试，`python -m pytest tests -q` 约 50 秒跑完。`tests/bench.py` 是本地计算基准。
