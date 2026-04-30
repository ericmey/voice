"""Cross-modal recall dataset — same agent, different surfaces.

The premise: every memory saved on one surface (Claude Code, openclaw,
voice phone call) MUST be recallable on the other surfaces under the
same agent. This dataset pins examples where Eric saves something on
surface A and asks for it on surface B; a correct answer means the
``nyla/*/episodic`` (or ``aoi/*/episodic``) wildcard search worked
end-to-end.

This dataset is the regression test for ADR 0032 (cross-modal default)
and ADR 0030 (agent-as-tenant namespace model). When either of those
ADRs gets touched, replay this dataset.

Rules of engagement:

- Each example specifies a ``saved_on`` surface and an ``asked_on``
  surface. They MUST be different. Same-surface examples belong in
  ``golden_recall`` instead.
- ``answer_contains_any`` is the minimum signal that the row was
  retrieved — not full content match. A real answer will paraphrase
  the saved fact in the agent's voice.
- Stick to facts Eric actually saved, not synthetic ones. Synthetic
  examples test the search index but not the production memory.
"""

from __future__ import annotations

EXAMPLES: list[dict] = [
    {
        "inputs": {
            "user_question": "What's my favorite metal band?",
            "channel": "voice",
            "agent": "nyla",
        },
        "outputs": {
            "saved_on": "openclaw-nyla",
            "asked_on": "voice-nyla",
            "expected_tool": "musubi_search",
            "expected_namespace_pattern": "nyla/*/episodic",
            "expected_query_contains": "favorite",
            "answer_contains_any": ["Gojira"],
            "notes": (
                "User-profile fact saved on openclaw, asked on voice. "
                "Tests cross-modal search hits the openclaw episodic "
                "namespace from voice."
            ),
        },
    },
    {
        "inputs": {
            "user_question": "Did I tell you about the prank with the cocoa pods?",
            "channel": "voice",
            "agent": "nyla",
        },
        "outputs": {
            "saved_on": "openclaw-nyla",
            "asked_on": "voice-nyla",
            "expected_tool": "musubi_search",
            "expected_namespace_pattern": "nyla/*/episodic",
            "expected_query_contains": "cocoa",
            "answer_contains_any": ["cocoa", "coffee pod", "breakroom"],
            "notes": (
                "Specific event saved as a story on openclaw. "
                "Asking on voice forces the ``*`` machine wildcard."
            ),
        },
    },
    {
        "inputs": {
            "user_question": "What did Aoi and I land on for the LangSmith integration?",
            "channel": "claude-code",
            "agent": "aoi",
        },
        "outputs": {
            "saved_on": "voice-aoi",
            "asked_on": "claude-code-aoi",
            "expected_tool": "musubi_search",
            "expected_namespace_pattern": "aoi/*/episodic",
            "expected_query_contains": "LangSmith",
            "answer_contains_any": ["evaluator", "judge", "online", "trace"],
            "notes": (
                "Decision discussed verbally with Phone-Aoi, recalled in "
                "Claude Code. Tests Aoi's cross-modal namespace too."
            ),
        },
    },
    {
        "inputs": {
            "user_question": "What's been going on with you lately?",
            "channel": "voice",
            "agent": "nyla",
        },
        "outputs": {
            "saved_on": "any-nyla",
            "asked_on": "voice-nyla",
            "expected_tool": "musubi_recent",
            "expected_namespace_pattern": "nyla/*/episodic",
            "answer_contains_any": [],
            "notes": (
                "Recency question, NOT a search. Even with cross-modal "
                "wildcards, must pick recent over search."
            ),
        },
    },
    {
        "inputs": {
            "user_question": "What did we decide about Tama's bone structure?",
            "channel": "voice",
            "agent": "aoi",
        },
        "outputs": {
            "saved_on": "openclaw-aoi",
            "asked_on": "voice-aoi",
            "expected_tool": "musubi_search",
            "expected_namespace_pattern": "aoi/*/episodic",
            "expected_query_contains": "Tama",
            "answer_contains_any": ["bone structure", "cheekbone", "facial"],
            "notes": (
                "Character-design canon discussed with Aoi in Claude Code, "
                "recalled on voice. Tests cross-modal even within Aoi's "
                "creative-collaboration use case."
            ),
        },
    },
    {
        "inputs": {
            "user_question": "Hey Nyla, what's my Tesla's name?",
            "channel": "voice",
            "agent": "nyla",
        },
        "outputs": {
            "saved_on": "openclaw-nyla",
            "asked_on": "voice-nyla",
            "expected_tool": "musubi_search",
            "expected_namespace_pattern": "nyla/*/episodic",
            "expected_query_contains": "Tesla",
            "answer_contains_any": [],
            "notes": (
                "Personal-fact recall. Tests not just the search but "
                "whether tone stays casual when answering on voice."
            ),
        },
    },
    {
        "inputs": {
            "user_question": "What was the Musubi pre-rank scoring decision?",
            "channel": "claude-code",
            "agent": "aoi",
        },
        "outputs": {
            "saved_on": "voice-aoi",
            "asked_on": "claude-code-aoi",
            "expected_tool": "musubi_search",
            "expected_namespace_pattern": "aoi/*/episodic",
            "expected_query_contains": "pre-rank",
            "answer_contains_any": ["pre-rank", "intake", "scoring", "W1.2"],
            "notes": (
                "Architecture discussion held verbally; recalled in code. "
                "If this misses, the cross-modal pull is broken."
            ),
        },
    },
    {
        "inputs": {
            "user_question": "Did I save anything about the deploy freeze?",
            "channel": "voice",
            "agent": "nyla",
        },
        "outputs": {
            "saved_on": "any-nyla",
            "asked_on": "voice-nyla",
            "expected_tool": "musubi_search",
            "expected_namespace_pattern": "nyla/*/episodic",
            "expected_query_contains": "deploy freeze",
            "answer_contains_any": [],
            "notes": (
                "Operational fact — date / thing-being-frozen. Tests "
                "search recall on imprecise wording."
            ),
        },
    },
    {
        "inputs": {
            "user_question": "What did Eric say about LiveKit yesterday?",
            "channel": "voice",
            "agent": "aoi",
        },
        "outputs": {
            "saved_on": "any-aoi",
            "asked_on": "voice-aoi",
            "expected_tool": "musubi_recent",
            "expected_namespace_pattern": "aoi/*/episodic",
            "answer_contains_any": [],
            "notes": (
                'Recency-anchored ("yesterday") forces musubi_recent. '
                "Common failure mode: agent picks search because the "
                "subject is technical."
            ),
        },
    },
    {
        "inputs": {
            "user_question": "Could you remind Aoi I'm logging off in 10?",
            "channel": "voice",
            "agent": "nyla",
        },
        "outputs": {
            "saved_on": "n/a",
            "asked_on": "voice-nyla",
            "expected_tool": "session_send",
            "expected_recipient_pattern": "aoi*",
            "answer_contains_any": ["sent", "told", "delivered", "passed"],
            "notes": (
                "Inter-agent message routing — Nyla sends, Aoi receives. "
                "Tests session_send picks the right recipient by name."
            ),
        },
    },
]
