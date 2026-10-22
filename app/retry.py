"""失败重试策略：按失败性质分级 + 指数退避 + 次数上限 + 可中断。

三类分法看的是「再试一次能不能变好」：
  transient 网络抖动、5xx、读超时 —— 退避后重试，通常第二次就成
  dead      死种、无进展 —— 重试会拿回同一条任务，只给一次长退避机会
  quota     Drive 配额、单日上限 —— 今天怎么试都没用，留待办等明天
  config    挂载错、目录 id 失效、未授权 —— 重试一万次也一样，立刻报

单独成模块是为了能纯逻辑测试——真实重试要等几分钟，不适合塞进集成测试。
bot 与 web 两条链路共用同一套判断，避免两边行为不一致。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

# 配额类：今天没戏，明天再说（不是错误，是等待）
QUOTA_PATTERNS = (
    "uploadlimitExceeded".lower(),
    "quota",
    "配额",
    "超过 daily_upload_gb",
    "userratelimitexceeded",
    "drive 上传配额",
    "上传量已达",              # 今日上传量已达 20GB 上限
    "明日再试",               # 配额报错的收尾用语
    "每日上传",
)
# 死种类：再试也不会自己好，重试只会拿回同一条任务
DEAD_PATTERNS = (
    "没有任何进展",
    "可能是死种",
    "任务超时",
    "更换资源",
)
# 配置类：人没配对，重试无意义
CONFIG_PATTERNS = (
    "xunlei_parent_id",
    "device_space_not_active",
    "不在 tmp_root",
    "unauthorized",
    "invalid_grant",
    "磁力链接无效",
    "看不懂这个",
)

TRANSIENT, DEAD, QUOTA, CONFIG = "transient", "dead", "quota", "config"

def classify(error: str) -> str:
    """把失败文本归到四类之一；认不出的一律当 transient（宁可多试一次）。"""
    text = (error or "").lower()
    for name, patterns in (("quota", QUOTA_PATTERNS), ("dead", DEAD_PATTERNS),
                           ("config", CONFIG_PATTERNS)):
        if any(p.lower() in text for p in patterns):
            return name
    return TRANSIENT

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
    "正在下载中",              # 同一磁力被并发提交，重试也没用
    "already registered",
    "已停止",                    # 用户主动停的
    "unauthorized",
    "invalid_grant",
    "磁力链接无效",
    "xunlei_parent_id",         # 迅雷下载目录 id 没配/失效，重试也不会自己好
    "device_space_not_active",
    "迅雷投递失败",             # 投递阶段就是被面板拒绝，不是网络抖动
    "没有任何进展",             # 死种：重试只会拿回同一条任务（infohash 去重），白等
    "不在 TMP_ROOT",            # 挂载配错，重试一万次也一样
)


def is_fatal(error: str) -> bool:
    """判断这个错误值不值得再试一次（配置/配额/死种都算）。"""
    text = (error or "").lower()
    if any(p.lower() in text for p in FATAL_PATTERNS):
        return True
    return classify(error) in (CONFIG, QUOTA)


@dataclass
class RetryPolicy:
    """retries=2 表示最多再试 2 次（总共 3 次机会）。"""

    retries: int = 2
    base_delay: float = 3.0
    max_delay: float = 60.0
    jitter: float = 0.25          # ±25% 抖动，避免多任务同时重试撞在一起
    attempts: int = 0
    history: list[str] = field(default_factory=list)

    def limit_for(self, error: str = "") -> int:
        """这一次失败最多还能试几次。"""
        kind = classify(error)
        if kind in (CONFIG, QUOTA):
            return 0
        if kind == DEAD:
            return min(self.retries, 1)          # 死种只再给一次，别拿配额赌
        return self.retries

    def can_retry(self, error: str = "") -> bool:
        if self.attempts >= self.limit_for(error):
            return False
        return not is_fatal(error)

    def delay(self, attempt: int | None = None, error: str = "") -> float:
        """第 n 次重试前等多久：base * 2^(n-1)，上限 max_delay，再加抖动。

        死种类给 base×6 的长退避：等 peer 回来，比连着撞十次有用。
        """
        n = self.attempts if attempt is None else attempt
        factor = 6 if classify(error) == DEAD else 1
        raw = min(self.base_delay * factor * (2 ** max(0, n)), self.max_delay)
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


def backoff_sleep(policy: RetryPolicy, cancel_check=None, sleep=None, error: str = "") -> bool:
    """睡到该重试的时间点；期间定期检查是否被取消。

    返回 True 表示可以继续重试，False 表示被取消了。
    sleep 参数用于测试注入。
    """
    wait = policy.delay(error=error)
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
