"""A missing persona file must FAIL LOUD. It must never degrade into a stranger.

`load_persona()` used to do this:

    if path.exists():
        return path.read_text(...)
    logger.warning("persona file not found: %s", path)
    return _DEFAULT_PERSONA          # "You are a voice assistant on a phone call."

So if `prompts/system.md` went missing — a bad image build, a bind-mount typo, a file not
copied into the container — the agent did not fail. It **answered Eric's phone as a generic
assistant**. A warning went into a log nobody was reading, and the call went ahead.

That is the same class as every other identity bug in this codebase: `NYLA_DEFAULT_CONFIG`
(a misconfigured agent silently becomes Nyla), `ENV AGENT=aoi` (a container with no AGENT
silently becomes Aoi), a hand-typed SIP dispatch literal (a call silently routes to the
wrong sister). In each case the system had a *default* where it needed a *refusal*.

The persona IS the identity. An agent without hers is not a degraded Nyla — she is not Nyla
at all. She is a stranger holding Nyla's phone number, and she is talking to Eric.

And this one degrades at the single worst moment: mid-call, live, with no way to recover.
Better to refuse to start.

Sumi had her own copy of this loader with a *different* fallback string
("You are the Harem World host on a phone call with Eric"), which is the same defect plus
divergence. Both are gone; there is one loader now, and it raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tools.base_agent import load_persona


def test_a_real_persona_file_loads(tmp_path: Path) -> None:
    (tmp_path / "system.md").write_text("You are Nyla.\n", encoding="utf-8")
    assert load_persona(tmp_path) == "You are Nyla."


def test_a_missing_persona_file_raises_instead_of_answering_as_a_stranger(
    tmp_path: Path,
) -> None:
    """No persona => no agent. Refuse to start; do not answer the phone as someone else."""
    with pytest.raises(FileNotFoundError) as exc:
        load_persona(tmp_path)

    # The message has to tell an operator at 2am what actually happened.
    msg = str(exc.value)
    assert "system.md" in msg
    assert "identity" in msg.lower()


def test_an_empty_persona_file_also_raises(tmp_path: Path) -> None:
    """An empty (or whitespace-only) system.md is the same failure wearing a different hat —
    the file exists, so `path.exists()` was satisfied, and the agent would have gone on air
    with an empty system prompt. That is a stranger too."""
    (tmp_path / "system.md").write_text("   \n\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_persona(tmp_path)
    assert "empty" in str(exc.value).lower()


def test_there_is_exactly_one_persona_loader() -> None:
    """No agent may carry its own copy with its own fallback.

    Sumi had `_load_persona()` + `_DEFAULT_PERSONA = "You are the Harem World host..."`
    duplicated in her agent.py — a second implementation with a *different* silent default.
    Divergence-without-reason, in the identity path.
    """
    repo = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for agent in ("aoi", "nyla", "yua", "sumi"):
        src = (repo / "agents" / agent / "src" / "agent.py").read_text()
        if "_DEFAULT_PERSONA" in src:
            offenders.append(f"agents/{agent}/src/agent.py declares its own _DEFAULT_PERSONA")
        if "def _load_persona" in src:
            offenders.append(f"agents/{agent}/src/agent.py declares its own _load_persona")

    assert not offenders, (
        "these agents duplicate the persona loader instead of using tools.base_agent."
        f"load_persona: {offenders}. One loader, one behaviour, no silent defaults — the "
        "persona is identity."
    )


def test_no_default_persona_constant_survives_anywhere() -> None:
    """The fallback string itself must not exist. If it exists, someone will return it."""
    repo = Path(__file__).resolve().parents[2]
    hits: list[str] = []
    for path in list((repo / "tools" / "src").rglob("*.py")) + list(
        (repo / "sdk" / "src").rglob("*.py")
    ):
        if "_DEFAULT_PERSONA" in path.read_text():
            hits.append(str(path.relative_to(repo)))
    assert not hits, (
        f"_DEFAULT_PERSONA still exists in {hits}. A default persona is a stranger with a "
        f"warning attached."
    )


# ---------------------------------------------------------------------------
# The voice is identity too.
# ---------------------------------------------------------------------------


def test_build_realtime_model_requires_an_explicit_voice() -> None:
    """`voice` must be required. It used to default to "Leda" — which is YUA's voice.

    Every current agent passes it explicitly, so the default never fired. But a new agent —
    or a refactor that dropped the argument — would have silently SOUNDED LIKE YUA while
    introducing herself as someone else. A default in the identity path is a stranger waiting
    for the first person who forgets an argument.
    """
    import inspect

    from tools.base_agent import build_realtime_model

    sig = inspect.signature(build_realtime_model)
    voice = sig.parameters["voice"]

    assert voice.default is inspect.Parameter.empty, (
        f"build_realtime_model(voice=...) defaults to {voice.default!r}. There must be no "
        f"default voice — an agent that forgets the argument would sound like whoever that "
        f"default belongs to."
    )
    assert voice.kind is inspect.Parameter.KEYWORD_ONLY, (
        "voice must be keyword-only, so it can never be passed positionally by accident"
    )


def test_every_agent_declares_its_own_voice() -> None:
    """Nobody may inherit a voice. Each agent states hers, explicitly."""
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for agent in ("aoi", "nyla", "yua"):  # sumi is a local chain (Orpheus), not Gemini
        shared = repo / "agents" / agent / "src" / "_shared.py"
        src = shared.read_text()
        assert re.search(r"build_realtime_model\(\s*voice\s*=", src), (
            f"{agent} calls build_realtime_model without an explicit voice="
        )
