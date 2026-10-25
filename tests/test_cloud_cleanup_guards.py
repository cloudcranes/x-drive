"""离线行为回归：实际观察、整理和清理链保护未知内容。"""
import asyncio
from types import SimpleNamespace

import pytest

from app import cloudingest, cloudorganize
from app.storage.base import StorageError


class Cloud:
    def __init__(self, files):
        self.files = dict(files)
        self.deleted = []

    def list_dirs(self, path):
        prefix = path.rstrip('/') + '/'
        return sorted({p[len(prefix):].split('/', 1)[0] for p in self.files
                       if p.startswith(prefix) and '/' in p[len(prefix):]})

    def list_files(self, path):
        prefix = path.rstrip('/') + '/'
        return [SimpleNamespace(name=p[len(prefix):], size=size)
                for p, size in self.files.items()
                if p.startswith(prefix) and '/' not in p[len(prefix):]]

    def ensure_dir(self, path):
        return path.rsplit('/', 1)[-1]

    def move_file(self, src, dst):
        self.files[dst] = self.files.pop(src)

    def purge(self, path):
        self.deleted.append(path)
        for p in list(self.files):
            if p == path or p.startswith(path + '/'):
                del self.files[p]
        return '已删除'


def organize(cloud, source='seed'):
    landing = cloudingest.measure(cloud, 'inbox')[source or cloudingest.LOOSE_KEY]
    plan = cloudorganize.plan(source, landing['files'], folder_code='DEMO-123')
    out = cloudorganize.apply(cloud, 'inbox', plan, dst_root='archive')
    assert out['files'] == ['DEMO-123.mp4']
    return plan


def test_nested_media_survives_observe_organize_cleanup():
    cloud = Cloud({'inbox/seed/main.mp4': 100,
                   'inbox/seed/extras/second.mp4': 80,
                   'inbox/seed/ad.txt': 1})
    plan = organize(cloud)
    with pytest.raises(cloudorganize.OrganizeError, match='未知内容.*extras/'):
        cloudorganize.cleanup_source(cloud, 'inbox', plan, 'archive')
    assert cloud.deleted == []
    assert cloud.files['inbox/seed/extras/second.mp4'] == 80
    assert cloud.files['archive/DEMO-123/DEMO-123.mp4'] == 100


def test_late_file_and_reappearing_media_are_preserved():
    for late in ('late.mp4', 'main.mp4'):
        cloud = Cloud({'inbox/seed/main.mp4': 100})
        plan = organize(cloud)
        cloud.files['inbox/seed/' + late] = 77
        with pytest.raises(cloudorganize.OrganizeError, match='未知内容'):
            cloudorganize.cleanup_source(cloud, 'inbox', plan, 'archive')
        assert cloud.deleted == []
        assert cloud.files['inbox/seed/' + late] == 77


@pytest.mark.parametrize('operation', ['list_dirs', 'list_files'])
def test_cleanup_read_failure_preserves_source(monkeypatch, operation):
    cloud = Cloud({'inbox/seed/main.mp4': 100, 'inbox/seed/ad.txt': 1})
    plan = organize(cloud)
    def fail(path):
        raise StorageError('fixture read failure')
    monkeypatch.setattr(cloud, operation, fail)
    with pytest.raises(cloudorganize.OrganizeError, match='fixture read failure'):
        cloudorganize.cleanup_source(cloud, 'inbox', plan, 'archive')
    assert cloud.deleted == []
    assert cloud.files['inbox/seed/ad.txt'] == 1


def test_loose_cleanup_removes_only_remaining_known_drop():
    cloud = Cloud({'inbox/main.mp4': 100, 'inbox/ad.txt': 1})
    plan = organize(cloud, '')
    cloudorganize.cleanup_source(cloud, 'inbox', plan, 'archive')
    assert cloud.deleted == ['inbox/ad.txt']
    assert cloud.files == {'archive/DEMO-123/DEMO-123.mp4': 100}


def test_loose_delete_failure_is_visible_to_pending_cleanup(monkeypatch):
    cloud = Cloud({'inbox/main.mp4': 100, 'inbox/ad.txt': 1})
    plan = organize(cloud, '')
    def fail(path):
        raise StorageError('fixture delete failure')
    monkeypatch.setattr(cloud, 'purge', fail)
    with pytest.raises(cloudorganize.OrganizeError, match='fixture delete failure'):
        cloudorganize.cleanup_source(cloud, 'inbox', plan, 'archive')
    assert cloud.files['inbox/ad.txt'] == 1


def test_existing_empty_inbox_single_file_reaches_landing():
    cloud = Cloud({})
    before = cloudingest.snapshot_strict(cloud, 'inbox')
    assert cloudingest.LOOSE_KEY not in before
    cloud.files['inbox/main.mp4'] = 100
    landed = asyncio.run(cloudingest.wait_landing(
        cloud, 'inbox', before, poll=.01, timeout=.2, stable=2))
    assert len(landed) == 1
    assert landed[0].files == [{'name': 'main.mp4', 'size': 100}]


@pytest.mark.parametrize('size', [0, 100])
def test_historical_loose_files_never_get_claimed(size):
    cloud = Cloud({'inbox/old.mp4': size})
    before = cloudingest.snapshot_strict(cloud, 'inbox')
    cloud.files['inbox/new.mp4'] = 200
    assert cloudingest.LOOSE_KEY in before
    assert cloudingest.newcomers(before, cloudingest.measure(cloud, 'inbox')) == []


@pytest.mark.parametrize('source', ['seed', ''])
def test_file_arriving_after_cleanup_listing_is_not_purged(monkeypatch, source):
    prefix = 'inbox/' + source if source else 'inbox'
    cloud = Cloud({prefix + '/main.mp4': 100, prefix + '/ad.txt': 1})
    plan = organize(cloud, source)
    original = cloud.list_files
    injected = False
    def list_then_arrive(path):
        nonlocal injected
        entries = original(path)
        if path == prefix and not injected:
            cloud.files[prefix + '/late.mp4'] = 88
            injected = True
        return entries
    monkeypatch.setattr(cloud, 'list_files', list_then_arrive)
    with pytest.raises(cloudorganize.OrganizeError, match='清理后仍有内容'):
        cloudorganize.cleanup_source(cloud, 'inbox', plan, 'archive')
    assert cloud.deleted == [prefix + '/ad.txt']
    assert cloud.files[prefix + '/late.mp4'] == 88
    assert cloud.files['archive/DEMO-123/DEMO-123.mp4'] == 100


def test_partial_drop_failure_remains_visible(monkeypatch):
    cloud = Cloud({'inbox/seed/main.mp4': 100,
                   'inbox/seed/ad1.txt': 1, 'inbox/seed/ad2.txt': 1})
    plan = organize(cloud)
    real = cloud.purge
    def purge(path):
        if path.endswith('/ad2.txt'):
            raise StorageError('second drop failed')
        return real(path)
    monkeypatch.setattr(cloud, 'purge', purge)
    with pytest.raises(cloudorganize.OrganizeError, match='second drop failed'):
        cloudorganize.cleanup_source(cloud, 'inbox', plan, 'archive')
    assert 'inbox/seed/ad1.txt' not in cloud.files
    assert cloud.files['inbox/seed/ad2.txt'] == 1
    assert cloud.deleted == ['inbox/seed/ad1.txt']
    assert cloud.files['archive/DEMO-123/DEMO-123.mp4'] == 100
