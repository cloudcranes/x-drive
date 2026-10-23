"""本地残留盘点：只读列出带归属标记的目录，分类已归档残留 / 失败保留。

背景：归档回收删的是 Drive，本地 TMP_ROOT 与 STATE_DIR 的目录一个字节都不动。
磁盘满了用户点回收，以为腾了空间，本地还是满的。这个接口把本地占用摊开。

纪律（三条都钉死）：
  1. 只读 —— 不删目录、不删文件、不改状态，调用前后目录结构不变；
  2. 没有归属标记的目录一律跳过、不报告；
  3. 探测失败不抛异常。

全部离线：tmp_path 造目录，monkeypatch 掉 web.TMP_ROOT / STATE_DIR / STATE。
"""
from __future__ import annotations

import hashlib

from app import web, workspace


def _mark(dir, key, extra=None):
    """造一个带归属标记的目录（等价 workspace.claim 的结果，但 claim 会拒已存在目录）。"""
    dir.mkdir(parents=True, exist_ok=True)
    lines = [workspace.key_of(key)]
    for name, value in (extra or {}).items():
        if value not in (None, ""):
            lines.append(f"{name}={value}")
    (dir / workspace.OWNER_MARK).write_text("\n".join(lines), encoding="utf-8")


def _tree_hash(path) -> str:
    """目录结构指纹：验证「只读」时用它判断目录没被动过。"""
    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        rel = str(p.relative_to(path))
        h.update(rel.encode())
        if p.is_file() and not p.is_symlink():
            h.update(b"F")
            h.update(str(p.stat().st_size).encode())
        else:
            h.update(b"D")
    return h.hexdigest()


def test_inventory_reports_only_marked_dirs_and_classifies(tmp_path, monkeypatch):
    dl = tmp_path / "downloads"
    work = tmp_path / "work"
    (dl).mkdir(); (work / "state").mkdir(parents=True)

    # 已归档番号（在 STATE 里）
    core_state = {"SSIS-123": {"code": "SSIS-123"}}
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    from app import core
    core.save_state(web.STATE, core_state)
    monkeypatch.setattr(web, "TMP_ROOT", str(dl))
    monkeypatch.setattr(web, "STATE_DIR", str(work))

    # 带标记的目录：一个已归档残留（SSIS-123 在归档表），一个失败保留
    a = dl / "SSIS123"
    _mark(a, "SSIS-123")
    (a / "a.mp4").write_bytes(b"x" * 100)
    b = dl / "ABP456"
    _mark(b, "ABP-456")
    (b / "b.mp4").write_bytes(b"x" * 200)
    # 工作区里一个带标记的
    c = work / "state" / "SSIS123"
    _mark(c, "SSIS-123")
    (c / "c.mp4").write_bytes(b"x" * 50)
    # 无标记的目录：必须被跳过
    nomark = dl / "nomark"
    nomark.mkdir()
    (nomark / "x.mp4").write_bytes(b"x" * 9999)

    out = web.local_disk_inventory()

    assert out["ok"] is True
    keys = {i["key"] for i in out["items"]}
    assert keys == {"SSIS123", "ABP456"}, f"只该报告带标记的目录：{keys}"
    # 同 key 可能出现在 downloads 和 workspace 两处，不能按 key 建单值映射
    ss = [i for i in out["items"] if i["key"] == "SSIS123"]
    abp = [i for i in out["items"] if i["key"] == "ABP456"]
    assert all(i["kind"] == "已归档残留" for i in ss)
    assert abp[0]["kind"] == "失败保留"
    assert {i["bucket"] for i in ss} == {"downloads", "workspace"}
    assert abp[0]["bucket"] == "downloads"
    # size 含归属标记文件本身（约几字节），只断言 >= 内容体积、且大致正确
    assert abp[0]["size"] >= 200, f"size 应至少含 200 字节内容：{abp[0]['size']}"
    assert out["by_kind"]["已归档残留"] == 2  # downloads + workspace 各一个
    assert out["by_kind"]["失败保留"] == 1


def test_unmarked_directory_is_never_reported(tmp_path, monkeypatch):
    dl = tmp_path / "dl"
    dl.mkdir()
    nomark = dl / "someone-elses"
    nomark.mkdir()
    (nomark / "keep.mp4").write_bytes(b"z" * 500)
    monkeypatch.setattr(web, "TMP_ROOT", str(dl))
    monkeypatch.setattr(web, "STATE_DIR", str(tmp_path / "work"))
    from app import core
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {})

    out = web.local_disk_inventory()

    assert out["items"] == [], "无标记目录不许被报告"
    assert out["count"] == 0


def test_inventory_is_read_only(tmp_path, monkeypatch):
    dl = tmp_path / "dl"
    dl.mkdir()
    a = dl / "SSIS123"
    _mark(a, "SSIS-123")
    (a / "a.mp4").write_bytes(b"x" * 100)
    monkeypatch.setattr(web, "TMP_ROOT", str(dl))
    monkeypatch.setattr(web, "STATE_DIR", str(tmp_path / "work"))
    from app import core
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {"SSIS-123": {}})

    before = _tree_hash(dl)
    web.local_disk_inventory()
    after = _tree_hash(dl)

    assert before == after, "只读接口不得改动目录结构"


def test_inventory_survives_missing_roots(tmp_path, monkeypatch):
    """TMP_ROOT / STATE_DIR 都不存在时，返回空清单而不是抛异常。"""
    monkeypatch.setattr(web, "TMP_ROOT", str(tmp_path / "no-such-dl"))
    monkeypatch.setattr(web, "STATE_DIR", str(tmp_path / "no-such-work"))
    from app import core
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {})

    out = web.local_disk_inventory()

    assert out["ok"] is True
    assert out["items"] == [] and out["total_size"] == 0


def test_inventory_skips_symlinked_dirs(tmp_path, monkeypatch):
    """符号链接的目录不跟进去，也不把链接算成目录。"""
    dl = tmp_path / "dl"
    dl.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    _mark(real, "REAL-1")
    (real / "big.mp4").write_bytes(b"y" * 1000)
    # 符号链接指向 real，但链接本身没有标记 → 不报告
    link = dl / "REAL1"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        import pytest
        pytest.skip("当前环境不支持符号链接")

    monkeypatch.setattr(web, "TMP_ROOT", str(dl))
    monkeypatch.setattr(web, "STATE_DIR", str(tmp_path / "work"))
    from app import core
    monkeypatch.setattr(web, "STATE", str(tmp_path / "state.json"))
    core.save_state(web.STATE, {})

    out = web.local_disk_inventory()

    assert out["items"] == [], "符号链接目录不该被报告（无归属标记）"
