"""磁力预览缓存：按 infohash 命中、TTL、负缓存。"""
import time

from app import preview

MAG = "magnet:?xt=urn:btih:5806ff5dc7f98f044e1d85d45dd405c9fd1bca30&dn=x&tr=udp%3A%2F%2Fa"

def test_key_ignores_trackers_and_name():
    key = preview.PreviewCache.key_of(MAG)
    assert key == "5806ff5dc7f98f044e1d85d45dd405c9fd1bca30"
    assert preview.PreviewCache.key_of("https://example.com/a.mp4") == ""

def test_put_then_get_hits_cache(tmp_path):
    c = preview.PreviewCache(str(tmp_path / "p.json"))
    assert c.get(MAG) is None
    c.put(MAG, {"name": "Sintel", "size": 129302391})
    got = c.get(MAG)
    assert got["name"] == "Sintel" and got["size"] == 129302391

def test_same_infohash_different_trackers_share_entry(tmp_path):
    c = preview.PreviewCache(str(tmp_path / "p.json"))
    c.put(MAG, {"name": "A"})
    other = "magnet:?xt=urn:btih:5806FF5DC7F98F044E1D85D45DD405C9FD1BCA30&tr=udp%3A%2F%2Fb"
    assert c.get(other)["name"] == "A", "大小写与 trackers 不同也该命中"

def test_negative_cache_expires_fast(tmp_path):
    c = preview.PreviewCache(str(tmp_path / "p.json"))
    c.put(MAG, None)
    assert c.get(MAG) == {}, "负缓存命中返回空字典而不是 None"
    path = tmp_path / "p.json"
    import json
    data = json.loads(path.read_text())
    key = preview.PreviewCache.key_of(MAG)
    data[key]["at"] = time.time() - preview.FAIL_TTL - 5
    path.write_text(json.dumps(data))
    assert c.get(MAG) is None, "负缓存过期后应重新请求"

def test_success_cache_survives_negttl(tmp_path):
    c = preview.PreviewCache(str(tmp_path / "p.json"))
    c.put(MAG, {"name": "A"})
    path = tmp_path / "p.json"
    import json
    data = json.loads(path.read_text())
    data[preview.PreviewCache.key_of(MAG)]["at"] = time.time() - preview.FAIL_TTL - 5
    path.write_text(json.dumps(data))
    assert c.get(MAG)["name"] == "A", "成功结果 7 天内有效"

def test_prune_removes_stale(tmp_path):
    c = preview.PreviewCache(str(tmp_path / "p.json"))
    c.put(MAG, {"name": "A"})
    path = tmp_path / "p.json"
    import json
    data = json.loads(path.read_text())
    data[preview.PreviewCache.key_of(MAG)]["at"] = time.time() - 40 * 86400
    path.write_text(json.dumps(data))
    assert c.prune(30) == 1 and c.get(MAG) is None
