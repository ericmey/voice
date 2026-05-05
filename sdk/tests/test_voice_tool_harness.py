"""Tests for the no-phone voice tool harness."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "voice_tool_harness.py"
_SPEC = importlib.util.spec_from_file_location("voice_tool_harness", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
voice_tool_harness: Any = importlib.util.module_from_spec(_SPEC)
sys.modules["voice_tool_harness"] = voice_tool_harness
_SPEC.loader.exec_module(voice_tool_harness)

HarnessCase = voice_tool_harness.HarnessCase
_run_cases = voice_tool_harness._run_cases
_select_cases = voice_tool_harness._select_cases


def test_select_custom_case_requires_agent_and_task():
    with pytest.raises(SystemExit):
        _select_cases("all", "nyla", None)


@pytest.mark.asyncio
async def test_harness_mock_mode_uses_real_agent_tool_shape():
    report = await _run_cases(
        agent_name="nyla",
        cases=[HarnessCase(name="custom", agent_id="yumi", task="Research LiveKit")],
        live_hooks=False,
    )

    assert report["tool_visibility"]["openclaw_delegate"] == "model-visible"
    assert report["tool_visibility"]["sessions_send"] == "helper-only"
    assert report["tool_visibility"]["academy_selfie"] == "absent"
    assert report["results"][0]["status"] == "accepted"
    assert report["captured_hook_requests"][0]["agent_id"] == "yumi"
    assert report["captured_hook_requests"][0]["channel"] == "discord"


@pytest.mark.asyncio
async def test_harness_respects_agent_delegation_allowlist():
    report = await _run_cases(
        agent_name="aoi",
        cases=[HarnessCase(name="custom", agent_id="hana", task="Draw something")],
        live_hooks=False,
    )

    assert report["results"][0]["status"] == "rejected"
    assert report["captured_hook_requests"] == []
    assert "don't route to hana" in report["results"][0]["result"]
