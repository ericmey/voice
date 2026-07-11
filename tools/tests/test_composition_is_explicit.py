"""Each agent states her own tools. The base class must never decide for her.

## Why this matters now

The four agents are about to stop being the same product with different faces. Nyla runs on
Hermes and will grow tools she shares with Sumi but not the others. Aoi is on Claude Code
with an interaction channel of her own. Yua is on Codex. Their *capabilities* are the point
of them being different people.

## What was in the way

`BaseRealtimeAgent` was declared:

    class BaseRealtimeAgent(CoreToolsMixin, MusubiToolsMixin, Agent)

so the base class **decided the tool set for everyone who inherited it**. A subclass could
ADD tools (`extra_tools=`) but could never choose a *different* set. So the first agent who
genuinely needed a different composition had no move except to bypass the base entirely.

That is exactly what Sumi did — and it is *why* she ended up with a duplicated persona loader
carrying a different silent default, her own copy of `__init__`, and her own divergent
config. Every one of those was a downstream symptom of one upstream mistake: **the base
pre-empted composition.**

The mixin mechanism was never the problem. The base taking the decision was.

## The split

- `BaseVoiceAgent`   — config, `__init__`, lifecycle. NO tools. NO model.
- `BaseRealtimeAgent(BaseVoiceAgent)` — adds only the Gemini realtime greeting.
- Every agent lists her own mixins.

What stays *shared* is infrastructure, not capability: identity/config, the Musubi client and
its per-agent namespace fence, telemetry, telephony, transcripts, post-call memory. Those
must have exactly one implementation — four hand-rolled copies would be four chances to write
one girl's memories into another's namespace.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tools.base_agent import BaseRealtimeAgent, BaseVoiceAgent
from tools.core import CoreToolsMixin
from tools.memory import MusubiToolsMixin

REPO = Path(__file__).resolve().parents[2]
AGENTS = ("aoi", "nyla", "yua", "sumi")


def _load_agent_class(agent: str) -> type:
    """Import an agent's class BY FILE PATH.

    Three of the four declare it in a module literally named `_shared` — so a plain
    `importlib.import_module("_shared")` returns whichever one landed in sys.modules first,
    and the test silently checks the same agent three times. Load by path, under a unique
    name.
    """
    import importlib.util
    import sys

    src_dir = REPO / "agents" / agent / "src"
    filename = "agent.py" if agent == "sumi" else "_shared.py"
    spec = importlib.util.spec_from_file_location(f"_agent_{agent}", src_dir / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(src_dir))  # sumi's agent.py imports sibling `orpheus_tts`
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(src_dir))
    return getattr(module, f"{agent.capitalize()}Agent")


def _agent_source(agent: str) -> str:
    """Sumi declares her class in agent.py; the other three in _shared.py."""
    for candidate in (
        REPO / "agents" / agent / "src" / "_shared.py",
        REPO / "agents" / agent / "src" / "agent.py",
    ):
        if not candidate.exists():
            continue
        src = candidate.read_text()
        if re.search(rf"class {agent.capitalize()}Agent\(", src):
            return src
    raise AssertionError(f"could not find {agent}'s Agent class")


# ---------------------------------------------------------------------------
# The base must not own the tool set
# ---------------------------------------------------------------------------


def test_the_base_classes_carry_no_tool_mixins() -> None:
    """If the base inherits a tool mixin, it has taken the decision away from the agents.

    This is the regression that matters. Re-adding `CoreToolsMixin` to `BaseVoiceAgent`
    would look harmless — every agent uses it today — and would silently make it impossible
    for a future agent to NOT have it.
    """
    for base in (BaseVoiceAgent, BaseRealtimeAgent):
        offenders = [
            klass.__name__ for klass in base.__mro__ if klass.__name__.endswith("ToolsMixin")
        ]
        assert not offenders, (
            f"{base.__name__} inherits tool mixins {offenders}. The base must not decide "
            f"which tools an agent has — that is what forced Sumi to bypass it entirely and "
            f"duplicate the persona loader, __init__, and config. Compose on the AGENT."
        )


def test_base_voice_agent_carries_no_model() -> None:
    """BaseVoiceAgent must stay model-agnostic — it is Sumi's base too, and she is not Gemini."""
    # NOTE: `hasattr` is useless here — livekit's own `Agent` defines `on_enter`, so it is
    # always inherited. The question is whether OUR base DEFINES one, i.e. its own __dict__.
    assert "on_enter" not in BaseVoiceAgent.__dict__, (
        "BaseVoiceAgent defines on_enter. The greeting is model-specific: a chained "
        "STT->LLM->TTS pipeline cannot generate_reply() at session start. Keep it on "
        "BaseRealtimeAgent, or Sumi is forced out of the base again."
    )
    assert "on_enter" in BaseRealtimeAgent.__dict__, (
        "BaseRealtimeAgent no longer defines the realtime greeting"
    )


