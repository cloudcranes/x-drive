"""图片本地缓存：命名、命中、LRU 修剪、坏文件容错、老布局清理。

图床握手不稳，缓存是必需品而不是优化 —— 这些测试把「一张图成功一次之后就一直可用」
这条承诺钉住，也钉住「缓存出问题不能让看图挂掉」。
"""
from __future__ import annotations

import os
import time

import pytest

from app import imgcache


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COVER_CACHE", str(tmp_path / "covers"))
    monkeypatch.setattr(imgcache, "_legacy_checked", False)
    yield tmp_path / "covers"
    os.environ.pop("COVER_CACHE", None)


URL = "https://www.javbus.com/pics/thumb/cmio.jpg"


# ─────────────────────────────────────────── 存与取

def test_put_then_get(cache_dir):
    path = imgcache.put(URL, b"\xff\xd8jpeg", "image/jpeg")
    assert path and path.exists()
    blob, ctype, got = imgcache.get(URL)
    assert blob == b"\xff\xd8jpeg" and ctype == "image/jpeg" and got == path


def test_filename_is_readable(cache_dir):
    """文件名保留原图名：出问题时能 ls 出来看是哪张，而不是一堆 .bin。"""
    path = imgcache.put(URL, b"x", "image/jpeg")
    assert path.name.endswith("-cmio.jpg")
    assert len(path.name.split("-")[0]) == 16, "哈希前缀固定 16 位"


def test_different_urls_dont_collide(cache_dir):
    a = imgcache.put("https://a/x/same.jpg", b"aaa", "image/jpeg")
    b = imgcache.put("https://b/x/same.jpg", b"bbb", "image/jpeg")
    assert a != b
    assert imgcache.get("https://a/x/same.jpg")[0] == b"aaa"
    assert imgcache.get("https://b/x/same.jpg")[0] == b"bbb"


def test_miss_returns_none(cache_dir):
    assert imgcache.get("https://nope/1.jpg") is None
    assert imgcache.find("https://nope/1.jpg") is None


def test_extension_from_ctype_and_fallback(cache_dir):
    assert imgcache.ext_for("image/png") == ".png"
    assert imgcache.ext_for("image/webp; charset=binary") == ".webp"
    assert imgcache.ext_for("", "https://x/a.PNG") == ".png"
    assert imgcache.ext_for("application/octet-stream", "https://x/a") == ".jpg"


def test_type_for_roundtrip(cache_dir):
    for ctype in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        path = imgcache.put(URL + ctype, b"x", ctype)
        assert imgcache.type_for(path) == ctype


# ─────────────────────────────────────────── 容错

def test_empty_file_counts_as_miss(cache_dir):
    """空文件 = 上次没写完（进程被杀），当没命中重取。"""
    path = imgcache.put(URL, b"x", "image/jpeg")
    path.write_bytes(b"")
    assert imgcache.get(URL) is None


def test_broken_dir_does_not_raise(cache_dir, monkeypatch):
    """缓存目录不可读也不该抛：看图比缓存重要。"""
    monkeypatch.setattr(imgcache, "root", lambda: cache_dir / "nope" / "deep")
    assert imgcache.get(URL) is None
    assert imgcache.stats()["files"] == 0


def test_put_empty_data_is_noop(cache_dir):
    assert imgcache.put(URL, b"", "image/jpeg") is None
    assert imgcache.stats()["files"] == 0


# ─────────────────────────────────────────── 容量

def test_trim_respects_limit(cache_dir):
    for i in range(5):
        imgcache.put(f"https://x/{i}.jpg", b"z" * 100, "image/jpeg")
    assert imgcache.stats()["files"] == 5
    imgcache.trim(250)                       # 只留得下 2 张
    assert imgcache.stats()["bytes"] <= 250


def test_trim_drops_least_recently_used(cache_dir):
    """按「最近用过」淘汰，不是「最早写入」：常看的那张要留下。"""
    old = imgcache.put("https://x/old.jpg", b"o" * 100, "image/jpeg")
    keep = imgcache.put("https://x/keep.jpg", b"k" * 100, "image/jpeg")
    # keep 刚被读过 → mtime 更新
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    imgcache.get("https://x/keep.jpg")
    imgcache.trim(150)
    assert not old.exists() and keep.exists(), "读过的那张应该活下来"


def test_trim_zero_means_unlimited(cache_dir):
    imgcache.put(URL, b"z" * 10, "image/jpeg")
    assert imgcache.trim(0) == 0
    assert imgcache.stats()["files"] == 1


def test_clear_removes_everything(cache_dir):
    for i in range(3):
        imgcache.put(f"https://x/{i}.jpg", b"z" * 10, "image/jpeg")
    result = imgcache.clear()
    assert result["removed"] == 3 and result["bytes"] == 30
    assert imgcache.stats()["files"] == 0


# ─────────────────────────────────────────── 老布局

def test_legacy_layout_is_purged(cache_dir):
    """老布局只存 URL 哈希、没存 URL，无法迁移：留着只占地方，删掉。"""
    legacy = cache_dir / "ab"
    legacy.mkdir(parents=True)
    (legacy / "abcdef.bin").write_bytes(b"x" * 10)
    (legacy / "abcdef.type").write_text("image/jpeg", encoding="utf-8")
    imgcache.put(URL, b"new", "image/jpeg")
    assert not (legacy / "abcdef.bin").exists()
    assert imgcache.stats()["files"] == 1, "新的那张要留着"


def test_sweep_tmp(cache_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)
    stale = cache_dir / "x.jpg.tmp"
    stale.write_bytes(b"x")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    assert imgcache.sweep_tmp() == 1
    assert not stale.exists()


# ─────────────────────────────────────────── 目录解析

def test_root_follows_setting(monkeypatch, cache_dir):
    """网页设置 IMG_CACHE_DIR 优先于环境变量。"""
    from app import settings as settings_mod
    from app import store
    inst = settings_mod.Settings(store.Store(str(cache_dir.parent / "j.db")))
    inst.set("IMG_CACHE_DIR", "/tmp/from-setting")
    settings_mod.bind(inst)
    try:
        assert str(imgcache.root()) == "/tmp/from-setting"
    finally:
        settings_mod.bind(None)
