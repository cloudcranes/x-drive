"""CD2 上传的消息长度限制：chunk 必须小于服务端 4MB 上限。

2026-10-25 真机实测（迅雷云盘 + CD2 1.0.18）：
`upload_dir` 直接报
    SERVER: Received message larger than max (4194318 vs. 4194304)
gRPC 单条消息的**服务端**上限是 4MB，而上传按 chunk 切 ——
`CD2_CHUNK_MB` 默认**也是 4**，两者相等，加上 protobuf 消息头（实测 14 字节）
就必然超限。

三个要点：
  1. 限制在**服务端**：抬客户端的 max_send_message_length 没用；
  2. 抬服务端上限做不到 —— 只能把 chunk 压下来；
  3. 打桩测不出来：假服务不经过 gRPC 编码，没有长度上限。

所以这里对着**真 gRPC 服务**验，并且明确记下「4MB chunk 会被服务端拒」这个事实。
"""
from __future__ import annotations

import pytest

from app.storage import cd2_grpc
from app.storage import clouddrive2 as cd2mod
from app.storage.proto import CloudDrive_pb2 as pb
from tests.cd2_fake import FakeCd2


SERVER_MAX = 4 * 1024 * 1024          # CD2 服务端单条消息上限


def test_default_chunk_is_below_the_server_limit():
    """适配器默认 chunk 必须小于服务端上限 —— 相等就会超限。"""
    backend = object.__new__(cd2mod.CloudDrive2)
    backend.chunk = int(cd2mod.CloudDrive2.MAX_CHUNK_MB * 1024 * 1024)
    assert backend.chunk < SERVER_MAX
    # 还要给 protobuf 消息头留余量
    assert SERVER_MAX - backend.chunk > 1024


def test_configured_chunk_above_the_limit_is_clamped():
    """用户把 CD2_CHUNK_MB 配成 4（或更大）时必须被压下去，不能照用。"""
    fake = FakeCd2().start()
    try:
        backend = cd2mod.CloudDrive2(fake.url, fake.user, fake.password, chunk_mb=4)
        assert backend.chunk <= int(cd2mod.CloudDrive2.MAX_CHUNK_MB * 1024 * 1024)
        assert backend.chunk < SERVER_MAX
        backend2 = cd2mod.CloudDrive2(fake.url, fake.user, fake.password, chunk_mb=64)
        assert backend2.chunk < SERVER_MAX, "更大的配置也要被压住"
    finally:
        fake.stop()


def test_channel_creation_sets_the_message_limit():
    """建通道时把上限传下去（options 拼错等于没设）。"""
    fake = FakeCd2().start()
    try:
        chan = cd2_grpc.Cd2Channel(fake.url, fake.user, fake.password)
        options = getattr(chan, "_options", None)
        assert options, "通道必须带上 options"
        keys = {key for key, _ in options}
        assert "grpc.max_send_message_length" in keys
        assert "grpc.max_receive_message_length" in keys
        for _key, value in options:
            assert value == cd2_grpc.MAX_MESSAGE_BYTES
        chan.close()
    finally:
        fake.stop()


def test_four_megabyte_chunk_is_rejected_by_the_server():
    """钉住真机事实：4MB chunk 会被服务端拒（这正是缺陷本身）。"""
    pytest.importorskip("grpc")
    fake = FakeCd2().start()
    try:
        chan = cd2_grpc.Cd2Channel(fake.url, fake.user, fake.password)
        payload = b"x" * SERVER_MAX
        probe = pb.WriteFileRequest(fileHandle=1, startPos=0, length=len(payload),
                                    buffer=payload, closeFile=True)
        encoded = len(probe.SerializeToString())
        assert encoded > SERVER_MAX, f"4MB chunk 编码后应超过上限：{encoded}"
        assert encoded - len(payload) < 64 * 1024, "协议头不该有大得离谱的开销"

        created = chan.call("CreateFile",
                            pb.CreateFileRequest(parentPath="/", fileName="limit_probe.bin"),
                            pb.CreateFileResult)
        handle = int(created.fileHandle or 0)
        assert handle
        big = pb.WriteFileRequest(fileHandle=handle, startPos=0, length=len(payload),
                                   buffer=payload, closeFile=True)
        with pytest.raises(Exception) as err:
            chan.call("WriteToFileStream", lambda: iter([big]), pb.WriteFileResult,
                      mode=cd2_grpc.STREAM_IN, timeout=60)
        assert "larger than max" in str(err.value) or "message length too large" in str(err.value)
        chan.close()
    finally:
        fake.stop()


def test_clamped_chunk_is_accepted_by_the_server():
    """压下来的 chunk 必须能真发出去 —— 这才是修复的验收点。"""
    pytest.importorskip("grpc")
    fake = FakeCd2().start()
    try:
        backend = cd2mod.CloudDrive2(fake.url, fake.user, fake.password, chunk_mb=4)
        chan = cd2_grpc.Cd2Channel(fake.url, fake.user, fake.password)
        payload = b"x" * backend.chunk
        created = chan.call("CreateFile",
                            pb.CreateFileRequest(parentPath="/", fileName="limit_ok.bin"),
                            pb.CreateFileResult)
        handle = int(created.fileHandle or 0)
        big = pb.WriteFileRequest(fileHandle=handle, startPos=0, length=len(payload),
                                   buffer=payload, closeFile=True)
        assert len(big.SerializeToString()) <= SERVER_MAX
        chan.call("WriteToFileStream", lambda: iter([big]), pb.WriteFileResult,
                  mode=cd2_grpc.STREAM_IN, timeout=60)
        assert fake.files["/limit_ok.bin"] == len(payload)
        chan.close()
    finally:
        fake.stop()
