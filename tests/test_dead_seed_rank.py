"""死种（0 个做种）要排在活种后面，并且**要显示出来**。

起因（2026-10-24 实测，MIMK-267）：迅雷云盘解析 0 种子的种子会回
    phase = PHASE_TYPE_ERROR, message = "解析失败，请重试或更换链接"
而候选列表里 0 种子的那几条**什么都不显示**（`if (seed > 0)` 才渲染），
用户看不出哪条能下 —— 选了 0 种子的那条，任务干等两小时。

两处要改：
  · 排序：0 种子的排到最后（不管有没有字幕/高清/多大）
  · 展示：0 种子的明确标出来
"""
from __future__ import annotations

from app import sources
from app import subscribe


def _item(h, *, sub=False, hd=False, seeders=0, size=1000, source="javbus"):
    d = {"magnet": f"magnet:?xt=urn:btih:{h}", "title": h,
         "size_bytes": size, "size_text": f"{size}B", "source": source}
    if sub:
        d["has_subtitle"] = True
    if hd:
        d["is_hd"] = True
    if seeders is not None:
        d["seeders"] = seeders
    # seeders=None 表示「这个源不给做种数」—— 键整个不存在
    return d


def test_zero_seed_sorts_after_live_seed():
    """0 种子的排后面 —— 哪怕它带字幕、高清、体积更大。"""
    dead = _item("d" * 40, sub=True, hd=True, seeders=0, size=9_000_000_000)
    live = _item("l" * 40, seeders=3, size=1_000)
    assert sources.rank(dead) > sources.rank(live)


def test_missing_seeders_is_unknown_not_dead():
    """字段缺失 = 不知道（javbus/bt4g 不给做种数），不是「没人做种」。

    不知道的不能被当成死种 —— 那会让所有 javbus 候选都排到最后。
    """
    unknown = _item("u" * 40, seeders=None, sub=True, hd=True)
    dead = _item("d" * 40, seeders=0, sub=True, hd=True)
    assert sources.rank(unknown) < sources.rank(dead)


def test_live_seed_keeps_subtitle_priority():
    """同为活种时，字幕仍然优先（内容需求不变）。"""
    sub = _item("s" * 40, sub=True, hd=True, seeders=10)
    plain = _item("p" * 40, seeders=20)
    assert sources.rank(sub) < sources.rank(plain)


def test_rank_never_raises_on_missing_keys():
    """缺键当「没有」，不能抛 KeyError（这条排序用在两种形状的数据上）。"""
    assert sources.rank({"magnet": "magnet:?x"})
    assert sources.rank({})


def test_pick_candidate_prefers_live_seed():
    """订阅自动挑候选时也要避开死种。"""
    dead = _item("d" * 40, sub=True, hd=True, seeders=0, size=9_000_000_000)
    live = _item("l" * 40, seeders=3, size=1_000)
    got = subscribe.pick_candidate([dead, live])
    assert got and got["magnet"].endswith("l" * 40)


def test_pick_candidate_handles_unknown_seeders():
    """不知道做种数的候选不能被当成死种。"""
    unknown = _item("u" * 40, seeders=None, sub=True)
    dead = _item("d" * 40, seeders=0)
    got = subscribe.pick_candidate([dead, unknown])
    assert got and got["magnet"].endswith("u" * 40)
