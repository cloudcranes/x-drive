"""备用解析器：syndication 返回 TweetTombstone / 无媒体时，用 fxtwitter 兜底。

syndication 对「敏感内容」推文只返回 `{"__typename":"TweetTombstone"}`，
parse-video-py 这时会抛「该推文中没有找到视频或图片」。fxtwitter 仍能拿到媒体。
"""
from __future__ import annotations

import logging

import httpx

from . import core

log = logging.getLogger("x-drive.fallback")

FX_API = "https://api.fxtwitter.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://x.com/",
}


def to_parse_shape(payload: dict) -> dict | None:
    """把 fxtwitter 的 tweet 对象转成 parse-video-py 的 data 结构；无媒体返回 None。"""
    tweet = (payload or {}).get("tweet") or {}
    media = tweet.get("media") or {}
    photos = media.get("photos") or []
    videos = media.get("videos") or []
    if not photos and not videos:
        return None

    images = [{"url": p.get("url") or "", "live_photo_url": ""} for p in photos if p.get("url")]
    video_url = ""
    cover = ""
    if videos:
        # 同一条推文可能有多个视频，取第一个做 video_url，其余后续按 images 之外的顺序补
        video_url = videos[0].get("url") or ""
        cover = videos[0].get("thumbnail_url") or ""
    elif images:
        cover = images[0]["url"]

    author = tweet.get("author") or {}
    return {
        "video_url": video_url,
        "cover_url": cover,
        "title": (tweet.get("text") or "").strip(),
        "music_url": "",
        "images": images,
        "video_cover": cover if video_url else "",
        "author": {
            "uid": str(author.get("id") or ""),
            "name": author.get("name") or author.get("screen_name") or "",
            "avatar": author.get("avatar_url") or "",
        },
    }


async def fetch(client: httpx.AsyncClient, tweet_id: str) -> dict | None:
    """按推文 id 取媒体；失败或无媒体返回 None。"""
    if not tweet_id:
        return None
    try:
        resp = await client.get(f"{FX_API}/i/status/{tweet_id}", headers=HEADERS, timeout=30.0)
        if resp.status_code != 200:
            log.warning("fxtwitter %s 返回 %s", tweet_id, resp.status_code)
            return None
        payload = resp.json()
    except Exception as exc:  # 兜底失败不该盖掉原始错误
        log.warning("fxtwitter %s 请求失败：%s", tweet_id, exc)
        return None

    if (payload or {}).get("code") not in (200, "200", None):
        log.warning("fxtwitter %s code=%s", tweet_id, payload.get("code"))
        return None
    data = to_parse_shape(payload)
    if data:
        log.info("fxtwitter 命中 %s：%d 图 %d 视频", tweet_id,
                 len(data["images"]), 1 if data["video_url"] else 0)
    return data


def tweet_id(url: str) -> str | None:
    return core.tweet_id_from_url(url)
