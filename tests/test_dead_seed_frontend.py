"""前端排序必须跟后端 sources.rank 同一口径。

两边各排一次最容易漂移：后端改了「死种垫底」，前端还按老口径排，
用户看到的就是死种排在活种前面 —— 正好是 2026-10-24 误选的原因。

这个测试把两边的口径钉在一起：拿同一组候选，跑后端 rank 和前端
sortCandidates，断言顺序一致。
"""
from __future__ import annotations

import re

from app import sources

# 前端 sortCandidates / cmpCand / splitDead 的实现，从 webui.html 里抠出来跑。
FRONTEND_JS = """
const num = (v) => Number(v) || 0;
const isDead = (it) => "seeders" in it && !num(it.seeders);
const splitDead = (items) => {
  const live = items.filter((x) => !isDead(x));
  const dead = items.filter(isDead);
  return {live, dead};
};
const cmpCand = (a, b) => {
  const sub = (x) => (x.has_subtitle ? 0 : 1);
  const hd = (x) => (x.is_hd ? 0 : 1);
  return sub(a) - sub(b) || hd(a) - hd(b)
         || (num(b.seeders) - num(a.seeders))
         || (num(b.size_bytes) - num(a.size_bytes));
};
function sortCandidates(items, key) {
  const {live, dead} = splitDead(items);
  if (key === "seed") live.sort((a, b) => (num(b.seeders) - num(a.seeders)) || cmpCand(a, b));
  else if (key === "size") live.sort((a, b) => (num(b.size_bytes) - num(a.size_bytes)) || cmpCand(a, b));
  else live.sort(cmpCand);
  return [...live, ...dead];
}
"""


def _frontend_sort(items, key="rec"):
    """用 Node 跑前端实现（没有 Node 就跳过）。"""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        return None
    script = FRONTEND_JS + "\nconsole.log(JSON.stringify(sortCandidates(" \
                                + json.dumps(items, ensure_ascii=False) \
                                + ", " + json.dumps(key) + ")));\n"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         timeout=30)
    if out.returncode != 0:
        return None
    import json as _json
    return _json.loads(out.stdout.strip())


def _candidates():
    return [
        {"code": "A", "magnet": "magnet:?xt=urn:btih:" + "1" * 40,
         "seeders": 5, "has_subtitle": True, "is_hd": True, "size_bytes": 100},
        {"code": "B", "magnet": "magnet:?xt=urn:btih:" + "2" * 40,
         "seeders": 0, "has_subtitle": True, "is_hd": True, "size_bytes": 900},
        {"code": "C", "magnet": "magnet:?xt=urn:btih:" + "3" * 40,
         "seeders": 2, "has_subtitle": False, "is_hd": True, "size_bytes": 50},
        {"code": "D", "magnet": "magnet:?xt=urn:btih:" + "4" * 40,
         "seeders": 0, "has_subtitle": False, "is_hd": False, "size_bytes": 10},
    ]


def test_frontend_and_backend_agree_on_order():
    """同一组候选，前端排出来的顺序要跟后端 rank 排出来的一样。"""
    import json
    import shutil
    import subprocess

    if not shutil.which("node"):
        import pytest
        pytest.skip("没有 node，跳过前端一致性比对")

    items = _candidates()
    backend_order = [i["code"] for i in sorted(items, key=sources.rank)]
    frontend_order = [i["code"] for i in _frontend_sort(items, "rec")]
    assert frontend_order == backend_order, (
        f"前端 {frontend_order} != 后端 {backend_order}")


def test_frontend_puts_dead_seeds_last_in_every_mode():
    """三种排序下，0 种子的都必须在最后。"""
    import shutil

    if not shutil.which("node"):
        import pytest
        pytest.skip("没有 node，跳过前端一致性比对")

    items = _candidates()
    for key in ("rec", "seed", "size"):
        order = [i["code"] for i in _frontend_sort(items, key)]
        dead_positions = [i for i, code in enumerate(order) if code in ("B", "D")]
        assert dead_positions and min(dead_positions) >= 2, (
            f"{key} 排序下死种没垫底：{order}")


def test_backend_rank_also_puts_dead_last():
    """后端同样：0 种子的排最后。"""
    items = _candidates()
    order = [i["code"] for i in sorted(items, key=sources.rank)]
    assert order[-2:] == ["B", "D"] or set(order[-2:]) == {"B", "D"}, order
    assert order[0] == "A", order


def test_webui_has_the_same_dead_seed_rule():
    """webui.html 里的前端实现也要有「死种垫底」的分组逻辑。"""
    from pathlib import Path
    html = Path("app/webui.html").read_text(encoding="utf-8")
    assert "splitDead" in html
    assert "[...live, ...dead]" in html, "死种要拼在活种后面"
    assert "种子 0" in html, "0 种子要显式提示"
