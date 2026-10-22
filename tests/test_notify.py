"""通知：去重、不刷屏、失败不影响主流程、未配置时静默。"""
import pytest

from app import notify


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    notify.reset_dedupe()
    monkeypatch.setattr(notify, "TOKEN", "T")
    monkeypatch.setattr(notify, "CHAT_ID", "1")
    yield
    notify.reset_dedupe()


def test_not_configured_is_silent(monkeypatch):
    monkeypatch.setattr(notify, "TOKEN", "")
    sent = []
    monkeypatch.setattr(notify, "_post", lambda text, timeout=10.0: sent.append(text) or True)
    assert notify.job_done("SSIS-1") is False
    assert sent == []


def test_done_is_deduped_by_code(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_post", lambda text, timeout=10.0: sent.append(text) or True)
    assert notify.job_done("SSIS-1") is True
    assert notify.job_done("SSIS-1") is False, "同一番号短时间内不重复提醒"
    assert notify.job_done("SSIS-2") is True
    assert len(sent) == 2


def test_dedupe_window_expires(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_post", lambda text, timeout=10.0: sent.append(text) or True)
    assert notify.job_done("SSIS-3") is True
    # 窗口过后允许再发
    assert notify.notify("done", "SSIS-3", "again",
                         now=notify._SENT["done:SSIS-3"] + notify.DEDUPE_WINDOW + 1) is True


def test_send_failure_returns_false_and_does_not_raise(monkeypatch):
    monkeypatch.setattr(notify, "_post", lambda text, timeout=10.0: False)
    assert notify.job_failed("SSIS-4", "炸了") is False
    assert "failed:SSIS-4" not in notify._SENT, "没发出去就不能记成已发"


def test_html_is_escaped(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_post", lambda text, timeout=10.0: sent.append(text) or True)
    notify.job_failed("A-1", "<script>x</script>")
    assert "<script>" not in sent[0] and "&lt;script&gt;" in sent[0]


def test_daily_summary_once_per_day(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_post", lambda text, timeout=10.0: sent.append(text) or True)
    assert notify.daily_summary("今天 3 部", "2026-01-01") is True
    assert notify.daily_summary("今天 3 部", "2026-01-01") is False
    assert notify.daily_summary("今天 4 部", "2026-01-02") is True
