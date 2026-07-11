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
import shutil
from pathlib import Path

import pytest
from sdk.config import AgentConfig
from sdk.sip_preflight import (
    AGENTS,
    SipPreflightError,
    registration_name,
    validate_dispatch_set,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DISPATCH_DIR = REPO_ROOT / "config"
REGISTRAR = REPO_ROOT / "scripts" / "register-sip-routing.sh"


def test_the_registration_name_has_exactly_one_spelling() -> None:
    """``sip_preflight.registration_name`` and ``AgentConfig.registration_name`` are one law.

    The validator derives ``phone-<agent>`` itself rather than constructing an AgentConfig
    per agent. That is a second spelling of the same rule, and two spellings drift. This
    pins them together, so if anyone ever changes the registration format, the SIP contract
    cannot silently keep validating against the old one.
    """
    for agent in AGENTS:
        config = AgentConfig(agent_name=agent, memory_agent_tag=f"{agent}-voice")
        assert registration_name(agent) == config.registration_name


def test_the_tracked_dispatch_set_is_valid() -> None:
    """The examples are the tracked contract; they must pass the real validator.

    In a clean checkout only ``*.json.example`` exists (``config/sip-*.json`` is gitignored),
    so this is all CI can see. That was the WHOLE hole in the old tests: they proved the
    examples and stopped there, while the registrar consumed the untested ``.json`` files.

    What closes it is not this test — it is that the registrar now runs THIS SAME VALIDATOR
    over the real files before it deletes anything. This test proves the validator accepts a
    correct set; ``test_the_registrar_validates_before_it_deletes`` proves production runs it.
    """
    rules = validate_dispatch_set(DISPATCH_DIR, allow_example=True)
    assert {r.agent for r in rules} == set(AGENTS)


def test_the_registrar_validates_the_complete_set_before_it_deletes_anything() -> None:
    """The ordering IS the safety property, so assert the ordering.

    ``register_rule`` deletes the live rule and then creates the replacement. If validation
    happened per-file inside that loop, discovering a bad file at rule three would leave
    rules one and two already deleted — the script would have destroyed working routing on
    its way to telling you it could not finish.

    So preflight must run over ALL FOUR before the first delete. In a bash script that is a
    fact about line order, and nothing else enforces it.
    """
    src = REGISTRAR.read_text()

    preflight_at = src.index("sdk.sip_preflight")
    register_at = src.index('register_rule "${CONFIG_DIR}')
    assert preflight_at < register_at, (
        "the registrar deletes live dispatch rules BEFORE validating their replacements — "
        "a bad file destroys working routing"
    )

    assert "lk sip dispatch list --json" in src[register_at:], (
        "the registrar does not verify the rules are actually live after registering them. "
        "'the command exited 0' is not 'the four of them are routable'."
    )


# --- the validator must REFUSE. A gate with no red path is not a gate. -------------


@pytest.fixture
def candidate_set(tmp_path: Path) -> Path:
    """A valid four-agent dispatch set, on disk, that a test can then corrupt."""
    for agent in AGENTS:
        src = DISPATCH_DIR / f"sip-dispatch-{agent}.json.example"
        shutil.copy(src, tmp_path / f"sip-dispatch-{agent}.json")
    validate_dispatch_set(tmp_path)  # the control: it starts clean
    return tmp_path


def _edit(path: Path, mutate) -> None:
    doc = json.loads(path.read_text())
    mutate(doc["dispatch_rule"])
    path.write_text(json.dumps(doc, indent=2))


def test_the_wrong_sister_is_refused(candidate_set: Path) -> None:
    """THE ONE THAT MATTERS. Aoi's file routing to Nyla.

    It is valid JSON. It registers cleanly. LiveKit is happy. Eric dials Aoi's number and
    Nyla picks up — and the only way anyone finds out is that it happens.
    """
    _edit(
        candidate_set / "sip-dispatch-aoi.json",
        lambda r: r["room_config"]["agents"][0].update({"agentName": "phone-nyla"}),
    )

    with pytest.raises(SipPreflightError) as exc:
        validate_dispatch_set(candidate_set)

    assert any("wrong-sister" in p.lower() for p in exc.value.problems), exc.value.problems


def test_a_duplicate_did_is_refused(candidate_set: Path) -> None:
    """Two sisters claiming one phone number. LiveKit picks one; we do not get to say which.

    Invisible in any per-file check — it only exists when you hold the whole set at once,
    which is precisely why the set is validated before the first delete.
    """
    aoi = json.loads((candidate_set / "sip-dispatch-aoi.json").read_text())
    stolen = aoi["dispatch_rule"]["numbers"][0]
    _edit(candidate_set / "sip-dispatch-yua.json", lambda r: r.update({"numbers": [stolen]}))

    with pytest.raises(SipPreflightError) as exc:
        validate_dispatch_set(candidate_set)

    assert any(stolen in p and "ONE" in p for p in exc.value.problems), exc.value.problems


def test_a_missing_sister_is_refused(candidate_set: Path) -> None:
    """Three files register fine. The fourth agent is simply unreachable by phone, silently."""
    (candidate_set / "sip-dispatch-sumi.json").unlink()

    with pytest.raises(SipPreflightError) as exc:
        validate_dispatch_set(candidate_set)

    assert any("sumi" in p and "MISSING" in p for p in exc.value.problems), exc.value.problems


def test_a_rule_that_matches_no_number_is_refused(candidate_set: Path) -> None:
    """Empty ``numbers`` registers successfully and matches nothing. She never rings."""
    _edit(candidate_set / "sip-dispatch-nyla.json", lambda r: r.update({"numbers": []}))

    with pytest.raises(SipPreflightError) as exc:
        validate_dispatch_set(candidate_set)

    assert any("matches NO inbound call" in p for p in exc.value.problems), exc.value.problems


def test_a_truncated_file_is_refused_before_it_replaces_a_live_rule(candidate_set: Path) -> None:
    """The delete already happened, in the old flow. Then the parse failed."""
    (candidate_set / "sip-dispatch-yua.json").write_text('{"dispatch_rule": {"name":')

    with pytest.raises(SipPreflightError) as exc:
        validate_dispatch_set(candidate_set)

    assert any("not valid JSON" in p for p in exc.value.problems), exc.value.problems


def test_a_stale_rule_for_a_retired_agent_is_refused(candidate_set: Path) -> None:
    """The old ``phone-party`` rule. A worker nobody runs, silently claiming inbound calls."""
    shutil.copy(
        candidate_set / "sip-dispatch-aoi.json",
        candidate_set / "sip-dispatch-party.json",
    )

    with pytest.raises(SipPreflightError) as exc:
        validate_dispatch_set(candidate_set)

    assert any("party" in p for p in exc.value.problems), exc.value.problems


def test_every_problem_is_reported_not_just_the_first(candidate_set: Path) -> None:
    """A validator that stops at the first fault turns one fix into N deploys — and each
    aborted deploy is another window where routing is half-written."""
    _edit(
        candidate_set / "sip-dispatch-aoi.json",
        lambda r: r["room_config"]["agents"][0].update({"agentName": "phone-nyla"}),
    )
    _edit(candidate_set / "sip-dispatch-yua.json", lambda r: r.update({"numbers": []}))
    (candidate_set / "sip-dispatch-sumi.json").unlink()

    with pytest.raises(SipPreflightError) as exc:
        validate_dispatch_set(candidate_set)

    assert len(exc.value.problems) >= 3, exc.value.problems


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
# container is marked UNHEALTHY forever while the agent inside is perfectly fine — and NOTHING
# HAPPENS, which is worse. `restart: unless-stopped` does NOT act on health: Docker restart
# policies fire on container EXIT, not on health status. (An earlier version of this comment
# claimed docker would restart it on a loop. That was FALSE. Yua caught it.) So a port drift
# yields an agent that is permanently unhealthy, never restarted, and — until this pass —
# never reported by `make health` either. Three silences stacked.
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


# ---------------------------------------------------------------------------
# The architecture doc must not invent config fields.
#
# docs/ARCHITECTURE.md described `AgentConfig` as carrying `discord_room`,
# `musubi_v2_presence` and `household_presences`. It has THREE fields: `agent_name`,
# `memory_agent_tag`, `musubi_v2_namespace`. Two of the five named do not exist.
#
# A config doc that names knobs you cannot set is how someone spends an afternoon looking for
# one that was never there — or worse, writes code against it and finds out at runtime.
# ---------------------------------------------------------------------------


def test_the_architecture_doc_names_only_real_config_fields() -> None:
    """Every `AgentConfig` field ARCHITECTURE.md claims must actually exist."""
    import dataclasses

    from sdk.config import AgentConfig

    real = {f.name for f in dataclasses.fields(AgentConfig)}

    arch = REPO_ROOT / "docs" / "ARCHITECTURE.md"
    if not arch.exists():
        pytest.skip("no ARCHITECTURE.md")

    # The block that describes AgentConfig's fields.
    body = arch.read_text()
    m = re.search(r"`AgentConfig` dataclass.*?(?=\n- \*\*|\n##)", body, re.DOTALL)
    if not m:
        pytest.skip("could not locate the AgentConfig description")

    # A doc may NAME a deleted field in order to say it is deleted — that is the point of a
    # retirement note, and the note is what stops someone re-adding the phantom. (This test
    # flagged its own explanatory sentence on the first run: the CHECK being over-broad, not
    # the doc being wrong. Same lesson as the tool-catalog test.)
    lines = [
        line
        for line in m.group(0).splitlines()
        if not any(w in line.lower() for w in ("do not exist", "used to list", "none of them"))
    ]
    claimed = set(re.findall(r"`([a-z][a-z0-9_]*_[a-z0-9_]+)`", "\n".join(lines)))
    # `registration_name` is a derived PROPERTY, not a field — it is legitimately named.
    claimed -= {"registration_name", "phone_name"}

    phantoms = claimed - real
    assert not phantoms, (
        f"docs/ARCHITECTURE.md claims AgentConfig fields that DO NOT EXIST: {sorted(phantoms)}. "
        f"The real fields are {sorted(real)}. A config doc naming knobs you cannot set sends "
        f"the next person hunting one that was never there."
    )


# ---------------------------------------------------------------------------
# THE MEMORY FENCE — the head that was still open.
#
# `assert_agent_identity` proves $AGENT == agent_name. It said NOTHING about
# `musubi_v2_namespace` — and that is the field that decides WHERE THE MEMORIES GO.
#
# This passed at 96e2388:
#
#     AgentConfig(agent_name="aoi", musubi_v2_namespace="nyla/voice")
#
# Aoi answers as Aoi. Registers as phone-aoi. Identity assert passes. And every memory she
# writes lands in nyla/voice/episodic, while musubi_search widens to nyla/*/episodic and
# reads Nyla's entire tenant back to her.
#
# Three heads of the identity model were closed. The one that actually routes the memories
# was left open. (Yua, QA of 96e2388.)
# ---------------------------------------------------------------------------

# The real four, as deployed. A table, so a fifth agent cannot be added without landing here.
EXPECTED_IDENTITY = {
    "aoi": ("aoi-voice", "aoi/voice"),
    "nyla": ("nyla-voice", "nyla/voice"),
    "yua": ("yua-voice", "yua/voice"),
    "sumi": ("sumi-voice", "sumi/voice"),
}


def _agent_config(agent: str):
    """Load the real AgentConfig each agent actually ships."""
    import importlib.util
    import sys

    src = REPO_ROOT / "agents" / agent / "src"
    fn = "agent.py" if agent == "sumi" else "_shared.py"
    spec = importlib.util.spec_from_file_location(f"_cfg_{agent}", src / fn)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(src))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(src))
    return getattr(mod, f"{agent.upper()}_CONFIG")


