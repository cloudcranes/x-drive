"""重试策略测试：纯逻辑，不等待真实时间。"""
import app.retry as retry


def test_fatal_errors_are_not_retried():
    for msg in ("uploadLimitExceeded", "Drive 上传配额已用完",
                "下载完成但没有找到视频文件", "已停止", "超过 DAILY_UPLOAD_GB 限制"):
        assert retry.is_fatal(msg), msg
        pol = retry.RetryPolicy(retries=3)
        assert not pol.can_retry(msg)


def test_transient_errors_are_retried():
    for msg in ("连接超时", "迅雷任务失败：timeout", "rclone 上传失败：connection reset",
                "HTTP 503"):
        assert not retry.is_fatal(msg), msg
        pol = retry.RetryPolicy(retries=2)
        assert pol.can_retry(msg)


def test_retry_stops_after_limit():
    pol = retry.RetryPolicy(retries=2)
    assert pol.can_retry("超时")
    pol.record("超时")
    assert pol.can_retry("超时")
    pol.record("超时")
    assert not pol.can_retry("超时"), "超过次数后不能无限重试"
    assert pol.attempts == 2


def test_backoff_grows_exponentially_and_is_capped():
    pol = retry.RetryPolicy(base_delay=3, max_delay=20, jitter=0)
    assert pol.delay(0) == 3
    assert pol.delay(1) == 6
    assert pol.delay(2) == 12
    assert pol.delay(5) == 20, "必须封顶，不能翻到几分钟"


def test_jitter_stays_within_bounds():
    pol = retry.RetryPolicy(base_delay=10, max_delay=10, jitter=0.25)
    values = {round(pol.delay(0), 3) for _ in range(40)}
    assert all(7.5 <= v <= 12.5 for v in values), values
    assert len(values) > 1, "抖动应带来随机性"


def test_cancel_interrupts_wait():
    pol = retry.RetryPolicy(retries=3, base_delay=5, jitter=0)
    slept = []

    def fake_sleep(sec):
        slept.append(sec)

    ok = retry.backoff_sleep(pol, cancel_check=lambda: True, sleep=fake_sleep)
    assert ok is False
    assert sum(slept) < 5, "被取消时应立刻返回，不必等满"


def test_sleep_completes_when_not_cancelled():
    pol = retry.RetryPolicy(base_delay=1, jitter=0)
    slept = []
    ok = retry.backoff_sleep(pol, cancel_check=lambda: False, sleep=lambda s: slept.append(s))
    assert ok is True
    assert sum(slept) >= 1


def test_summary_and_reset():
    pol = retry.RetryPolicy(retries=3)
    assert pol.summary() == ""
    pol.record("超时")
    assert "已重试 1 次" in pol.summary()
    pol.reset()
    assert pol.attempts == 0 and pol.summary() == ""


def test_run_with_retry_succeeds_after_failures():
    import asyncio
    attempts = {"n": 0}

    async def factory():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("连接超时")
        return "ok"

    pol = retry.RetryPolicy(retries=3, base_delay=0, jitter=0)
    result = asyncio.run(retry.run_with_retry(factory, pol))
    assert result == "ok" and attempts["n"] == 3
    assert pol.attempts == 2


def test_run_with_retry_raises_fatal_immediately():
    import asyncio
    attempts = {"n": 0}

    async def factory():
        attempts["n"] += 1
        raise RuntimeError("uploadLimitExceeded")

    pol = retry.RetryPolicy(retries=5, base_delay=0, jitter=0)
    try:
        asyncio.run(retry.run_with_retry(factory, pol))
        raise AssertionError("应该抛出")
    except RuntimeError as exc:
        assert "uploadLimitExceeded" in str(exc)
    assert attempts["n"] == 1, "决定性错误只尝试一次"


def test_run_with_retry_reports_each_attempt():
    import asyncio
    seen = []

    async def factory():
        raise RuntimeError("timeout")

    async def on_retry(attempt, error, wait):
        seen.append((attempt, error, round(wait, 1)))

    pol = retry.RetryPolicy(retries=2, base_delay=1, jitter=0)
    try:
        asyncio.run(retry.run_with_retry(factory, pol, on_retry=on_retry))
    except RuntimeError:
        pass
    assert [s[0] for s in seen] == [1, 2], seen
    # 退避应逐次变长：base=1 → 第一次 1s，第二次 2s
    assert [s[2] for s in seen] == [1.0, 2.0], seen


def test_duplicate_torrent_is_not_retried():
    """同一磁力正在下载属于确定性冲突，重试只是白等。"""
    for msg in ("这个种子正在下载中，等它跑完再提交",
                "迅雷投递失败：parent_folder_id invalid"):
        assert retry.is_fatal(msg), msg

def test_dead_torrent_and_bad_mount_are_fatal():
    """死种重试只会拿回同一条任务；挂载配错重试一万次也一样。"""
    assert retry.is_fatal("900 秒没有任何进展（停在 12.3%），可能是死种")
    assert retry.is_fatal("迅雷把文件下到了 /mnt/x/A，不在 TMP_ROOT=/downloads 下")

# ---------------------------------------------------------------- 分级

def test_classify_four_kinds():
    assert retry.classify("连接超时") == retry.TRANSIENT
    assert retry.classify("HTTP 502 Bad Gateway") == retry.TRANSIENT
    assert retry.classify("900 秒没有任何进展（停在 12.3%），可能是死种") == retry.DEAD
    assert retry.classify("迅雷任务失败：任务超时,已重试10次,请更换资源") == retry.DEAD
    assert retry.classify("今日上传量已达 20GB 上限，文件留在 /downloads 可明天重试") == retry.QUOTA
    assert retry.classify("Drive 上传配额已用完，明日再试") == retry.QUOTA
    assert retry.classify("没有配置 XUNLEI_PARENT_ID") == retry.CONFIG
    assert retry.classify("迅雷把文件下到了 /mnt/x，不在 TMP_ROOT=/downloads 下") == retry.CONFIG

def test_limits_differ_by_kind():
    pol = retry.RetryPolicy(retries=3)
    assert pol.limit_for("连接超时") == 3, "网络类给满次数"
    assert pol.limit_for("可能是死种") == 1, "死种只再给一次"
    assert pol.limit_for("今日上传量已达上限") == 0, "配额今天没戏"
    assert pol.limit_for("没有配置 XUNLEI_PARENT_ID") == 0, "配置错重试无意义"

def test_dead_gets_longer_backoff():
    pol = retry.RetryPolicy(retries=3, base_delay=10.0, max_delay=600.0, jitter=0)
    assert pol.delay(0, error="连接超时") == 10.0
    assert pol.delay(0, error="可能是死种") == 60.0, "死种等久一点，让 peer 回来"

def test_can_retry_respects_kind():
    pol = retry.RetryPolicy(retries=2)
    pol.record("可能是死种")
    assert pol.can_retry("可能是死种") is False, "死种已用掉唯一一次机会"
    pol2 = retry.RetryPolicy(retries=2)
    pol2.record("连接超时")
    assert pol2.can_retry("连接超时") is True
