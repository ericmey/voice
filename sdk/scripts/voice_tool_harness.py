#!/usr/bin/env python3
"""Exercise voice-agent OpenClaw tools without placing a phone call.

Default mode is safe: it instantiates the real agent class and runs the
real delegation method, but patches the Gateway hook client so no request
leaves the process. Use `--live-hooks` to submit to the configured
OpenClaw Gateway `/hooks/agent` endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sdk.openclaw_hooks import OpenClawHookAccepted

from tools import sessions as sessions_module

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class HarnessCase:
    name: str
    agent_id: str
    task: str
    deliver_to: str = "room"
    expect_status: str = "accepted"


CASES: tuple[HarnessCase, ...] = (
    HarnessCase(
        name="selfie",
        agent_id="nyla",
        task=(
            "Eric asked for a selfie. Handle this using your normal OpenClaw tools "
            "and delivery behavior."
        ),
    ),
    HarnessCase(
        name="research",
        agent_id="yumi",
        task="Research what changed in LiveKit agents this week and report back normally.",
    ),
    HarnessCase(
        name="ops-check",
        agent_id="rin",
        task="Check whether the LiveKit voice stack is healthy and report back normally.",
    ),
    HarnessCase(
        name="technical-handoff",
        agent_id="aoi",
        task="Review the latest deploy for obvious technical risk and report back normally.",
    ),
)


def _agent_class(agent_name: str) -> type:
    if agent_name == "nyla":
        module = _load_agent_module(
            "voice_harness_nyla_shared",
            REPO_ROOT / "agents" / "nyla" / "src" / "_shared.py",
        )
        return module.NylaAgent
    if agent_name == "aoi":
        module = _load_agent_module(
            "voice_harness_aoi_shared",
            REPO_ROOT / "agents" / "aoi" / "src" / "_shared.py",
        )
        return module.AoiAgent
    if agent_name == "yua":
        module = _load_agent_module(
            "voice_harness_yua_shared",
            REPO_ROOT / "agents" / "yua" / "src" / "_shared.py",
        )
        return module.YuaAgent
    if agent_name == "party":
        module_path = REPO_ROOT / "agents" / "party" / "src"
        sys.path.insert(0, str(module_path))
        try:
            module = _load_agent_module(
                "voice_harness_party_agent",
                REPO_ROOT / "agents" / "party" / "src" / "agent.py",
            )
            return module.PartyAgent
        finally:
            sys.path.remove(str(module_path))
    raise ValueError(f"unknown agent: {agent_name}")


def _load_agent_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tool_visibility(agent_cls: type) -> dict[str, str]:
    names = [
        "openclaw_delegate",
        "sessions_send",
        "sessions_spawn",
        "academy_selfie",
        "academy_send",
    ]
    visibility: dict[str, str] = {}
    for name in names:
        attr = getattr(agent_cls, name, None)
        if attr is None:
            visibility[name] = "absent"
        elif type(attr).__name__ == "FunctionTool":
            visibility[name] = "model-visible"
        elif callable(attr):
            visibility[name] = "helper-only"
        else:
            visibility[name] = type(attr).__name__
    return visibility


async def _run_cases(
    *,
    agent_name: str,
    cases: list[HarnessCase],
    live_hooks: bool,
) -> dict[str, Any]:
    agent_cls = _agent_class(agent_name)
    agent = agent_cls(instructions="voice tool harness", caller_from="+10000000000")

    captured: list[dict[str, Any]] = []
    original_post_agent_hook = sessions_module.post_agent_hook

    async def fake_post_agent_hook(**kwargs: Any) -> OpenClawHookAccepted:
        captured.append(kwargs)
        return OpenClawHookAccepted(run_id=f"mock-run-{len(captured)}")

    if not live_hooks:
        sessions_module.post_agent_hook = fake_post_agent_hook

    results: list[dict[str, Any]] = []
    try:
        for case in cases:
            result = await agent._delegate_to_openclaw(
                agent_id=case.agent_id,
                task=case.task,
                deliver_to=case.deliver_to,
            )
            status = "accepted" if "accepted by OpenClaw" in result else "rejected"
            results.append(
                {
                    "case": asdict(case),
                    "status": status,
                    "result": result,
                }
            )
    finally:
        sessions_module.post_agent_hook = original_post_agent_hook

    return {
        "agent": agent_name,
        "mode": "live-hooks" if live_hooks else "mock",
        "tool_visibility": _tool_visibility(agent_cls),
        "captured_hook_requests": captured,
        "results": results,
    }


def _select_cases(
    name: str, custom_agent_id: str | None, custom_task: str | None
) -> list[HarnessCase]:
    if custom_agent_id or custom_task:
        if not custom_agent_id or not custom_task:
            raise SystemExit("--agent-id and --task must be supplied together")
        return [HarnessCase(name="custom", agent_id=custom_agent_id, task=custom_task)]
    if name == "all":
        return list(CASES)
    for case in CASES:
        if case.name == name:
            return [case]
    valid = ", ".join(["all", *(case.name for case in CASES)])
    raise SystemExit(f"unknown case {name!r}; valid cases: {valid}")


def _print_human(report: dict[str, Any]) -> None:
    print(f"voice tool harness: agent={report['agent']} mode={report['mode']}")
    print("\ntool visibility:")
    for name, visibility in report["tool_visibility"].items():
        print(f"  {name:18} {visibility}")

    print("\nresults:")
    captured = report["captured_hook_requests"]
    for idx, result in enumerate(report["results"], 1):
        case = result["case"]
        print(f"  {idx}. {case['name']}: {result['status']}")
        print(f"     target={case['agent_id']} deliver_to={case['deliver_to']}")
        print(f"     {result['result']}")
        if captured:
            hook = captured[idx - 1]
            print(
                "     hook="
                f"agentId={hook.get('agent_id')} "
                f"channel={hook.get('channel', '<default>')} "
                f"to={hook.get('to', '<default>')} "
                f"timeout={hook.get('timeout_seconds')}"
            )
    if report["mode"] == "mock":
        print("\nNo OpenClaw request was sent. Re-run with --live-hooks to hit Gateway.")


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=["nyla", "aoi", "yua", "party"], default="nyla")
    parser.add_argument(
        "--case",
        default="all",
        help="Case to run: all, selfie, research, ops-check, technical-handoff",
    )
    parser.add_argument("--agent-id", help="Custom OpenClaw target agent id")
    parser.add_argument("--task", help="Custom task text for --agent-id")
    parser.add_argument(
        "--live-hooks",
        action="store_true",
        help="Submit to the real OpenClaw Gateway hook endpoint",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    cases = _select_cases(args.case, args.agent_id, args.task)
    report = await _run_cases(agent_name=args.agent, cases=cases, live_hooks=args.live_hooks)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)


if __name__ == "__main__":
    asyncio.run(_main())
