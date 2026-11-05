"""观看闭环：进度落库、90% 自动已看、收藏与 collect 回填。"""
import pytest

from app import core, meta, store, web


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    monkeypatch.setattr(web, "STORE", store.Store(db))
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {
        "TCHB-029": {"code": "TCHB-029", "at": "2025-01-02 03:04:05",
                     "files": ["TCHB-029.mp4"], "size": 10, "path": "x"},
        "TCHR-035": {"code": "TCHR-035", "at": "2025-01-03 03:04:05",
                     "files": ["TCHR-035.mp4"], "size": 10, "path": "y"},
    })
    monkeypatch.setattr(meta, "cached", lambda *a, **k: None)
    return web


def test_progress_reaches_ninety_percent_and_marks_done(env):
    first = env.watch_update("TCHB-029", pos=10, dur=100)
    assert first["pct"] == 10 and first["done"] is False

    done = env.watch_update("TCHB-029", pos=90, dur=100)
    assert done["done"] is True
    assert done["pct"] == 100
    assert done["pos"] == 100


def test_favorite_keeps_progress(env):
    env.watch_update("TCHB-029", pos=30, dur=100)
    favorited = env.watch_update("TCHB-029", fav=True)
    assert favorited["fav"] is True
    assert favorited["pos"] == 30
    assert favorited["done"] is False


def test_mark_unwatched_resets_position(env):
    env.watch_update("TCHB-029", pos=100, dur=100)
    reset = env.watch_update("TCHB-029", done=False)
    assert reset["done"] is False
    assert reset["pos"] == 0
    assert reset["pct"] == 0


def test_collect_attaches_watch_to_task(env):
    env.watch_update("TCHB-029", pos=42, dur=100)
    data = env.collect()
    task = next(t for t in data["tasks"] if t["code"] == "TCHB-029")
    assert task["watch"]["pct"] == 42
    assert task["watch"]["pos"] == 42
    other = next(t for t in data["tasks"] if t["code"] == "TCHR-035")
    assert other["watch"] == {}


def test_watch_ignores_unknown_and_bad_records(env):
    assert env.watch_update("", pos=1, dur=2) == {}
    env.STORE.state_put({"watch": {"BAD": "nope"}})
    assert "BAD" not in env.watch_view()
