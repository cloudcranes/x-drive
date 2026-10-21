"""aria2 调优脚本的行为测试：幂等、可重复执行、不产生重复键。"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "tune-aria2.sh"


def run_tune(conf: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(conf)],
        capture_output=True,
        text=True,
        check=True,
    )


def test_disables_force_encryption(tmp_path):
    """实测：关掉强制加密后连接数 49→88、稳态 40.96→61.96 MB/s，所以要关。"""
    conf = tmp_path / "aria2.conf"
    conf.write_text("dir=/downloads\nbt-force-encryption=true\n")
    run_tune(conf)
    body = conf.read_text()
    assert "bt-force-encryption=false" in body
    assert "bt-force-encryption=true" not in body
    assert "bt-require-crypto=false" in body
    assert "bt-min-crypto-level=plain" in body


def test_idempotent(tmp_path):
    """重复部署时会反复执行，不能堆出重复键。"""
    conf = tmp_path / "aria2.conf"
    conf.write_text("dir=/downloads\n")
    run_tune(conf)
    first = conf.read_text()
    run_tune(conf)
    run_tune(conf)
    assert conf.read_text() == first
    assert first.count("bt-force-encryption=") == 1


def test_replaces_commented_key(tmp_path):
    """默认配置里这行可能是注释掉的，要就地改掉而不是追加第二份。"""
    conf = tmp_path / "aria2.conf"
    conf.write_text("dir=/downloads\n#bt-require-crypto=true\n")
    run_tune(conf)
    body = conf.read_text()
    assert "bt-require-crypto=false" in body
    assert body.count("bt-require-crypto=") == 1


def test_keeps_other_settings(tmp_path):
    """别把用户其它配置改坏。"""
    conf = tmp_path / "aria2.conf"
    original = "dir=/downloads\nmax-concurrent-downloads=5\nseed-ratio=1.0\n"
    conf.write_text(original)
    run_tune(conf)
    body = conf.read_text()
    assert "max-concurrent-downloads=5" in body, "实测无收益的参数不该被动"
    assert "seed-ratio=1.0" in body
    assert "dir=/downloads" in body


def test_missing_conf_fails(tmp_path):
    """没配置文件要明确报错，而不是静默生成半套配置。"""
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(
            ["bash", str(SCRIPT), str(tmp_path / "nope.conf")],
            capture_output=True,
            text=True,
            check=True,
        )
