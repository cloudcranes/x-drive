import os
from datetime import datetime

from app.core import (
    media_filename,
    domain_allowed,
    extract_url,
    guess_ext,
    media_entries,
    orig_image_url,
    receipt,
    save_state,
    load_state,
    slug,
    target_dir,
    tweet_id_from_url,
)


def test_extract_url_basic():
    assert extract_url("看这个 https://x.com/a/status/123?s=20 好看") == "https://x.com/a/status/123?s=20"


def test_extract_url_strips_trailing_punctuation():
    assert extract_url("链接：https://x.com/a/status/123。") == "https://x.com/a/status/123"
    assert extract_url("(https://t.co/abc)") == "https://t.co/abc"


def test_extract_url_none():
    assert extract_url("没有链接") is None
    assert extract_url("") is None


def test_domain_allowed():
    allowed = ["x.com", "twitter.com", "t.co"]
    assert domain_allowed("https://x.com/a/status/1", allowed)
    assert domain_allowed("https://mobile.twitter.com/a/status/1", allowed)
    assert domain_allowed("https://t.co/abc", allowed)
    assert not domain_allowed("https://evil-x.com/a/status/1", allowed)
    assert not domain_allowed("https://douyin.com/x", allowed)


def test_tweet_id_from_url():
    assert tweet_id_from_url("https://x.com/u/status/1849000000000000000?s=20") == "1849000000000000000"
    assert tweet_id_from_url("https://twitter.com/u/statuses/12345") == "12345"
    assert tweet_id_from_url("https://x.com/u/likes") is None


def test_slug():
    assert slug("Elon Musk") == "Elon_Musk"
    assert slug("小王 / 测试") == "小王_测试"
    assert slug("🎉🎉") == ""
    assert len(slug("a" * 100)) == 40


def test_target_dir():
    now = datetime(2026, 10, 19)
    assert target_dir("gdrive", "X-Drive", "123", "42", "Elon Musk", now) == "gdrive:X-Drive/Elon_Musk/2026-10/123"
    assert target_dir("gdrive", "/X-Drive/", "9", "42", "", now) == "gdrive:X-Drive/42/2026-10/9"
    assert target_dir("gdrive", "", "9", "", "", now) == "gdrive:unknown/2026-10/9"


def test_media_filename():
    now = datetime(2026, 10, 19)
    assert media_filename("Elon Musk", now, 2, ".jpg") == "Elon_Musk_2026-10-19_02.jpg"
    assert media_filename("小王", now, 1, ".mp4") == "小王_2026-10-19_01.mp4"
    assert media_filename("🎉", now, 1, ".jpg") == "unknown_2026-10-19_01.jpg"


def test_guess_ext():
    assert guess_ext("https://pbs.twimg.com/media/x.jpg?name=orig", "image") == ".jpg"
    assert guess_ext("https://video.twimg.com/x/720x1280/abc.mp4", "video") == ".mp4"
    assert guess_ext("https://video.twimg.com/hls/index.m3u8", "video") == ".m3u8"
    assert guess_ext("https://x.com/none", "image") == ".jpg"
    assert guess_ext("https://x.com/none", "video") == ".mp4"


def test_orig_image_url():
    got = orig_image_url("https://pbs.twimg.com/media/ABC.jpg?format=jpg&name=small")
    assert "name=orig" in got and "name=small" not in got
    assert orig_image_url("https://video.twimg.com/a.mp4") == "https://video.twimg.com/a.mp4"


def test_media_entries():
    data = {
        "video_url": "https://video.twimg.com/1.mp4",
        "images": [{"url": "https://pbs.twimg.com/media/1.jpg?name=small", "live_photo_url": ""}],
    }
    entries = media_entries(data)
    assert entries[0] == ("video", "https://video.twimg.com/1.mp4")
    assert entries[1][0] == "image" and "name=orig" in entries[1][1]
    assert media_entries({"video_url": "", "images": []}) == []


def test_state_roundtrip(tmp_path):
    path = str(tmp_path / "state.json")
    assert load_state(path) == {}
    save_state(path, {"123": {"link": "http://d"}})
    assert load_state(path)["123"]["link"] == "http://d"


def test_receipt():
    text = receipt("https://drive.google.com/x", ["01.jpg", "02.jpg", "03.mp4"], [], "标题")
    assert "2 图" in text and "1 视频" in text and "drive.google.com" in text
    assert "没有文件入库" in receipt(None, [], ["下载失败"])
    long_title = "字" * 200
    assert len(receipt(None, ["a.jpg"], [], long_title)) < 300
