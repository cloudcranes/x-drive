"""任务目录归属与清理：没标记的目录一律不碰。"""
import pytest

from app import workspace

def test_claim_and_owned(tmp_path):
    d = tmp_path / "SSIS123"
    workspace.claim(d, "SSIS123", "magnet:?xt=urn:btih:aa")
    assert workspace.owned(d, "SSIS123")
    assert not workspace.owned(d, "OTHER1")

def test_claim_refuses_directory_without_mark(tmp_path):
    """既有但无标记的目录绝不接管 —— 那是别人的数据，补个标记就能删掉是致命的。"""
    d = tmp_path / "SSIS123"
    d.mkdir()
    (d / "external.mp4").write_bytes(b"external")
    with pytest.raises(RuntimeError, match="没有归属标记"):
        workspace.claim(d, "SSIS123")
    assert not workspace.owned(d, "SSIS123"), "不能凭空变成自己的"
    assert (d / "external.mp4").exists()

def test_claim_refuses_foreign_directory(tmp_path):
    """同名目录属于别的任务时不能接管 —— 那是别人的下载。"""
    d = tmp_path / "SSIS123"
    workspace.claim(d, "SSIS123")
    with pytest.raises(RuntimeError, match="属于另一个任务"):
        workspace.claim(d, "OTHER1")

def test_claim_is_idempotent_for_same_key(tmp_path):
    d = tmp_path / "SSIS123"
    workspace.claim(d, "SSIS123")
    workspace.claim(d, "SSIS123")
    assert workspace.owned(d, "SSIS123")

def test_owned_path_accepts_dest_and_xunlei_suffix(tmp_path):
    dest = tmp_path / "SSIS123"
    workspace.claim(dest, "SSIS123")
    renamed = tmp_path / "SSIS123-1"
    renamed.mkdir()
    assert workspace.shape_of(renamed, dest), "迅雷重名改成 -N 属于本任务"
    assert not workspace.owned_path(renamed, dest, "SSIS123"), "还没打标记就不算已归属"
    workspace.adopt(renamed, "SSIS123")
    assert workspace.owned_path(renamed, dest, "SSIS123")

def test_adopt_refuses_other_owners_directory(tmp_path):
    d = tmp_path / "SSIS123-3"
    d.mkdir()
    workspace.adopt(d, "SSIS123")
    with pytest.raises(RuntimeError, match="属于另一个任务"):
        workspace.adopt(d, "OTHER1")

def test_owned_path_rejects_similar_prefix(tmp_path):
    """`SSIS123OTHER` 不是 `SSIS123` 的目录，前缀相同不算数。"""
    dest = tmp_path / "SSIS123"
    workspace.claim(dest, "SSIS123")
    other = tmp_path / "SSIS123OTHER"
    other.mkdir()
    assert not workspace.shape_of(other, dest)
    assert not workspace.owned_path(other, dest, "SSIS123")
    nested = tmp_path / "nested" / "SSIS123"
    nested.mkdir(parents=True)
    assert not workspace.owned_path(nested, dest, "SSIS123"), "别的父目录也不算"

def test_owned_path_rejects_unmarked_suffixed_dir(tmp_path):
    dest = tmp_path / "SSIS123"
    workspace.claim(dest, "SSIS123")
    stranger = tmp_path / "SSIS123-2"
    stranger.mkdir()
    assert not workspace.owned_path(stranger, dest, "SSIS123"), "无标记一律不认"

def test_drop_only_removes_owned(tmp_path):
    mine = tmp_path / "mine"
    workspace.claim(mine, "mine")
    (mine / "a.mp4").write_bytes(b"x")
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / "b.mp4").write_bytes(b"x")

    assert workspace.drop(mine, "mine") is True and not mine.exists()
    assert workspace.drop(stranger, "stranger") is False and stranger.exists(), "无标记不删"

def test_drop_workspace_checks_directory_name(tmp_path):
    ws = tmp_path / "state" / "SSIS123"
    ws.mkdir(parents=True)
    assert workspace.drop_workspace(ws, "SSIS123") is True
    other = tmp_path / "state" / "ABP456"
    other.mkdir(parents=True)
    assert workspace.drop_workspace(other, "SSIS123") is False and other.exists()

def test_key_of_normalises(tmp_path):
    assert workspace.key_of("SSIS-123") == "SSIS123"
    assert workspace.key_of("") == "job"
    assert workspace.staging(str(tmp_path), "SSIS-123") == tmp_path / "SSIS123"
    assert workspace.workspace(str(tmp_path), "SSIS-123") == tmp_path / "state" / "SSIS123"

def test_shape_of_rejects_same_length_different_prefix(tmp_path):
    """长度相同、后缀也像 `-1`，但前缀不同 —— 不是本任务的目录。"""
    dest = tmp_path / "TEST001"
    assert workspace.shape_of(tmp_path / "OTHER99-1", dest) is False
    assert workspace.shape_of(tmp_path / "TEST001-1", dest) is True
    assert workspace.shape_of(tmp_path / "TEST001X-1", dest) is False
    assert workspace.shape_of(tmp_path / "TEST001-a", dest) is False


def test_mark_records_task_evidence(tmp_path):
    """标记要能回答「这是哪个任务下的」，不能只有一个番号。"""
    d = tmp_path / "SSIS123"
    workspace.claim(d, "SSIS123", "magnet:?xt=urn:btih:abc", task_id="T9", infohash="abc")
    ev = workspace.evidence_of(d)
    assert ev["owner"] == "SSIS123" and ev["task"] == "T9" and ev["btih"] == "abc"
    assert workspace.owned(d, "SSIS123")


def test_legacy_mark_format_still_owned(tmp_path):
    """老版本标记只有三行，升级后仍要认得出是自己建的。"""
    d = tmp_path / "SSIS123"
    d.mkdir()
    (d / ".x-drive-owner").write_text("SSIS123\n2026-01-01T00:00:00\nmagnet:x", encoding="utf-8")
    assert workspace.owned(d, "SSIS123")
    assert workspace.evidence_of(d)["owner"] == "SSIS123"