@pytest.mark.parametrize("agent", AGENTS)
def test_each_agent_writes_to_her_own_tenant(agent: str) -> None:
    """The deployed table. agent_name, memory tag and namespace tenant must all agree."""
    cfg = _agent_config(agent)
    tag, ns = EXPECTED_IDENTITY[agent]

    assert cfg.agent_name == agent
    assert cfg.memory_agent_tag == tag, (
        f"{agent}'s memory tag is {cfg.memory_agent_tag!r}, expected {tag!r} — sharing a tag "
        f"is how one agent's calls bleed into another's recall."
    )
    assert cfg.musubi_v2_namespace == ns
    assert cfg.musubi_v2_namespace.split("/")[0] == cfg.agent_name, (
        f"{agent} writes to tenant {cfg.musubi_v2_namespace.split('/')[0]!r}. THE NAMESPACE "
        f"TENANT MUST BE THE AGENT."
    )


def test_no_two_agents_share_a_namespace_or_a_memory_tag() -> None:
    """Two agents on one tenant is the same misattribution by a different road."""
    seen_ns: dict[str, str] = {}
    seen_tag: dict[str, str] = {}
    for agent in AGENTS:
        cfg = _agent_config(agent)
        assert cfg.musubi_v2_namespace not in seen_ns, (
            f"{agent} and {seen_ns[cfg.musubi_v2_namespace]} share namespace "
            f"{cfg.musubi_v2_namespace!r} — their memories would be indistinguishable."
        )
        assert cfg.memory_agent_tag not in seen_tag, (
            f"{agent} and {seen_tag[cfg.memory_agent_tag]} share memory tag "
            f"{cfg.memory_agent_tag!r}."
        )
        seen_ns[cfg.musubi_v2_namespace] = agent
        seen_tag[cfg.memory_agent_tag] = agent