# ---------------------------------------------------------------------------
# Every agent states her own composition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_every_agent_declares_her_own_tool_mixins(agent: str) -> None:
    """The composition must be visible AT THE AGENT — that is where a reader looks."""
    src = _agent_source(agent)
    decl = re.search(rf"class {agent.capitalize()}Agent\((.*?)\):", src, re.DOTALL)
    assert decl, f"{agent} has no Agent class declaration"

    bases = decl.group(1)
    assert "CoreToolsMixin" in bases, (
        f"{agent} does not declare CoreToolsMixin in her own class. Inheriting tools "
        f"invisibly from a base is what this refactor removed."
    )
    assert "MusubiToolsMixin" in bases, (
        f"{agent} does not declare MusubiToolsMixin. Her memory tools — and the namespace "
        f"fence that keeps her memories out of her sisters' planes — must be stated on her."
    )


@pytest.mark.parametrize("agent", AGENTS)
def test_every_agent_actually_has_the_tools_she_declares(agent: str) -> None:
    """Declaration is not enough — the MRO has to really carry them.

    A source-text check alone would pass on a typo in a comment. This imports the real class.
    """
    klass = _load_agent_class(agent)
    mro = klass.__mro__
    assert CoreToolsMixin in mro, f"{agent} declares CoreToolsMixin but does not inherit it"
    assert MusubiToolsMixin in mro, f"{agent} declares MusubiToolsMixin but does not inherit it"
    assert BaseVoiceAgent in mro, (
        f"{agent} does not inherit BaseVoiceAgent — she is re-implementing the shared "
        f"scaffolding (config, __init__, persona) instead of using it. That divergence is "
        f"how the identity bugs got in."
    )


def test_sumi_is_the_only_agent_off_the_realtime_base() -> None:
    """Her divergence must be exactly ONE thing: the pipeline, hence the greeting.

    Anything more means the base is failing her again.
    """
    off_realtime = [a for a in AGENTS if BaseRealtimeAgent not in _load_agent_class(a).__mro__]

    assert off_realtime == ["sumi"], (
        f"expected only sumi to be off BaseRealtimeAgent (she runs a chained pipeline and "
        f"cannot generate_reply() at session start); got {off_realtime}. Anyone else off it "
        f"needs a stated reason."
    )


def test_no_agent_reimplements_the_shared_scaffolding() -> None:
    """__init__, config handling and the persona loader belong to the base. Once.

    Sumi had a byte-for-byte copy of the base's `__init__`. Not because she needed one —
    because the base bundled a tool set she could not refuse.
    """
    offenders: list[str] = []
    for agent in AGENTS:
        src = _agent_source(agent)
        if re.search(r"def __init__\(\s*\n?\s*self,\s*\n?\s*\*,\s*\n?\s*caller_from", src):
            offenders.append(f"{agent} re-implements BaseVoiceAgent.__init__")
        if "def _load_persona" in src or "_DEFAULT_PERSONA" in src:
            offenders.append(f"{agent} re-implements the persona loader")
    assert not offenders, offenders
