"""失败重试策略：指数退避 + 次数上限 + 可中断。

单独成模块是为了能纯逻辑测试——真实重试要等几分钟，不适合塞进集成测试。
bot 与 web 两条链路共用同一套判断，避免两边行为不一致。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

# 不该重试的错误：重试只会浪费时间，还可能把配额撞得更死
FATAL_PATTERNS = (
    "uploadLimitExceeded",      # Drive 当日配额用完
    "quota",                    # 同上（各种大小写写法）
    "配额",                      # 中文提示：配额已用完
    "磁盘空间不足",
    "userRateLimitExceeded",
    "超过 DAILY_UPLOAD_GB",
    "没有找到视频文件",          # 下载完了但没内容，再试也一样
    "已归档过了",
    "已停止",                    # 用户主动停的
    "unauthorized",
    "invalid_grant",
    "磁力链接无效",
)


def is_fatal(error: str) -> bool:
    """判断这个错误值不值得再试一次。"""
    text = (error or "").lower()
    return any(p.lower() in text for p in FATAL_PATTERNS)


@dataclass
class RetryPolicy:
    """retries=2 表示最多再试 2 次（总共 3 次机会）。"""

    retries: int = 2
    base_delay: float = 3.0
    max_delay: float = 60.0
    jitter: float = 0.25          # ±25% 抖动，避免多任务同时重试撞在一起
    attempts: int = 0
    history: list[str] = field(default_factory=list)

    def can_retry(self, error: str = "") -> bool:
        if self.attempts >= self.retries:
            return False
        return not is_fatal(error)

    def delay(self, attempt: int | None = None) -> float:
        """第 n 次重试前等多久：base * 2^(n-1)，上限 max_delay，再加抖动。"""
        n = self.attempts if attempt is None else attempt
        raw = min(self.base_delay * (2 ** max(0, n)), self.max_delay)
        spread = raw * self.jitter
        return max(0.0, raw + random.uniform(-spread, spread))

    def record(self, error: str) -> None:
        self.attempts += 1
        self.history.append((error or "")[:200])

    def reset(self) -> None:
        self.attempts = 0
        self.history.clear()

    def summary(self) -> str:
        if not self.history:
            return ""
        return f"已重试 {self.attempts} 次"


def backoff_sleep(policy: RetryPolicy, cancel_check=None, sleep=None) -> bool:
    """睡到该重试的时间点；期间定期检查是否被取消。

    返回 True 表示可以继续重试，False 表示被取消了。
    sleep 参数用于测试注入。
    """
    wait = policy.delay()
    do_sleep = sleep or time.sleep
    step = 0.25
    elapsed = 0.0
    while elapsed < wait:
        if cancel_check and cancel_check():
            return False
        do_sleep(min(step, wait - elapsed))
        elapsed += step
    return not (cancel_check and cancel_check())


async def run_with_retry(
    factory,
    policy: RetryPolicy,
    *,
    on_retry=None,
    cancel_check=None,
    sleep=None,
):
    """反复调用 factory() 直到成功、次数用完、或遇到决定性错误。

    factory 是 async 无参函数（每次重试都重新执行一遍）。
    on_retry(attempt, error, wait) 用于向用户回报「正在重试」。
    bot 与 web 共用这一个实现，避免两边重试行为不一致。
    """
    while True:
        try:
            return await factory()
        except Exception as exc:      # noqa: BLE001 由 is_fatal 决定去留
            if not policy.can_retry(str(exc)):
                raise
            policy.record(str(exc))
            # record 已经把 attempts 加过 1，取延迟要减回去，否则第一次重试就等 2×base
            wait = policy.delay(policy.attempts - 1)
            if on_retry:
                await on_retry(policy.attempts, str(exc), wait)
            if not await asyncio_sleep(policy, cancel_check, sleep):
                raise RuntimeError("已停止") from exc


async def asyncio_sleep(policy: RetryPolicy, cancel_check=None, sleep=None) -> bool:
    """异步版退避等待，期间可被取消。"""
    import asyncio

    wait = policy.delay(max(0, policy.attempts - 1))
    step = 0.25
    elapsed = 0.0
    while elapsed < wait:
        if cancel_check and cancel_check():
            return False
        await asyncio.sleep(min(step, wait - elapsed))
        elapsed += step
    return not (cancel_check and cancel_check())
