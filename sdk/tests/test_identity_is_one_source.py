"""Every identity axis derives from ONE root. This test closes the last unverified one.

The original bug this whole config module exists to kill was a THREE-HEADED identity model:

  1. ``config.agent_name``            — the intended source of truth
  2. hand-typed ``"phone-nyla"`` literals scattered through the code
  3. the ``$AGENT`` environment variable

None of them checked the others, and a ``NYLA_DEFAULT_CONFIG`` fallback meant a
misconfigured agent silently BECAME Nyla — registering under her name and writing into her
memory namespace.

The 2026-07-10 pass collapsed heads 1 and 3: ``AgentConfig.agent_name`` is the root,
``registration_name`` derives from it, and ``assert_agent_identity`` proves
``$AGENT == config.agent_name`` at startup, failing loud.

**Head 2 was never fully closed.** The ``agentName`` in each ``config/sip-dispatch-*.json``
is still a hand-typed ``"phone-<x>"`` literal, and NOTHING checks it against
``registration_name``. That literal is what LiveKit's SIP dispatch actually routes on — so
a typo there does not crash anything. It routes an inbound call to a worker name nobody is
registered under, and the phone simply rings into silence. Or worse: it routes to the wrong
agent, and Eric gets Yua when he dialled Nyla.

This is the missing half of the fix. It is a test rather than a runtime assert because the
dispatch JSON is deploy-time config, not runtime state — the right place to catch it is
before it ships, not after a call fails.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DISPATCH_DIR = REPO_ROOT / "config"

# The four agents and the registration name each MUST route on. Derived from the one root:
# AgentConfig.registration_name == f"phone-{agent_name}".
AGENTS = ("aoi", "nyla", "sumi", "yua")


def _registration_name(agent: str) -> str:
    """The single source of truth, re-derived here exactly as AgentConfig derives it."""
    return f"phone-{agent}"


def _dispatch_files() -> dict[str, Path]:
    """Map agent -> its SIP dispatch file. Accepts `.json` (live) or `.json.example`."""
    found: dict[str, Path] = {}
    for agent in AGENTS:
        for candidate in (
            DISPATCH_DIR / f"sip-dispatch-{agent}.json",
            DISPATCH_DIR / f"sip-dispatch-{agent}.json.example",
        ):
            if candidate.exists():
                found[agent] = candidate
                break
    return found


def test_every_agent_has_a_sip_dispatch_file() -> None:
    """An agent with no dispatch rule cannot be reached by phone at all."""
    found = _dispatch_files()
    missing = sorted(set(AGENTS) - set(found))
    assert not missing, (
        f"no config/sip-dispatch-<agent>.json[.example] for: {missing}. "
        "Without a dispatch rule an inbound call has nowhere to route — the agent is "
        "unreachable by phone and nothing errors."
    )


@pytest.mark.parametrize("agent", AGENTS)
def test_sip_dispatch_agent_name_matches_the_derived_registration_name(agent: str) -> None:
    """The dispatch literal must equal `phone-<agent_name>` — the derived registration name.

    This is the cross-check that was missing. LiveKit routes the inbound call on this
    string; if it disagrees with what the worker actually registered as, the call routes
    into silence (or to the wrong sister) and nothing raises.
    """
    files = _dispatch_files()
    if agent not in files:
        pytest.skip(f"no dispatch file for {agent} (covered by the test above)")

    path = files[agent]
    doc = json.loads(path.read_text())

    # The agentName lives under the dispatch rule; accept either shape rather than
    # over-fitting to today's schema.
    raw = json.dumps(doc)
    names = re.findall(r'"agentName"\s*:\s*"([^"]+)"', raw)

    assert names, f"{path.name} declares no agentName — LiveKit has nothing to route on"

    expected = _registration_name(agent)
    wrong = [n for n in names if n != expected]
    assert not wrong, (
        f"{path.name} routes to agentName={wrong!r}, but {agent}'s worker registers as "
        f"{expected!r} (AgentConfig.registration_name, derived from agent_name).\n"
        f"A hand-typed dispatch literal that disagrees with the registration name does not "
        f"crash — the call routes to a worker nobody is registered under and the phone rings "
        f"into silence, or it routes to the WRONG SISTER. This literal is the third head of "
        f"the identity model; it must derive from the same root as everything else."
    )


def test_no_dispatch_file_routes_to_an_unknown_agent() -> None:
    """The reverse direction: every dispatch literal must belong to a real agent.

    Catches a leftover rule for a retired agent (e.g. the old `phone-party`) that would
    silently claim inbound calls for a worker that no longer exists.
    """
    valid = {_registration_name(a) for a in AGENTS}
    offenders: list[str] = []
    for path in sorted(DISPATCH_DIR.glob("sip-dispatch-*.json*")):
        for name in re.findall(r'"agentName"\s*:\s*"([^"]+)"', path.read_text()):
            if name not in valid:
                offenders.append(f"{path.name} -> {name!r}")
    assert not offenders, (
        f"dispatch rules route to agents that do not exist: {offenders}. "
        f"Valid registration names are {sorted(valid)}. A stale rule silently claims inbound "
        f"calls for a worker nobody runs."
    )


# ---------------------------------------------------------------------------
# The provisioning template must not fall behind the agents.
#
# `config/livekit.env.tpl` renders `secrets/livekit-agents.env` from 1Password. When Sumi
# was added, her token was hand-patched into the LIVE env file and never added to the
# TEMPLATE. That is not a cosmetic gap:
#
#   - re-render from the template and agent-sumi crash-loops (exit 78, missing bearer);
#   - the likely operator "fix" under pressure is pasting a sibling's bearer — which
#     silently writes her memories into someone else's Musubi namespace.
#
# Misattribution by a different road, and no error anywhere. This test is the class fix:
# every agent that exists must have a bearer line in the template, forever.
# ---------------------------------------------------------------------------

TEMPLATE = REPO_ROOT / "config" / "livekit.env.tpl"


def test_every_agent_has_a_musubi_bearer_in_the_provisioning_template() -> None:
    """An agent the template forgets is an agent that crash-loops on the next re-render."""
    if not TEMPLATE.exists():
        pytest.skip("no provisioning template in this checkout")

    body = TEMPLATE.read_text()
    declared = set(re.findall(r"^MUSUBI_V2_TOKEN_([A-Z]+)=", body, re.MULTILINE))
    expected = {a.upper() for a in AGENTS}

    missing = sorted(expected - declared)
    assert not missing, (
        f"config/livekit.env.tpl has no MUSUBI_V2_TOKEN_ line for: {missing}. "
        f"Re-rendering the secrets file would leave that agent with no bearer — it "
        f"crash-loops (exit 78), and the tempting fix is to paste a sibling's token, which "
        f"writes her memories into the wrong namespace."
    )

    stale = sorted(declared - expected)
    assert not stale, (
        f"config/livekit.env.tpl provisions bearers for agents that do not exist: {stale}. "
        f"A retired agent's token should not keep being handed to the fleet."
    )


def test_the_entrypoint_maps_a_bearer_for_every_agent() -> None:
    """The third place identity is written down: the entrypoint's token `case` statement.

    Template, entrypoint, and agent set must agree. Any one of them falling behind is a
    silent misattribution or a crash-loop.
    """
    body = (REPO_ROOT / "scripts" / "agent-entrypoint.sh").read_text()
    mapped = set(re.findall(r"^\s*(\w+)\)\s*token_var=MUSUBI_V2_TOKEN_", body, re.MULTILINE))
    missing = sorted(set(AGENTS) - mapped)
    assert not missing, (
        f"scripts/agent-entrypoint.sh has no Musubi bearer mapping for: {missing}. "
        f"That agent exits 64 at container start."
    )


# ---------------------------------------------------------------------------
# The health port is declared twice. Pin the two together.
#
# `AgentServer(port=8081)` in agents/nyla/src/agent.py, and `AGENT_PORT: "8081"` in
# docker-compose.agents.yaml (which the container healthcheck probes).
#
# Two declarations of one fact is exactly the shape that produced every identity bug in this
# codebase. If they drift, the healthcheck probes a port nobody is listening on — and the
# container is marked UNHEALTHY and restarted, forever, while the agent inside is perfectly
# fine. A monitoring bug that manufactures the outage it is watching for.
# ---------------------------------------------------------------------------

COMPOSE = REPO_ROOT / "docker-compose.agents.yaml"


@pytest.mark.parametrize("agent", AGENTS)
def test_the_healthcheck_port_matches_the_port_the_agent_listens_on(agent: str) -> None:
    """AGENT_PORT in compose must equal AgentServer(port=...) in the agent's source."""
    src = (REPO_ROOT / "agents" / agent / "src" / "agent.py").read_text()
    m = re.search(r"AgentServer\(\s*port\s*=\s*(\d+)", src)
    assert m, f"{agent} does not call AgentServer(port=...)"
    listens_on = m.group(1)

    compose = COMPOSE.read_text()
    block = re.search(rf"AGENT:\s*{agent}\b(?:.*?\n)*?\s*AGENT_PORT:\s*\"(\d+)\"", compose)
    assert block, f"docker-compose.agents.yaml declares no AGENT_PORT for {agent}"
    probed = block.group(1)

    assert probed == listens_on, (
        f"{agent} listens on {listens_on} but the container healthcheck probes {probed}. "
        f"The healthcheck would fail forever against a perfectly healthy agent, and docker "
        f"would restart her on a loop — a monitoring bug that manufactures the outage it is "
        f"watching for."
    )


def test_no_two_agents_share_a_health_port() -> None:
    """A port collision would make one agent's healthcheck pass on her sister's server."""
    ports: dict[str, str] = {}
    for agent in AGENTS:
        src = (REPO_ROOT / "agents" / agent / "src" / "agent.py").read_text()
        m = re.search(r"AgentServer\(\s*port\s*=\s*(\d+)", src)
        assert m
        ports[agent] = m.group(1)

    seen: dict[str, str] = {}
    for agent, port in ports.items():
        assert port not in seen, (
            f"{agent} and {seen[port]} both listen on {port} — one agent's healthcheck would "
            f"be answered by the other's server."
        )
        seen[port] = agent
