"""Regression tests for scripts/agent-entrypoint.sh.

Every production env bug from the launchd -> compose move lived in this script's
absence: MUSUBI_V2_TOKEN was never mapped, LIVEKIT_VOICE_LOGS was never set,
VOICE_AGENT_NAME was never exported. All three were silent — the agents started,
registered, answered calls, and quietly lost memory and telemetry.

The script now owns that mapping, so it gets a test. `exec` is stubbed by
replacing the final line, so nothing actually launches an agent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "scripts" / "agent-entrypoint.sh"

# Exit codes the script uses. 64 = unknown agent (EX_USAGE), 78 = bad config (EX_CONFIG).
EX_USAGE = 64
EX_CONFIG = 78

TOKENS = {
    "MUSUBI_V2_TOKEN_AOI": "tok-aoi",
    "MUSUBI_V2_TOKEN_NYLA": "tok-nyla",
    "MUSUBI_V2_TOKEN_YUA": "tok-yua",
}


def _run(agent: str | None, env_overrides: dict[str, str] | None = None):
    """Run the entrypoint with `exec` neutered, echoing the resolved env instead."""
    body = ENTRYPOINT.read_text()
    exec_line = 'exec uv run python "agents/${AGENT}/src/agent.py" start'
    assert exec_line in body, "entrypoint exec line changed; this stub is stale"
    body = body.replace(
        exec_line,
        'echo "MUSUBI_V2_TOKEN=$MUSUBI_V2_TOKEN"\n'
        'echo "LIVEKIT_VOICE_LOGS=$LIVEKIT_VOICE_LOGS"\n'
        'echo "VOICE_AGENT_NAME=$VOICE_AGENT_NAME"',
    )
    env: dict[str, str] = {"PATH": "/usr/bin:/bin"}
    if agent is not None:
        env["AGENT"] = agent
    env.update(TOKENS)
    if env_overrides:
        env.update({k: v for k, v in env_overrides.items() if v is not None})
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
    return subprocess.run(
        ["/bin/sh", "-c", body], env=env, capture_output=True, text=True, check=False
    )


def _parsed(result) -> dict[str, str]:
    return dict(line.split("=", 1) for line in result.stdout.strip().splitlines() if "=" in line)


@pytest.mark.parametrize(
    ("agent", "expected_token"),
    [
        ("aoi", "tok-aoi"),
        ("yua", "tok-yua"),
        ("nyla", "tok-nyla"),
        # party mirrors nyla's AgentConfig namespace, so it shares her bearer.
        ("party", "tok-nyla"),
    ],
)
def test_resolves_the_per_agent_musubi_bearer(agent, expected_token):
    result = _run(agent)
    assert result.returncode == 0, result.stderr
    assert _parsed(result)["MUSUBI_V2_TOKEN"] == expected_token


@pytest.mark.parametrize("agent", ["aoi", "nyla", "yua", "party"])
def test_exports_voice_agent_name_so_service_name_is_per_agent(agent):
    """Unset, tracing.py collapses all four agents into service_name=voice and
    every `voice-.*` dashboard panel and alert selector matches nothing."""
    result = _run(agent)
    assert result.returncode == 0, result.stderr
    assert _parsed(result)["VOICE_AGENT_NAME"] == agent


def test_defaults_livekit_voice_logs():
    """Unset, transcripts / trace / telemetry / post-call review / post-call
    memory each silently no-op."""
    result = _run("aoi")
    assert _parsed(result)["LIVEKIT_VOICE_LOGS"] == "/app/logs/voice"


def test_respects_an_explicit_livekit_voice_logs():
    result = _run("aoi", {"LIVEKIT_VOICE_LOGS": "/custom/logs"})
    assert _parsed(result)["LIVEKIT_VOICE_LOGS"] == "/custom/logs"


def test_refuses_to_start_on_an_empty_bearer():
    """A degraded agent sounds healthy while losing every memory. Crash instead."""
    result = _run("aoi", {"MUSUBI_V2_TOKEN_AOI": ""})
    assert result.returncode == EX_CONFIG
    assert "MUSUBI_V2_TOKEN_AOI is empty" in result.stderr


def test_empty_bearer_error_names_the_variable_actually_required():
    """party needs NYLA's bearer. Naming MUSUBI_V2_TOKEN_PARTY would send an
    operator hunting for a variable that does not exist."""
    result = _run("party", {"MUSUBI_V2_TOKEN_NYLA": ""})
    assert result.returncode == EX_CONFIG
    assert "MUSUBI_V2_TOKEN_NYLA is empty" in result.stderr
    assert "MUSUBI_V2_TOKEN_PARTY" not in result.stderr


def test_unknown_agent_is_rejected():
    result = _run("mizuki")
    assert result.returncode == EX_USAGE
    assert "no Musubi token mapping" in result.stderr


def test_missing_agent_is_rejected():
    result = _run(None)
    assert result.returncode != 0
    assert "AGENT" in result.stderr
