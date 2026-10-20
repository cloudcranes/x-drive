"""性能基线：把「感觉慢」变成可复现的数字。

只测纯本地、无网络的路径，这样在任何机器上结果可比：
  · 状态读写（每次归档都写一次 state.json）
  · 用量统计（每个任务读写一次 usage.json）
  · 番号识别（每条消息都跑）
  · 目录整理（每个任务跑一次，含 rglob）

用 `python tests/bench.py` 直接跑，输出各阶段耗时。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import codes, core, fetcher, pipeline  # noqa: E402


def timeit(label: str, fn, repeat: int = 20) -> float:
    fn()                                    # 预热
    start = time.perf_counter()
    for _ in range(repeat):
        fn()
    per = (time.perf_counter() - start) / repeat
    print(f"{label:<44} {per * 1000:8.2f} ms")
    return per


def bench_state(n_entries: int) -> None:
    d = Path(tempfile.mkdtemp())
    path = str(d / "state.json")
    state = {
        f"SSIS-{i:04d}": {
            "code": f"SSIS-{i:04d}",
            "path": f"gdrive:X-Drive/SSIS-{i:04d}",
            "link": f"https://drive.google.com/drive/folders/{i}",
            "files": [f"SSIS-{i:04d}.mp4"],
            "size": 2 * 1024**3,
            "source": "magnet:?xt=urn:btih:" + "a" * 40,
            "at": "2026-10-20T12:00:00",
        }
        for i in range(n_entries)
    }
    print(f"\n== state.json（{n_entries} 条）")
    timeit("load_state", lambda: core.load_state(path if Path(path).exists() else path))
    core.save_state(path, state)
    timeit("load_state（已有数据）", lambda: core.load_state(path))
    timeit("save_state", lambda: core.save_state(path, state))
    print(f"   文件大小 {Path(path).stat().st_size / 1024:.1f} KB")


def bench_codes() -> None:
    print("\n== 番号识别")
    samples = [
        "magnet:?xt=urn:btih:5AFF2AE18DA78EB04665DB379A3D9D8E2D382102&dn=mimk-288ch",
        "ssis-123 1080p x264 WEB-DL",
        "FC2-PPV-4422110 magnet:?xt=urn:btih:aa",
        "https://example.com/HD/SSIS-045-C.mp4",
    ]
    for s in samples:
        timeit(f"detect({s[:26]}…)", lambda s=s: codes.detect(s), repeat=200)


def bench_organize(n_files: int) -> None:
    print(f"\n== 目录整理（{n_files} 个文件）")
    root = Path(tempfile.mkdtemp())

    def make() -> Path:
        work = root / f"job{n_files}"
        pipeline.cleanup(work)
        sub = work / "mimk-288ch" / "extra"
        sub.mkdir(parents=True)
        for i in range(n_files):
            (sub / f"file{i}.mp4").write_bytes(b"x" * 2048)
        for i in range(3):
            (sub / f"ad{i}.url").write_text("ad")
        return work

    work = make()
    timeit("media_files（首次）", lambda: fetcher.media_files(work), repeat=20)


def bench_json_roundtrip() -> None:
    """并发写同一份 state 是潜在的数据覆盖点，先量出单次写的代价。"""
    print("\n== JSON 往返（1000 条）")
    data = {f"K{i}": {"files": [f"f{i}.mp4"], "size": i} for i in range(1000)}
    d = Path(tempfile.mkdtemp())
    p = d / "s.json"
    timeit("json.dump indent=1", lambda: p.write_text(json.dumps(data, ensure_ascii=False, indent=1)))
    timeit("json.dump 紧凑", lambda: p.write_text(json.dumps(data, ensure_ascii=False)))
    text = p.read_text()
    timeit("json.loads", lambda: json.loads(text))


if __name__ == "__main__":
    bench_state(50)
    bench_state(500)
    bench_codes()
    for n in (10, 100, 400):
        bench_organize(n)
    bench_json_roundtrip()
