"""迅雷官方直链（api-pan.xunlei.com）：照 greasyfork 脚本的接口契约实现。

脚本「迅雷云盘」拿直链走两个接口：
  GET /drive/v1/files?parent_id=…        列目录
  GET /drive/v1/files/{id}               单文件详情 → web_content_link + medias[]
鉴权是浏览器 localStorage 里 credentials_{clientid} 的 token_type/access_token
加 clientid 头。直链有有效期，所以不缓存、每次现取。
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from app import xunleipan


def transport(handler):
    return httpx.MockTransport(handler)


def client(handler):
    return httpx.Client(transport=transport(handler), base_url=xunleipan.API)


def _env(monkeypatch, token="abc123", client_id="cid-1", device="dev-1"):
    monkeypatch.setattr(xunleipan, "_setting",
                        lambda key: {"XLPAN_TOKEN": token,
                                     "XLPAN_CLIENT_ID": client_id,
                                     "XLPAN_DEVICE_ID": device}.get(key, ""))


@pytest.fixture(autouse=True)
def _fresh_caches():
    """路径解析缓存是模块级的：不清就会串味，第二个用例命中第一个的缓存。"""
    xunleipan.clear_caches()
    yield
    xunleipan.clear_caches()


# ─────────────────────────────────────────── 鉴权头

def test_headers_carry_token_clientid_device(monkeypatch):
    _env(monkeypatch)
    got = {}

    def handler(request: httpx.Request) -> httpx.Response:
        got.update(dict(request.headers))
        return httpx.Response(200, json={"files": []})

    xunleipan.list_files(client=client(handler))
    assert got["authorization"] == "Bearer abc123", "裸 token 自动补 Bearer"
    assert got["clientid"] == "cid-1"
    assert got["x-device-id"] == "dev-1"
    assert "x-captcha-token" not in got, "没配就不发这个头"


def test_headers_carry_captcha_token_when_set(monkeypatch):
    """风控头：captcha_invalid 是实测踩到的（API 400 error_code 9）。"""
    values = {"XLPAN_TOKEN": "t", "XLPAN_CLIENT_ID": "c",
              "XLPAN_DEVICE_ID": "", "XLPAN_CAPTCHA_TOKEN": "cap-9"}
    monkeypatch.setattr(xunleipan, "_setting", lambda key: values.get(key, ""))
    got = {}

    def handler(request: httpx.Request) -> httpx.Response:
        got.update(dict(request.headers))
        return httpx.Response(200, json={"files": []})

    xunleipan.list_files(client=client(handler))
    assert got["x-captcha-token"] == "cap-9"


def test_captcha_error_says_what_to_fill(monkeypatch):
    """captcha 走不通时的提示：链对不上，让人从浏览器再取一次 captcha。

    （不再让人手填设置项 —— 那是上一版的做法；现在程序自己签发并轮换。）
    """
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "captcha_invalid", "error_code": 9})

    with pytest.raises(xunleipan.PanError) as err:
        xunleipan.list_files(client=client(handler))
    assert "captcha" in str(err.value).lower()


def test_headers_keep_explicit_bearer(monkeypatch):
    _env(monkeypatch, token="Bearer xyz")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer xyz"
        return httpx.Response(200, json={"files": []})

    xunleipan.list_files(client=client(handler))


def test_configured_needs_both_values(monkeypatch):
    _env(monkeypatch, token="", client_id="cid")
    assert xunleipan.configured() is False
    _env(monkeypatch, token="t", client_id="")
    assert xunleipan.configured() is False
    _env(monkeypatch)
    assert xunleipan.configured() is True


def test_device_id_follows_script_truncation(monkeypatch):
    """脚本取 deviceid 点后前 32 字符（localStorage 是 wdi10.<hex>）。"""
    _env(monkeypatch, device="wdi10." + "a" * 64)
    assert xunleipan._device_id() == "a" * 32
    _env(monkeypatch, device="plain-id-12345")
    assert xunleipan._device_id() == "plain-id-12345"


# ─────────────────────────────────────────── 列目录 / 详情

def test_list_files_parses_items(monkeypatch):
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["parent_id"] == "pid1"
        return httpx.Response(200, json={"files": [
            {"id": "f1", "name": "MIMK-290.mp4", "kind": "drive#file",
             "phase": "PHASE_TYPE_COMPLETE", "trashed": False},
            {"id": "d1", "name": "子目录", "kind": "drive#folder", "trashed": False},
        ]})

    items = xunleipan.list_files("pid1", client=client(handler))
    assert [i["name"] for i in items] == ["MIMK-290.mp4", "子目录"]
    assert items[0]["kind"] == "drive#file"


def test_list_files_drops_tombstones_in_active_view(monkeypatch):
    """默认列表会把 trashed=true 的墓碑一起吐回来，必须滤掉。

    2026-10-28 实测：自检每 180 秒建删一次探针目录，一个月在云盘根目录留下
    478 条同名 `_xdrive_healthcheck`（全部 trashed=true，网页看不到，但 API
    每次都返回）。不过滤的话调用方会以为目录里塞满了垃圾。
    """
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"files": [
            {"id": "live", "name": "X-Drive", "kind": "drive#folder", "trashed": False},
            {"id": "dead1", "name": "_xdrive_healthcheck", "kind": "drive#folder", "trashed": True},
            {"id": "dead2", "name": "_xdrive_healthcheck", "kind": "drive#folder", "trashed": True},
        ]})

    items = xunleipan.list_files("", client=client(handler))

    assert [i["name"] for i in items] == ["X-Drive"], "墓碑不该出现在活目录列表里"


def test_list_files_trashed_only_returns_trashed(monkeypatch):
    """要回收站就只给回收站 —— 活目录条目不能混进来（照着删会误删真数据）。"""
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "trashed" in request.url.params.get("filters", "")
        return httpx.Response(200, json={"files": [
            {"id": "live", "name": "X-Drive", "kind": "drive#folder", "trashed": False},
            {"id": "dead", "name": "旧片", "kind": "drive#file", "trashed": True},
        ]})

    items = xunleipan.list_files("", trashed=True, client=client(handler))

    assert [i["name"] for i in items] == ["旧片"]


def test_file_info_prefers_accelerated_link_for_original_quality(monkeypatch):
    """「原始画质」优先用**加速档**（medias[].link.url），不是 web_content_link。

    实测（2026-10-26，MIMK-186，同一份文件两个 URL）：
    · web_content_link（普通）→ 0.08 MB/s（读 256KB 要 3.3 秒）
    · medias[].link（加速）  → 1.57 MB/s（0.2 秒）—— **快 20 倍**
    参数差别：spr=flow→**vip**（SVIP 加速通道，这就是迅雷 App 快的根因）、
    ms/th=5242880→10000000/1000000、vb=0→真实码率、vc=—→h264。
    加速档还自带 token_type=TOKEN_TYPE_ACCELERATION 与 expire。
    """
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "f1", "name": "MIMK-290.mp4", "size": 4408303886,
            "web_content_link": "https://vod-plain/x?a=1",
            "medias": [
                {"media_name": "原始画质",
                 "link": {"url": "https://vod-accel/x?b=2",
                          "token": "ACCEL-TOKEN",
                          "token_type": "TOKEN_TYPE_ACCELERATION",
                          "expire": "2026-10-26T23:55:00+08:00"}},
                {"media_name": "720P", "link": {"url": "https://dl.example/720"}},
            ]})

    info = xunleipan.file_info("f1", client=client(handler))
    assert info["link"] == "https://vod-accel/x?b=2", "应当用加速档"
    assert info["accelerated"] is True
    assert info["plain_link"] == "https://vod-plain/x?a=1"
    assert info["accel_token"] == "ACCEL-TOKEN"
    assert info["accel_token_type"] == "TOKEN_TYPE_ACCELERATION"
    assert info["medias"][0] == {"name": "原始画质", "url": "https://vod-accel/x?b=2"}
    assert info["medias"][1] == {"name": "720P", "url": "https://dl.example/720"}


def test_file_info_falls_back_to_plain_when_no_accelerated_link(monkeypatch):
    """拿不到加速档就退回普通档 —— 不能因此就没直链。"""
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "f1", "name": "MIMK-290.mp4", "size": 100,
            "web_content_link": "https://dl.example/v1/signed",
            "medias": [{"media_name": "原始画质"}]})

    info = xunleipan.file_info("f1", client=client(handler))
    assert info["link"] == "https://dl.example/v1/signed"
    assert info["accelerated"] is False
    assert info["medias"][0] == {"name": "原始画质", "url": "https://dl.example/v1/signed"}


def test_api_error_raises_pan_error(monkeypatch):
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "invalid_token"})

    with pytest.raises(xunleipan.PanError):
        xunleipan.file_info("f1", client=client(handler))


# ─────────────────────────────────────────── 路径解析与直链

def test_resolve_walks_path(monkeypatch):
    _env(monkeypatch)
    listing = {
        "": [{"id": "c1", "name": "迅雷云盘", "kind": "drive#folder", "trashed": False}],
        "c1": [{"id": "c2", "name": "X-Drive", "kind": "drive#folder", "trashed": False}],
        "c2": [{"id": "f1", "name": "MIMK-290.mp4", "kind": "drive#file", "trashed": False}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        parent = request.url.params.get("parent_id", "")
        return httpx.Response(200, json={"files": listing.get(parent, [])})

    fid = xunleipan.resolve("迅雷云盘/X-Drive/MIMK-290.mp4", client=client(handler))
    assert fid == "f1"
    assert xunleipan.resolve("迅雷云盘/没有这个", client=client(handler)) == ""


def test_resolve_drops_mount_prefix_when_absent(monkeypatch):
    """CD2 路径带挂载名 `迅雷云盘/`，官方 API 根下没有它（实测）：自动去首段。"""
    _env(monkeypatch)
    listing = {
        "": [{"id": "c1", "name": "我的资源", "kind": "drive#folder", "trashed": False}],
        "c1": [{"id": "f1", "name": "迅雷11 绿色版.zip", "kind": "drive#file", "trashed": False}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        parent = request.url.params.get("parent_id", "")
        return httpx.Response(200, json={"files": listing.get(parent, [])})

    fid = xunleipan.resolve("迅雷云盘/我的资源/迅雷11 绿色版.zip", client=client(handler))
    assert fid == "f1"


def test_direct_link_end_to_end(monkeypatch):
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v1/files":
            parent = request.url.params.get("parent_id", "")
            rows = {"": [{"id": "f1", "name": "a.mp4", "kind": "drive#file",
                          "trashed": False}]}.get(parent, [])
            return httpx.Response(200, json={"files": rows})
        return httpx.Response(200, json={
            "name": "a.mp4", "web_content_link": "https://dl.example/x"})

    out = xunleipan.direct_link("a.mp4", client=client(handler))
    assert out["link"] == "https://dl.example/x" and out["name"] == "a.mp4"


def test_direct_link_missing_path_raises(monkeypatch):
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"files": []})

    with pytest.raises(xunleipan.PanError):
        xunleipan.direct_link("没有的文件.mp4", client=client(handler))


# ─────────────────────────────────────────── 路径解析缓存（省掉每次 11 秒翻页）

def test_resolve_caches_path_to_file_id(monkeypatch):
    """同一个路径第二次解析不再打接口。

    代价实测：根目录 1839 条（1766 条墓碑）翻 19 页 ≈ 7 秒，加上去首段重试
    的一轮 ≈ 11 秒。看同一个片子的封面/播放/再看一次都是同一个路径。
    """
    _env(monkeypatch)
    listing = {
        "": [{"id": "c1", "name": "X-Drive", "kind": "drive#folder", "trashed": False}],
        "c1": [{"id": "f1", "name": "a.mp4", "kind": "drive#file", "trashed": False}],
    }
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        parent = request.url.params.get("parent_id", "")
        return httpx.Response(200, json={"files": listing.get(parent, [])})

    c = client(handler)
    assert xunleipan.resolve("X-Drive/a.mp4", client=c) == "f1"
    first = calls["n"]
    assert first == 2, "根目录 + 子目录各一次"
    assert xunleipan.resolve("X-Drive/a.mp4", client=c) == "f1"
    assert calls["n"] == first, "第二次必须走缓存，一次接口都不打"


def test_resolve_expires_after_ttl(monkeypatch):
    """TTL 到了要重新解析 —— 文件可能被删或改名。"""
    _env(monkeypatch)
    monkeypatch.setattr(xunleipan, "RESOLVE_CACHE_TTL", 0.0)
    listing = {
        "": [{"id": "c1", "name": "X-Drive", "kind": "drive#folder", "trashed": False}],
        "c1": [{"id": "f1", "name": "a.mp4", "kind": "drive#file", "trashed": False}],
    }
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        parent = request.url.params.get("parent_id", "")
        return httpx.Response(200, json={"files": listing.get(parent, [])})

    c = client(handler)
    xunleipan.resolve("X-Drive/a.mp4", client=c)
    first = calls["n"]
    # 只测解析缓存这一层：分页缓存另有 TTL，这里清掉免得它替我挡下请求
    xunleipan._PAGE_CACHE.clear()
    xunleipan.resolve("X-Drive/a.mp4", client=c)
    assert calls["n"] > first, "过期后必须重新打接口"


def test_resolve_caches_misses_with_short_ttl(monkeypatch):
    """解析不到也缓存（短 TTL）：不该为「就是没有」重复翻 19 页。"""
    _env(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"files": []})

    c = client(handler)
    assert xunleipan.resolve("X-Drive/nope.mp4", client=c) == ""
    first = calls["n"]
    assert first > 0
    assert xunleipan.resolve("X-Drive/nope.mp4", client=c) == ""
    assert calls["n"] == first, "落空结果也该命中缓存"

    # 但落空的 TTL 比命中短：过期后要重新试（文件可能是刚上传的）
    monkeypatch.setattr(xunleipan, "RESOLVE_MISS_TTL", 0.0)
    xunleipan._PAGE_CACHE.clear()
    xunleipan.resolve("X-Drive/nope.mp4", client=c)
    assert calls["n"] > first


def test_resolve_learns_mount_prefix_and_skips_it(monkeypatch):
    """学到「首段是 CD2 挂载名」之后，不再从首段下钻（省一轮必败的翻页）。"""
    _env(monkeypatch)
    listing = {
        "": [{"id": "c1", "name": "X-Drive", "kind": "drive#folder", "trashed": False}],
        "c1": [{"id": "f1", "name": "a.mp4", "kind": "drive#file", "trashed": False}],
    }
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        parent = request.url.params.get("parent_id", "")
        return httpx.Response(200, json={"files": listing.get(parent, [])})

    c = client(handler)
    # 首段 `迅雷云盘` 在官方根下不存在 → 完整路径失败一次，去掉首段成功
    assert xunleipan.resolve("迅雷云盘/X-Drive/a.mp4", client=c) == "f1"
    assert "迅雷云盘" in xunleipan._MOUNT_NAMES, "失败一次就该学到这个挂载名"

    # 换一个没缓存过的路径：因为已知首段是挂载名，直接从第二段开始下钻
    xunleipan.clear_caches()
    xunleipan._MOUNT_NAMES["迅雷云盘"] = time.time()
    calls["n"] = 0
    assert xunleipan.resolve("迅雷云盘/X-Drive/b.mp4", client=c) == ""
    # 直接从 X-Drive 开始：根目录找 X-Drive + X-Drive 里找 b.mp4 = 2 次；
    # 若没学到挂载名，还要多翻一整轮首段（根目录 1 次 + 失败），至少 3 次
    assert calls["n"] == 2, f"学到挂载名后不该再翻首段那一轮，实际打了 {calls['n']} 次"


def test_clear_caches_empties_all_dicts(monkeypatch):
    _env(monkeypatch)
    xunleipan._RESOLVE_CACHE["X/a.mp4"] = (0.0, "f1")
    xunleipan._MOUNT_NAMES["迅雷云盘"] = 0.0
    xunleipan._PAGE_CACHE["\x00" + "0"] = (0.0, ([], ""))
    xunleipan.clear_caches()
    assert xunleipan._RESOLVE_CACHE == {}
    assert xunleipan._MOUNT_NAMES == {}
    assert xunleipan._PAGE_CACHE == {}


def test_gallery_siblings_share_the_directory_listing(monkeypatch):
    """图片包逐张取直链：3 张图在同一目录，只该翻一遍目录。

    实测代价：归档目录 `X-Drive/MIMK-033` 的父级是根目录（1839 条 / 19 页 /
    7 秒）。3 张图各翻一遍 = 21 秒；共享之后仍是一次。
    """
    _env(monkeypatch)
    listing = {
        "": [{"id": "c1", "name": "X-Drive", "kind": "drive#folder", "trashed": False}],
        "c1": [{"id": f"f{i}", "name": f"MIMK-033-{i}.jpg", "kind": "drive#file",
                "trashed": False} for i in (1, 2, 3)],
    }
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        parent = request.url.params.get("parent_id", "")
        return httpx.Response(200, json={"files": listing.get(parent, [])})

    c = client(handler)
    for i in (1, 2, 3):
        assert xunleipan.resolve(f"X-Drive/MIMK-033-{i}.jpg", client=c) == f"f{i}"
    # 第一次解析 2 次请求（根 + 目录）；后两张各自只需再确认父目录那一页
    # 已被缓存 → 命中解析缓存则更少。总之不该是 3 × 2 = 6 次。
    assert calls["n"] <= 3, f"兄弟文件不该重复翻目录，实际打了 {calls['n']} 次"


def test_delete_file_clears_the_cache(monkeypatch):
    """删了文件，路径 → file_id 的映射必须立刻作废。"""
    _env(monkeypatch)
    xunleipan._RESOLVE_CACHE["X/a.mp4"] = (0.0, "f1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    assert xunleipan.delete_file("f1", client=client(handler)) == "已删除"
    assert xunleipan._RESOLVE_CACHE == {}, "删除成功要清缓存"


# ─────────────────────────────────────────── captcha 签发与写操作

def test_sign_captcha_posts_contract(monkeypatch):
    """契约来自公开 chunk service-captcha：
    POST {shield}/v1/shield/captcha/init
    body {client_id, action, device_id, captcha_token, meta}
    """
    _env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"captcha_token": "ck0.new", "expires_in": 300})

    token, expires = xunleipan.sign_captcha("DELETE:/drive/v1/files/x",
                                            previous="ck0.old", client=client(handler))
    assert token == "ck0.new" and expires == 300
    assert seen["url"].endswith("/v1/shield/captcha/init")
    assert seen["body"]["action"] == "DELETE:/drive/v1/files/x"
    assert seen["body"]["captcha_token"] == "ck0.old", "必须回传旧 token 走轮换链"
    assert seen["body"]["device_id"]


def test_list_files_trashed_flag(monkeypatch):
    """回收站过滤必须用 filters（实测 trashed=true / filter=trash 都被静默忽略，
    照着那种列表删会误伤活目录）。"""
    _env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"files": []})

    xunleipan.list_files(trashed=True, client=client(handler))
    assert seen.get("filters") == '{"trashed":{"eq":true}}'
    assert "trashed" not in seen, "trashed=true 会被服务端忽略，必须用 filters"


def test_delete_file_rotates_captcha(monkeypatch):
    """删除流程：先带旧 token 打一次（被判 captcha）→ 回传旧 token 签发新的
    → 带新 token 再打一次才生效。跳过第一步会被判 captcha_invalid。"""
    _env(monkeypatch)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/shield/captcha/init"):
            calls.append("sign")
            return httpx.Response(200, json={"captcha_token": "ck0.fresh", "expires_in": 300})
        calls.append("delete")
        tok = request.headers.get("x-captcha-token")
        if tok == "ck0.fresh":
            return httpx.Response(200, json={})
        return httpx.Response(400, json={"error": "captcha_invalid"})

    out = xunleipan.delete_file("f1", client=client(handler))
    assert out == "已删除"
    assert calls == ["delete", "sign", "delete"], calls


def test_delete_file_reports_when_captcha_still_refused(monkeypatch):
    _env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/shield/captcha/init"):
            return httpx.Response(200, json={"captcha_token": "ck0.fresh", "expires_in": 300})
        return httpx.Response(400, json={"error": "captcha_invalid"})

    with pytest.raises(xunleipan.PanError) as err:
        xunleipan.delete_file("f1", client=client(handler))
    assert "captcha" in str(err.value)


# ─────────────────────────────────────────── 翻页解析路径

def test_walk_paginates_past_first_page(monkeypatch):
    """目录条目多时，目标可能在后面的页 —— 不翻页就永远解析不到。

    2026-10-28 实测：某账号云盘根目录 500 条（494 条是删了又重建留下的墓碑），
    `X-Drive` 排在第 6 页。只读第一页时 `resolve()` 返回空，直链只能退回 CD2
    内网地址（浏览器打不开），表现为「获取迅雷云盘直链失败」。
    """
    _env(monkeypatch)
    pages = {
        "": {"files": [{"id": "t1", "name": "_xdrive_healthcheck",
                        "kind": "drive#folder", "trashed": True}],
             "next_page_token": "p2"},
        "p2": {"files": [{"id": "xd", "name": "X-Drive",
                          "kind": "drive#folder", "trashed": False}],
               "next_page_token": ""},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("page_token", "")
        return httpx.Response(200, json=pages[token])

    c = client(handler)
    try:
        fid = xunleipan.resolve("X-Drive", client=c)
    finally:
        c.close()
    assert fid == "xd", "第二页的目录也要能解析到"


def test_iter_files_stops_when_no_more_pages(monkeypatch):
    """没有 next_page_token / 空页就收手，不能无限翻。"""
    _env(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"files": [
            {"id": "a", "name": "A", "kind": "drive#folder", "trashed": False}]})

    c = client(handler)
    try:
        items = list(xunleipan.iter_files("", client=c))
    finally:
        c.close()
    assert [i["name"] for i in items] == ["A"]
    assert calls["n"] == 1, "已到尽头就只该请求一次"


def test_iter_files_respects_max_pages(monkeypatch):
    """接口万一一直给 next_page_token，也不能无限翻。"""
    _env(monkeypatch)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={
            "files": [{"id": "a", "name": "A", "kind": "drive#folder", "trashed": False}],
            "next_page_token": "always-more"})

    c = client(handler)
    try:
        items = list(xunleipan.iter_files("", client=c, max_pages=3))
    finally:
        c.close()
    assert calls["n"] == 3
    assert len(items) == 3
