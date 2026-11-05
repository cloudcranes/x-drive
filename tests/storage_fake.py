"""Small CloudDrive2-shaped upload double for sidecar behavior tests."""
from pathlib import Path
from types import SimpleNamespace

from app.storage.base import StorageError


def upload_backend(monkeypatch, web, upload):
    backend = SimpleNamespace(kind="clouddrive2", upload_file=upload)
    monkeypatch.setattr(web, "storage_backend", lambda: backend)
    monkeypatch.setattr(web.storage, "kind", lambda: "clouddrive2")
    return backend


def capture_uploads(monkeypatch, web, fail=()):
    sent = []

    def upload(local, remote):
        name = remote.rsplit("/", 1)[-1]
        sent.append({"name": name, "data": Path(local).read_bytes()})
        if name in fail:
            raise StorageError("boom")
        return remote

    upload_backend(monkeypatch, web, upload)
    return sent
