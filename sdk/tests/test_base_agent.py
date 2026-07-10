"""Tests for BaseRealtimeAgent's shared greeting logic.

``build_greeting_instructions`` is the on_enter instruction assembly for every
native-audio agent (aoi/nyla/yua). Pure, so the branching between usable
context and degraded/empty memory is testable without a live LiveKit session —
the gap that previously left this shared path covered only by the gated
integration test.
"""

from tools.base_agent import _GREETING_BASE, build_greeting_instructions


def test_no_context_returns_base_only():
    assert build_greeting_instructions("") == _GREETING_BASE
    assert build_greeting_instructions(None) == _GREETING_BASE


def test_degraded_memory_strings_are_not_spliced_as_context():
    """fetch_recent_context returns user-readable strings when memory is
    unavailable — they must NOT be presented to the model as real context."""
    for degraded in (
        "No recent memories found.",
        "Couldn't check memory — Musubi is unavailable right now.",
        "Memory lookup timed out.",
    ):
        out = build_greeting_instructions(degraded)
        assert out == _GREETING_BASE
        assert degraded not in out


def test_real_context_is_appended_as_background_awareness():
    context = "[nyla] Eric mentioned the dentist appointment on Tuesday."
    out = build_greeting_instructions(context)
    # Base greeting still leads; context is appended as awareness, not a script.
    assert out.startswith(_GREETING_BASE)
    assert context in out
    assert "for your awareness" in out
    assert "not a script" in out


def test_context_framing_discourages_formulaic_recall():
    """Eric's feedback (2026-04-27): openers that lead with a recall read as
    calculated. The framing must tell the model to only surface something
    notable, not to open with it."""
    out = build_greeting_instructions("[nyla] some recent thing")
    assert "only" in out and "notable" in out
    assert "Don't lead with a recall" in out
