"""运行日志读取：网页只拿尾部，异常栈要完整，过滤不能把上下文切断。"""
import logging

from app import logbuf


def _write(tmp_path, text):
    path = tmp_path / "x-drive.log"
    path.write_text(text, encoding="utf-8")
    return path


def test_tail_keeps_traceback_with_its_record(tmp_path, monkeypatch):
    path = _write(tmp_path, (
        "2026-10-31 04:00:00,000 INFO x-drive.web 启动\n"
        "2026-10-31 04:00:01,000 ERROR x-drive.job 云添加失败\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n'
        "RuntimeError: boom\n"
        "2026-10-31 04:00:02,000 INFO x-drive.web 收尾\n"
    ))
    monkeypatch.setattr(logbuf, "LOG_PATH", str(path))
    out = logbuf.tail(limit=10)
    assert out["ok"] is True and out["size"] == path.stat().st_size
    assert [i["level"] for i in out["items"]] == ["INFO", "ERROR", "INFO"]
    assert "RuntimeError: boom" in out["items"][1]["text"]
    assert out["items"][1]["name"] == "x-drive.job"


def test_tail_filters_level_and_query(tmp_path, monkeypatch):
    path = _write(tmp_path, (
        "2026-10-31 04:00:00,000 INFO x-drive.web 下载开始\n"
        "2026-10-31 04:00:01,000 WARNING x-drive.web 磁盘紧张\n"
        "2026-10-31 04:00:02,000 ERROR x-drive.job 下载失败\n"
    ))
    monkeypatch.setattr(logbuf, "LOG_PATH", str(path))
    errors = logbuf.tail(limit=10, level="ERROR")["items"]
    assert [i["text"] for i in errors] == ["下载失败"]
    hits = logbuf.tail(limit=10, q="下载")["items"]
    assert len(hits) == 2


def test_tail_missing_file_is_empty_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(logbuf, "LOG_PATH", str(tmp_path / "nope.log"))
    out = logbuf.tail()
    assert out == {"ok": True, "items": [], "size": 0, "path": str(tmp_path / "nope.log")}


def test_install_adds_one_file_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(logbuf, "_installed", False)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        first = logbuf.install(str(tmp_path / "a.log"))
        second = logbuf.install(str(tmp_path / "b.log"))
        assert first == second
        added = [h for h in root.handlers if isinstance(h, logging.FileHandler)
                 and h not in before]
        assert len(added) == 1
        assert added[0].baseFilename.endswith("a.log")
    finally:
        for handler in [h for h in root.handlers if h not in before]:
            root.removeHandler(handler)
            handler.close()
        logbuf._installed = False


def test_install_creates_parent_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(logbuf, "_installed", False)
    root = logging.getLogger()
    before = list(root.handlers)
    target = tmp_path / "deep" / "dir" / "x.log"
    try:
        logbuf.install(str(target))
        assert target.parent.is_dir()
    finally:
        for handler in [h for h in root.handlers if h not in before]:
            root.removeHandler(handler)
            handler.close()
        logbuf._installed = False
