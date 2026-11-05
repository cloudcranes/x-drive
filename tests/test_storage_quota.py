"""CloudDrive2 quota probes the archive root; unavailable capacity stays unknown."""
from app.storage.clouddrive2 import CloudDrive2
from tests.cd2_fake import FakeCd2


def test_cd2_quota_reports_space():
    fake = FakeCd2().start()
    try:
        backend = CloudDrive2(fake.url, fake.user, fake.password, chunk_mb=1,
                              task_timeout=20)
        q = backend.quota("迅雷云盘/X-Drive")
    finally:
        fake.stop()

    assert q.known
    assert q.total == 100 << 30
    assert q.used == 40 << 30
    assert q.free == 60 << 30


def test_cd2_quota_asks_the_given_path_not_root():
    """必须问归档根那一层：CD2 的 "/" 是聚合根，数字离谱。"""
    fake = FakeCd2().start()
    try:
        backend = CloudDrive2(fake.url, fake.user, fake.password, chunk_mb=1,
                              task_timeout=20)
        backend.quota("迅雷云盘/X-Drive")
        asked = [p for p in fake.space_paths]
    finally:
        fake.stop()

    assert asked, "没问过容量"
    assert all("/" in p and "迅雷云盘" in p for p in asked), \
        f"问错了层：{asked}（聚合根的容量不能信）"


def test_cd2_quota_error_returns_unknown():
    """连不上时给「不知道」，别抛也别报 0。"""
    fake = FakeCd2().start()
    try:
        backend = CloudDrive2(fake.url, fake.user, fake.password, chunk_mb=1,
                              task_timeout=20)
        fake.stop()          # 服务端没了
        q = backend.quota("迅雷云盘/X-Drive")
    finally:
        try:
            fake.stop()
        except Exception:
            pass

    assert not q.known


def test_storage_quota_returns_unknown_when_backend_broken(monkeypatch):
    from app import storage

    class Boom:
        kind = "clouddrive2"

        def quota(self, probe_path=""):
            raise RuntimeError("炸了")

    monkeypatch.setattr(storage, "get", lambda which=None: Boom())

    q = storage.quota()
    assert not q.known


def test_storage_quota_returns_unknown_when_backend_lacks_method(monkeypatch):
    """契约外的后端没 quota 方法：不能炸，返回「不知道」。"""
    from app import storage

    class Old:
        kind = "clouddrive2"

    monkeypatch.setattr(storage, "get", lambda which=None: Old())

    q = storage.quota()
    assert not q.known
