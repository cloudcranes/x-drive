#!/usr/bin/env bash
# 容器内验收：验证「容器视角」下真正会用到的东西。
# 用法：ENV_FILE=/tmp/verify.env bash scripts/verify.sh
#
# 三条纪律：
#   1. 只用独立项目名 + 唯一镜像标签 + 临时 data/work 目录，绝不碰正式部署的数据；
#   2. 收尾只清本轮创建的资源，不对任意项目执行 down -v；
#   3. 任何一项失败都必须让退出码非零 —— 以前挂载写入失败也会打印「验收通过」。
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ENV_FILE="${ENV_FILE:-.env}"
PROJECT="${PROJECT:-xdverify-$$}"
TAG="xdverify:$$"
COMPOSE_FILE="scripts/verify-compose.yml"
DATA_DIR="$(mktemp -d)"
WORK_DIR="$(mktemp -d)"
fail=0

say() { printf '%s\n' "$*"; }
ok()  { printf '✓ %s\n' "$*"; }
bad() { printf '✗ %s\n' "$*" >&2; fail=1; }

[ -f "$ENV_FILE" ] || { echo "缺少 $ENV_FILE" >&2; exit 1; }
[ -f "$COMPOSE_FILE" ] || { echo "缺少 $COMPOSE_FILE" >&2; exit 1; }

# 三个临时值必须导出：compose 的 ${...} 插值读的是环境变量，不是 .env 里的内容
export ENV_FILE PROJECT TAG DATA_DIR WORK_DIR
compose() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }

cleanup() {
  compose down -v --remove-orphans >/dev/null 2>&1
  docker rmi -f "${TAG}-runtime" "${TAG}-test" >/dev/null 2>&1
  rm -rf "$DATA_DIR" "$WORK_DIR"
}
trap cleanup EXIT

say "== 0. 环境"
say "   project=$PROJECT  tag=$TAG"
say "   data=$DATA_DIR  work=$WORK_DIR"
docker info >/dev/null 2>&1 || { bad "docker daemon 不可达"; exit 1; }

say "== 1. 配置可解析"
if compose config >/dev/null 2>&1; then ok "compose config"; else bad "compose config 失败"; fi

say "== 2. 运行镜像可构建"
export TAG="${TAG}-runtime"
if compose build web >/dev/null 2>&1; then ok "运行镜像已构建"; else bad "运行镜像构建失败"; exit 1; fi

say "== 3. 容器内单元测试（测试阶段镜像，带 pytest 与 tests/）"
# 以前这里跑的是运行镜像，里面既没有 pytest 也没有 tests，那一项永远是假的通过
if BUILD_TARGET=test TAG="${TAG}-test" compose build web >/dev/null 2>&1; then
  if BUILD_TARGET=test TAG="${TAG}-test" compose run --rm --no-deps web \
       python -m pytest tests -q -p no:cacheprovider >/tmp/xdverify-tests.log 2>&1; then
    ok "单元测试通过（$(grep -oE '[0-9]+ passed' /tmp/xdverify-tests.log | tail -1)）"
  else
    bad "容器内单元测试未通过，见 /tmp/xdverify-tests.log"
  fi
else
  bad "测试镜像构建失败"
fi

say "== 4. 容器内自检（telegram / 迅雷 / 挂载 / 权限 / rclone）"
export TAG="${TAG}-runtime"
if compose run --rm --no-deps web python -m app.main --check; then
  ok "自检通过"
else
  bad "自检未通过：重点看「迅雷」「下载目录」「工作区」三行"
fi

say "== 5. 下载目录与宿主机是同一份"
marker=".x-drive-verify-$$"
if compose run --rm --no-deps web sh -c "touch /downloads/$marker" >/dev/null 2>&1; then
  host_dir="$(grep '^XUNLEI_DOWNLOADS_HOST=' "$ENV_FILE" | cut -d= -f2-)"
  if [ -n "$host_dir" ] && [ -e "$host_dir/$marker" ]; then
    ok "共享挂载一致"
    rm -f "$host_dir/$marker"
  else
    bad "容器写的 /downloads/$marker 在宿主机 ${host_dir:-（未配置）} 看不见"
  fi
else
  bad "容器内无法写入 /downloads（挂载缺失或只读）"
fi

say "== 6. 面板从容器内可达"
if compose run --rm --no-deps web python - <<'PY'
import asyncio, os
from app import xunlei
async def main():
    xl = xunlei.Xunlei(os.getenv("XUNLEI_URL",""), os.getenv("XUNLEI_PARENT_ID",""),
                       os.getenv("TMP_ROOT","/downloads"), os.getenv("XUNLEI_PAN_AUTH",""))
    async with xl.client() as c:
        await xl.alive(c)
    print("panel-ok")
asyncio.run(main())
PY
then ok "容器到面板链路正常"; else bad "容器连不上迅雷面板（XUNLEI_URL 不要写 127.0.0.1）"; fi

say "== 7. 状态库可写、可迁移、可备份"
if compose run --rm --no-deps web python - <<'PY'
import os, sqlite3, tempfile
from app.store import Store
path = os.getenv("JOBS_DB", "/data/jobs.db")
st = Store(path)
st.start("verify-1", "VERIFY-001", "magnet:?xt=urn:btih:" + "0" * 40)
assert st.active_codes() >= {"VERIFY-001"}, "任务表写入后读不回来"
st.finish("verify-1", "done")
# 备份 API：一致快照，不能用「拷文件」糊弄
bak = tempfile.mktemp(suffix=".db")
with sqlite3.connect(path) as src, sqlite3.connect(bak) as dst:
    src.backup(dst)
st2 = Store(bak)
assert st2.get("verify-1")["stage"] == "done", "备份里读不到刚写的记录"
print("store-ok", st.migrate())
PY
then ok "状态库与备份可用"; else bad "状态库写入/备份失败"; fi

say "== 8. 工作区归属与清理隔离（离线）"
if compose run --rm --no-deps web python -m pytest tests/test_workspace.py tests/test_store.py \
     -q -p no:cacheprovider >/dev/null 2>&1; then
  ok "归属与迁移测试通过"
else
  bad "归属或迁移测试失败"
fi

say ""
if [ "$fail" -eq 0 ]; then echo "验收通过"; else echo "验收未通过"; fi
exit "$fail"
