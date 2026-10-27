# -*- coding: utf-8 -*-
"""运行时验收脚本必须把缺失证据和失败状态反映到退出码。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.smoke_e2e import validate_smoke_result
from scripts.verify_parallel import _dispatch_intervals, parallel_verdict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_smoke_result_requires_final_text_and_complete_tool_trace():
    body = {"final_text": "推荐结果"}
    events = [
        {"type": "tool.invoke", "payload": {"tool": "product_search_tool"}},
        {"type": "tool.result", "payload": {"tool": "product_search_tool"}},
        {"type": "final.result", "payload": {}},
    ]

    validate_smoke_result(body, events)

    with pytest.raises(AssertionError, match="tool.result"):
        validate_smoke_result(body, [events[0], events[2]])


def test_parallel_verdict_requires_multiple_dispatches_with_overlap():
    assert parallel_verdict({"dispatch_count": 3, "overlaps": 2}) is True
    assert parallel_verdict({"dispatch_count": 1, "overlaps": 0}) is False
    assert parallel_verdict({"dispatch_count": 3, "overlaps": 0}) is False


def test_parallel_interval_parser_ignores_incomplete_auxiliary_events():
    events = [
        {"type": "tool.result", "payload": {"tool": "task_dispatch", "error": "辅助错误"}},
        {
            "type": "tool.result",
            "payload": {
                "tool": "task_dispatch",
                "agent": "search_agent",
                "started_at": "2026-08-30T00:00:00+00:00",
                "finished_at": "2026-08-30T00:00:01+00:00",
            },
        },
    ]

    assert _dispatch_intervals(events) == [
        ("search_agent", "2026-08-30T00:00:00+00:00", "2026-08-30T00:00:01+00:00"),
    ]


def test_parallel_help_does_not_start_network_validation():
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "verify_parallel.py"), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "真并行" in result.stdout
    assert "并行版" not in result.stdout
