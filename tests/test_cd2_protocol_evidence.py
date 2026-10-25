"""上传完成证据：对着**真协议**（进程内假 gRPC 服务）跑完整上传链路。

为什么要有这个文件：前面的 test_cd2_completion_evidence.py 用打桩对象替换了
`_uploads` / `_find`，验证的是「分支逻辑」；本文件验证的是**整条链路** ——
真的建文件、真的分流、真的查任务列表、真的回读远端大小。协议层的差异
（任务状态怎么报、destPath 怎么带前缀、文件什么时候出现）打桩是看不到的。

三个必守的不变式：
  1. 远端文件已出现且长度等于本地 —— **不足以**判定上传完成（任务还在跑）；
  2. 任务 Finished —— 仍要核验远端大小精确相等；
  3. 任务从列表里消失 —— 需要连续两次精确大小回读，中途变化要重数。
"""
from __future__ import annotations

import threading
import time

import pytest

from app.storage import clouddrive2 as cd2mod
from app.storage.base import StorageError
from tests.cd2_fake import FakeCd2


@pytest.fixture()
def fake():
    server = FakeCd2().start()
    try:
        yield server
    finally:
        server.stop()


def backend(fake, **kw):
    kw.setdefault("chunk_mb", 1)
    kw.setdefault("task_timeout", 3.0)
    kw.setdefault("call_timeout", 10)
    return cd2mod.CloudDrive2(fake.url, fake.user, fake.password, **kw)


def _work(tmp_path, name="main.mp4", size=64):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / name).write_bytes(b"x" * size)
    return work


# ─────────────────────────────────────────── 进行中的任务不能算完成

def test_running_task_with_full_length_file_is_not_complete(fake, tmp_path):
    """真协议：文件已按最终长度落盘，但任务仍在传输 —— 不能返回成功。

    这是本次修复的核心场景：以前只看「远端文件存在」，预分配的文件一出现
    就会把上传判成完成，接着业务链会记账并删掉本地原件。
    """
    fake.pending_upload = {}
    cd2 = backend(fake)
    work = _work(tmp_path, size=64)

    err: list[BaseException] = []

    def run():
        try:
            cd2.upload_dir(work, "X-Drive/DEMO")
        except BaseException as exc:      # noqa: BLE001
            err.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    # 等任务进入「传输中」：远端文件已出现，长度已等于本地
    deadline = time.time() + 5
    while time.time() < deadline and "/X-Drive/DEMO/main.mp4" not in fake.files:
        time.sleep(0.02)
    assert "/X-Drive/DEMO/main.mp4" in fake.files, "预分配文件应当已经出现"
    assert fake.files["/X-Drive/DEMO/main.mp4"] == 64, "长度已等于本地大小"

    # 任务仍在进行 → 适配器必须一直等，不能返回成功
    thread.join(timeout=1.0)
    assert thread.is_alive(), "上传任务还在跑，适配器不该提前返回"
    assert not err, f"不该有异常，也不该成功：{err}"

    # 推进到完成 → 适配器应当正常返回
    fake.finish_pending_upload()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not err, f"完成后不该报错：{err}"


def test_running_task_then_error_is_reported_not_swallowed(fake, tmp_path):
    """任务在跑着变成 Error —— 报错要带原因，不能当成完成。"""
    fake.pending_upload = {}
    cd2 = backend(fake)
    work = _work(tmp_path, size=32)

    err: list[BaseException] = []

    def run():
        try:
            cd2.upload_dir(work, "X-Drive/DEMO")
        except BaseException as exc:      # noqa: BLE001
            err.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and not fake.uploads:
        time.sleep(0.02)
    assert fake.uploads, "上传任务应当已经登记"
    fake.fail_pending_upload("cloud said no")
    thread.join(timeout=5)
    assert err, "任务失败必须抛错"
    assert isinstance(err[0], StorageError)
    assert "cloud said no" in str(err[0])


def test_finished_task_with_wrong_final_size_is_rejected(fake, tmp_path, monkeypatch):
    """任务报 Finished，但远端大小对不上 —— 拒绝完成。

    真协议下把「远端大小」改小：任务说下完了，文件却只有一半。
    """
    cd2 = backend(fake)
    work = _work(tmp_path, size=64)
    original_find = cd2._find

    def half_size(path):
        found = original_find(path)
        if found is not None and not found.isDirectory:
            return type(found)(name=found.name, size=max(0, int(found.size or 0) // 2),
                               isDirectory=False)
        return found

    monkeypatch.setattr(cd2, "_find", half_size)
    with pytest.raises(StorageError, match="完成核验失败|超时"):
        cd2.upload_dir(work, "X-Drive/DEMO")


def test_normal_upload_completes_over_real_protocol(fake, tmp_path):
    """对照：一切正常时整条链路走通，远端文件与本地一致。"""
    cd2 = backend(fake)
    work = _work(tmp_path, size=1024)
    size = cd2.upload_dir(work, "X-Drive/DEMO")
    assert size == 1024
    assert fake.files["/X-Drive/DEMO/main.mp4"] == 1024
    assert fake.uploads[-1].statusEnum == cd2mod.FINISHED


def test_upload_never_reports_success_before_task_finishes(fake, tmp_path):
    """整条 upload_dir：返回即代表远端完成 —— 任务还在跑就不能返回。"""
    fake.pending_upload = {}
    cd2 = backend(fake)
    work = _work(tmp_path, size=48)
    done = threading.Event()

    def run():
        cd2.upload_dir(work, "X-Drive/DEMO")
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.4)
    assert not done.is_set(), "任务还在跑，upload_dir 不能返回"
    fake.finish_pending_upload()
    thread.join(timeout=5)
    assert done.is_set(), "任务完成后 upload_dir 应当返回"
    assert fake.files["/X-Drive/DEMO/main.mp4"] == 48
