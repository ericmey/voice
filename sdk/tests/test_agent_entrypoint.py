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
    "MUSUBI_V2_TOKEN_SUMI": "tok-sumi",
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
        # sumi's voice line carries its own bearer — memory is sumi/voice.
        ("sumi", "tok-sumi"),
    ],
)
def test_resolves_the_per_agent_musubi_bearer(agent, expected_token):
    result = _run(agent)
    assert result.returncode == 0, result.stderr
    assert _parsed(result)["MUSUBI_V2_TOKEN"] == expected_token


@pytest.mark.parametrize("agent", ["aoi", "nyla", "yua", "sumi"])
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
    """sumi needs its own MUSUBI_V2_TOKEN_SUMI. The error must name the
    variable the operator actually has to set."""
    result = _run("sumi", {"MUSUBI_V2_TOKEN_SUMI": ""})
    assert result.returncode == EX_CONFIG
    assert "MUSUBI_V2_TOKEN_SUMI is empty" in result.stderr


def test_unknown_agent_is_rejected():
    result = _run("mizuki")
    assert result.returncode == EX_USAGE
    assert "no Musubi token mapping" in result.stderr


def test_missing_agent_is_rejected():
    result = _run(None)
    assert result.returncode != 0
    assert "AGENT" in result.stderr


# ---------------------------------------------------------------------------
# The image must not pre-answer the identity question.
#
# `scripts/agent-entrypoint.sh` is built to fail loud on a missing AGENT: it has a
# `${AGENT:?}` guard AND a `*)` fallthrough that exits 64. Both are correct.
#
# `Dockerfile.agent` then set `ENV AGENT=aoi`, which DISARMS BOTH. AGENT is always
# "set", so the guard can never fire — and a container started without an explicit
# AGENT does not fail: it silently BECOMES AOI. It registers as `phone-aoi`, reads
# MUSUBI_V2_TOKEN_AOI, and writes its memories to `aoi/voice`.
#
# This is `NYLA_DEFAULT_CONFIG` reincarnated one layer down — the exact bug the whole
# config module exists to prevent (see sdk/src/sdk/config.py). And `assert_agent_identity`
# cannot catch it either: VOICE_AGENT_NAME is derived from the same $AGENT, so the
# cross-check compares the default against itself and passes.
#
# It was dormant only because all four compose services happen to pass AGENT. Dormant is
# not fixed. An identity default is never a convenience; it is a silent misattribution
# waiting for the one container someone starts by hand.
# ---------------------------------------------------------------------------


def test_dockerfile_does_not_default_the_agent_identity() -> None:
    """The image must NOT bake in an AGENT default — that disarms the entrypoint guard."""
    dockerfile = (REPO_ROOT / "Dockerfile.agent").read_text()
    offending = [
        line
        for line in dockerfile.splitlines()
        if line.strip().startswith("ENV ") and "AGENT=" in line and "VOICE_AGENT" not in line
    ]
    assert not offending, (
        "Dockerfile.agent bakes an AGENT default: "
        f"{offending!r}. This disarms the `${{AGENT:?}}` guard in agent-entrypoint.sh — a "
        "container started without AGENT would silently become that agent, register under "
        "its name, and write to its Musubi namespace. Identity must never have a default."
    )


def test_missing_agent_fails_loud() -> None:
    """With no AGENT in the environment, the entrypoint must REFUSE — not pick someone.

    This is the guard the Dockerfile default was disarming. `_run(None)` omits AGENT
    entirely, which is what a container started without `-e AGENT=...` actually sees once
    the image stops answering the question for it.
    """
    result = _run(None)
    assert result.returncode != 0, (
        "entrypoint started with no AGENT set. It must fail rather than default to an "
        "identity — a silent default here means registering under someone else's name and "
        "writing to their Musubi namespace."
    )
