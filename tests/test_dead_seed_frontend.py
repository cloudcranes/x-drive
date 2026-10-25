"""Execute the current UI sorter, and compare its three-state contract with sources.rank."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import sources
from test_frontend_state import run_js

PAGE = Path(__file__).resolve().parents[1] / "app" / "webui.html"


def _frontend_sort(items, key="rec"):
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过前端一致性比对")
    page = PAGE.read_text(encoding="utf-8")
    # Read the actual implementation, never keep a second hand-copied sorter here.
    start = page.index("const RES_SORTS =")
    end = page.index("async function searchResources(", start)
    script = "const resState={sort:" + json.dumps(key) + "};\n" + page[start:end]
    script += "\nconsole.log(JSON.stringify(sortCandidates(" + json.dumps(items) + ")));"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _candidates():
    return [
        {"code": "A", "seeders": 5, "has_subtitle": True, "is_hd": True, "size_bytes": 100},
        {"code": "B", "seeders": 0, "has_subtitle": True, "is_hd": True, "size_bytes": 900},
        {"code": "C", "seeders": 2, "has_subtitle": False, "is_hd": True, "size_bytes": 50},
        {"code": "D", "seeders": 0, "has_subtitle": False, "is_hd": False, "size_bytes": 10},
        {"code": "missing", "has_subtitle": True, "size_bytes": 40},
        {"code": "null", "seeders": None, "has_subtitle": True, "size_bytes": 30},
        {"code": "negative", "seeders": -2, "size_bytes": 80},
        {"code": "false", "seeders": False, "size_bytes": 70},
        {"code": "empty", "seeders": "", "size_bytes": 60},
        {"code": "float", "seeders": 1.5, "size_bytes": 50},
        {"code": "bad", "seeders": "NaN", "size_bytes": 45},
        {"code": "string-zero", "seeders": "0", "size_bytes": 60},
        {"code": "string-count", "seeders": " 6 ", "size_bytes": 40},
    ]


def test_frontend_and_backend_agree_on_order():
    items = list(reversed(_candidates()))  # Also exercise sorting inside the dead group.
    backend = [i["code"] for i in sorted(items, key=sources.rank)]
    frontend = [i["code"] for i in _frontend_sort(items, "rec")]
    assert frontend == backend


@pytest.mark.parametrize("mode", ["rec", "seed", "size"])
def test_frontend_puts_only_known_dead_seeds_last_in_every_mode(mode):
    items = _candidates()
    order = [i["code"] for i in _frontend_sort(items, mode)]
    assert set(order[-3:]) == {"B", "D", "string-zero"}
    assert "null" in order[:-3]
    assert "missing" in order[:-3]


def test_unknown_count_keeps_subtitle_preference_in_recommended_order():
    items = [{"code": "known", "seeders": 9},
             {"code": "unknown", "seeders": None, "has_subtitle": True}]
    assert [i["code"] for i in _frontend_sort(items)] == ["unknown", "known"]


def test_unknown_candidate_display_is_not_a_dead_warning():
    run_js(r'''
      const items=[{code:"missing"},{code:"null",seeders:null},{code:"zero",seeders:0},
        {code:"string-zero",seeders:"0"},{code:"live",seeders:8}];
      renderCandidates(items,"fixture",{});
      const rows=get("resource-slot").querySelector(".res-list").children;
      assert.equal(rows.filter(r=>r.textContent.includes("做种数未知")).length,2);
      assert.equal(rows.filter(r=>r.classList.contains("dead")).length,2);
      assert.equal(rows.filter(r=>r.textContent.includes("种子 8")).length,1);
      for(const row of rows.filter(r=>r.textContent.includes("做种数未知")))
        assert.equal(row.classList.contains("dead"),false);
    ''')
