"""单容器入口：同一进程里同时跑 bot 长轮询与网页看板。

为什么合并：两个容器共用同一个镜像，各自付一份解释器与依赖的常驻开销
（实测 bot 37 MiB + web 65 MiB，合起来 41 MiB）。共享状态本来就是按
「跨进程」设计的（文件锁 + SQLite + state 表 + 落库的取消标记），
所以合并不需要改任何业务代码，只是少一个容器。

线程布局不能反：
  · 主线程：bot 的 run_polling —— python-telegram-bot 默认在启动时安装
    SIGINT/SIGTERM/SIGABRT 处理器，信号处理器只能在主线程注册；
  · 后台线程：web.main() —— HTTP 服务与 5 条调度线程本来就都是 daemon。

代价（有意接受）：故障域合并。网页半边异常退出时容器整份退出，交给
docker 的 restart 策略重启 —— 否则会剩下一个「bot 还在跑、界面没了」的
半死状态，比整份重启更难排查。

用法：
  python -m app.allinone            # 同时启动 bot 与网页
  python -m app.allinone --check    # 自检 Telegram / 云添加 / CloudDrive2
  python -m app.allinone --health   # 容器 healthcheck：上面的自检 + 网页 /api/health
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import urllib.request

from . import main as botmain
from . import web as webmod

log = logging.getLogger("x-drive.allinone")

# healthcheck 探网页用多少秒：本机回环，正常是毫秒级
WEB_PROBE_TIMEOUT = float(os.getenv("WEB_PROBE_TIMEOUT", "5") or 5)


def _die(code: int) -> None:
    """立刻结束整个进程。

    用 os._exit 而不是 sys.exit：这是在**子线程**里调用，抛 SystemExit 只会
    结束那一条线程，主线程的 bot 还在轮询 —— 那正是我们要避免的半死状态。
    单独包一层是为了测试能替换掉它（真调用会把 pytest 进程带走）。
    """
    os._exit(code)


def _web_host() -> str:
    """探网页用哪个地址：监听 0.0.0.0 时回环即可，绑了具体网卡就用那个。"""
    bind = str(getattr(webmod, "BIND", "") or "")
    if bind in ("", "0.0.0.0"):
        return "127.0.0.1"
    if bind == "::":
        return "::1"
    return bind


def _probe_web(timeout: float = WEB_PROBE_TIMEOUT) -> tuple[bool, str]:
    """探一下本机的网页半边：/api/health 只读 state 文件，不触发任何外呼。

    绕过代理（ProxyHandler({})）：容器里若残留代理变量，回环地址会被当成
    外网转发出去，健康检查就会莫名其妙地红。
    """
    url = f"http://{_web_host()}:{webmod.PORT}/api/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:            # noqa: BLE001 探活失败要把原因原样带出来
        return False, f"网页不可达（{url}）：{type(exc).__name__}: {exc}"
    if not data.get("ok"):
        return False, f"网页报告异常：{data}"
    return True, (f"网页 OK（待办 {data.get('pending', 0)}，"
                  f"归档 {data.get('tasks', 0)}，文件 {data.get('files', 0)}）")


async def _health(cfg: botmain.Config) -> bool:
    """容器健康检查：两半都得在。bot 自检沿用原来的三行结论，不改判据。"""
    bot_ok = await botmain.self_check(cfg)
    web_ok, why = _probe_web()
    print(f"网页: {why}")
    print("容器健康检查", "通过" if bot_ok and web_ok else "未通过")
    return bot_ok and web_ok


def _run_web() -> None:
    """网页线程主体：它退出 = 整份服务不再完整，让 docker 重启整个容器。"""
    try:
        webmod.main()
    except BaseException:               # noqa: BLE001 线程里的任何异常都要收口
        log.exception("网页半边异常退出，容器退出以便 docker 重启整份服务")
        _die(1)
        return
    log.warning("网页服务已停止，容器退出")
    _die(0)


def _start_web_thread() -> threading.Thread:
    """把网页放进后台线程。

    daemon=True 是必需的：docker stop 发 SIGTERM → PTB 在主线程收尾 → 主线程
    返回后进程就该退出；非 daemon 线程会让进程卡在 serve_forever 上直到被 SIGKILL。
    """
    thread = threading.Thread(target=_run_web, daemon=True, name="web-main")
    thread.start()
    return thread


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # 两个被合并的入口各自在 main() 里装文件日志（logbuf.install，幂等），
    # 这里不重复；谁先起来谁装。
    if "--check" in sys.argv or "--health" in sys.argv:
        cfg = botmain.Config.from_env()
        # --check 与原来的 `python -m app.main --check` 等价（只查 bot 侧环境）；
        # --health 多探一条 /api/health，给容器 healthcheck 用。
        probe = _health if "--health" in sys.argv else botmain.self_check
        sys.exit(0 if asyncio.run(probe(cfg)) else 1)

    # 先起网页再起 bot：bot 的 run_polling 会一直占着主线程不返回。
    _start_web_thread()
    log.info("单容器启动：网页线程 web-main + 主线程 bot 长轮询")
    botmain.main()


if __name__ == "__main__":
    main()
