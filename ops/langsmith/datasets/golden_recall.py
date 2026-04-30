"""Golden recall dataset — known-good question/expected pairs.

Each example pins what a CORRECT response looks like for a recall
question. Used for:

1. **Regression tests.** When persona prompts change, replay the
   dataset and check the agent still gets the right tool + reasonable
   output.
2. **LLM-as-judge eval baseline.** When we wire online evals, this
   dataset is the ground truth ``recall_accuracy`` is judged against.
3. **Dataset-driven experimentation.** Compare two persona variants by
   running both against this dataset and diffing scores.

Rules of engagement for adding examples:

- Only add examples whose answer lives somewhere in Musubi at provision
  time. Synthetic "you would know X" examples test nothing real.
- The `expected_tool` is the tool you EXPECT the agent to call. If
  multiple tools could work, pick the one with the right semantic
  match (musubi_recent for "lately", musubi_search for "remember X").
- Avoid loaded questions — keep them representative of how Eric
  actually phrases recall asks on real calls.
"""

from __future__ import annotations

EXAMPLES: list[dict] = [
    {
        "inputs": {
            "user_question": "Hey, do you remember the prank we discussed?",
            "channel": "voice",
        },
        "outputs": {
            "expected_tool": "musubi_search",
            "expected_query_contains": "prank",
            "expected_namespace_pattern": "nyla/*/episodic",
            # Min content-overlap signal — if the actual response contains
            # any of these substrings, the agent retrieved the right row.
            "answer_contains_any": ["cocoa", "coffee pod", "breakroom", "crayon"],
            "notes": (
                "The cocoa-pods prank was saved on openclaw-Nyla; cross-channel "
                "wildcard MUST work for voice-Nyla to recall it."
            ),
        },
    },
    {
        "inputs": {
            "user_question": "What's been going on lately?",
            "channel": "voice",
        },
        "outputs": {
            "expected_tool": "musubi_recent",
            "expected_namespace_pattern": "nyla/*/episodic",
            "answer_contains_any": [],  # any non-empty recall passes
            "notes": (
                "Pure recency question — should hit musubi_recent, not search. "
                "Cross-modal default per ADR 0032."
            ),
        },
    },
    {
        "inputs": {
            "user_question": "What's my favorite band again?",
            "channel": "voice",
        },
        "outputs": {
            "expected_tool": "musubi_search",
            "expected_query_contains": "favorite",
            "answer_contains_any": ["Gojira"],
            "notes": (
                "User profile fact stored at 2026-04-26. "
                "If this fails, search isn't surfacing user-profile rows."
            ),
        },
    },
    {
        "inputs": {
            "user_question": "Tell my Claude Code session that the deploy is done.",
            "channel": "voice",
        },
        "outputs": {
            "expected_tool": "musubi_think",
            "expected_recipient_pattern": "claude*|aoi*",
            "answer_contains_any": ["sent", "delivered", "told"],
            "notes": (
                "Inter-agent message — should fire musubi_think, not "
                "musubi_remember. Watch for tool-selection drift."
            ),
        },
    },
    {
        "inputs": {
            "user_question": "Save a note that I want to refactor the auth middleware tomorrow.",
            "channel": "voice",
        },
        "outputs": {
            "expected_tool": "musubi_remember",
            "expected_content_contains": "auth middleware",
            "answer_contains_any": ["saved", "got it", "remembered", "noted"],
            "notes": (
                "Explicit save request — must hit musubi_remember. "
                "Common tool-selection failure: agent tries to call musubi_think instead."
            ),
        },
    },
]
