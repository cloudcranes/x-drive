"""fxtwitter 兜底解析器的测试：全部离线，直接喂真实响应片段。"""
import asyncio

import httpx
import pytest

from app import fallback

# 取自真实推文 2111712233826947214（syndication 返回 TweetTombstone，fxtwitter 仍能取到视频）
VIDEO_PAYLOAD = {
    "code": 200,
    "message": "OK",
    "tweet": {
        "id": "2111712233826947214",
        "text": "wataa\n07年的可爱少萝妹妹",
        "author": {"id": "1700000000000000000", "name": "妹妹S", "screen_name": "Ashley69311255",
                   "avatar_url": "https://pbs.twimg.com/a.jpg"},
        "media": {
            "photos": [],
            "videos": [
                {"url": "https://video.twimg.com/amplify_video/1/vid/720x1280/a.mp4?tag=14",
                 "thumbnail_url": "https://pbs.twimg.com/thumb.jpg", "duration": 16.788}
            ],
        },
    },
}

PHOTO_PAYLOAD = {
    "code": 200,
    "tweet": {
        "id": "1",
        "text": "three pics",
        "author": {"id": "42", "name": "Volt", "screen_name": "volt"},
        "media": {"photos": [{"url": f"https://pbs.twimg.com/media/{i}.jpg"} for i in range(3)]},
    },
}


def test_to_parse_shape_video():
    data = fallback.to_parse_shape(VIDEO_PAYLOAD)
    assert data["author"] == {"uid": "1700000000000000000", "name": "妹妹S",
                              "avatar": "https://pbs.twimg.com/a.jpg"}
    assert data["video_url"].startswith("https://video.twimg.com/")
    assert data["cover_url"] == "https://pbs.twimg.com/thumb.jpg"
    assert data["images"] == []


def test_to_parse_shape_photos():
    data = fallback.to_parse_shape(PHOTO_PAYLOAD)
    assert len(data["images"]) == 3
    assert data["video_url"] == ""
    assert data["cover_url"] == "https://pbs.twimg.com/media/0.jpg"


def test_to_parse_shape_no_media():
    assert fallback.to_parse_shape({"tweet": {"media": {"photos": [], "videos": []}}}) is None
    assert fallback.to_parse_shape({}) is None


def test_fetch_ok(monkeypatch):
    class FakeResp:
        status_code = 200
        def json(self): return VIDEO_PAYLOAD

    class FakeClient:
        async def get(self, url, headers=None, timeout=None):
            assert url.endswith("/i/status/2111712233826947214")
            return FakeResp()

    data = asyncio.run(fallback.fetch(FakeClient(), "2111712233826947214"))
    assert data and data["video_url"]


def test_fetch_http_error(monkeypatch):
    class FakeResp:
        status_code = 404
        def json(self): return {}

    class FakeClient:
        async def get(self, *a, **k): return FakeResp()

    assert asyncio.run(fallback.fetch(FakeClient(), "1")) is None


def test_fetch_network_error():
    class Boom:
        async def get(self, *a, **k): raise httpx.ConnectError("no net")

    assert asyncio.run(fallback.fetch(Boom(), "1")) is None


def test_fetch_empty_id():
    assert asyncio.run(fallback.fetch(object(), "")) is None


def test_multi_video_all_returned():
    payload = {"tweet": {"media": {
        "photos": [{"url": "https://pbs.twimg.com/p.jpg"}],
        "videos": [{"url": "https://video.twimg.com/1.mp4", "thumbnail_url": "https://pbs.twimg.com/t1.jpg"},
                   {"url": "https://video.twimg.com/2.mp4", "thumbnail_url": "https://pbs.twimg.com/t2.jpg"}],
    }}}
    data = fallback.to_parse_shape(payload)
    assert data["videos"] == ["https://video.twimg.com/1.mp4", "https://video.twimg.com/2.mp4"]
    assert data["video_url"] == "https://video.twimg.com/1.mp4"
    assert len(data["images"]) == 1, "有视频时图片也要带上"