def test_a_foreign_tenant_cannot_be_constructed() -> None:
    """Yua's exact repro. It must now be impossible to BUILD, not merely to run."""
    from sdk.config import AgentConfig

    with pytest.raises(ValueError) as exc:
        AgentConfig(
            agent_name="aoi",
            memory_agent_tag="aoi-voice",
            musubi_v2_namespace="nyla/voice",  # <- Aoi writing into Nyla's plane
        )
    msg = str(exc.value)
    assert "MEMORY FENCE" in msg
    assert "nyla" in msg


@pytest.mark.parametrize(
    "bad_ns",
    ["aoi", "aoi/voice/extra", "aoi/", "/voice", "", "aoi//voice"],
)
def test_a_non_canonical_namespace_cannot_be_constructed(bad_ns: str) -> None:
    """Empty or extra segments silently reshape the path the memory tools build."""
    from sdk.config import AgentConfig

    with pytest.raises(ValueError):
        AgentConfig(agent_name="aoi", memory_agent_tag="aoi-voice", musubi_v2_namespace=bad_ns)


def test_the_unconfigured_sentinel_still_constructs() -> None:
    """The fail-loud sentinel has namespace=None and must remain buildable — it is the thing
    a forgetful new agent gets INSTEAD of a real identity."""
    from sdk.config import UNCONFIGURED_CONFIG

    assert UNCONFIGURED_CONFIG.musubi_v2_namespace is None
    assert UNCONFIGURED_CONFIG.agent_name == "__unconfigured__"
