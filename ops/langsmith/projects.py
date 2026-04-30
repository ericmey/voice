"""Declarative LangSmith project + workspace settings.

Edit the constants below; re-run ``make langsmith-provision`` to apply.
Idempotent — the apply script reads current state and writes only the
delta.

Each section maps 1:1 to a LangSmith API resource. Comments explain
WHY the chosen value matters for our setup, not just WHAT it is —
the latter is in the LangSmith docs.
"""

from __future__ import annotations

from typing import TypedDict


class ProjectSettings(TypedDict):
    description: str
    """One-line description shown in the LangSmith project list."""

    metadata: dict[str, str]
    """Project-level metadata key/value pairs surfaced in the UI."""


# ---------------------------------------------------------------------------
# Project: Harem World — voice agents (Nyla, Aoi, Party)
#
# Intent: this project receives every OTel span emitted by Nyla and Aoi
# (the realtime voice agents) and Party (chained pipeline benchmark).
# Tracing is wired via OpenTelemetry — see sdk/src/sdk/tracing.py.
# ---------------------------------------------------------------------------

PROJECT_SETTINGS: ProjectSettings = {
    "description": (
        "OpenClaw LiveKit voice agents — realtime traces from Nyla, Aoi, and Party. "
        "Spans are enriched by sdk/src/sdk/langsmith_processor.py with per-stage "
        "latency metadata (ttft_ms, endpointing_delay_ms, e2e_latency_ms) so each "
        "turn shows where time went. Tool calls render with `tool:<name>` tags."
    ),
    "metadata": {
        "managed_by": "ops/langsmith/provision.py",
        "source_repo": "github.com/ericmey/openclaw-livekit",
        "agents": "nyla,aoi,party",
        "pipeline_shapes": "realtime,chained",
    },
}


# ---------------------------------------------------------------------------
# Feedback configs — one entry per dimension we'd like to score traces on.
#
# Feedback configs add click-to-rate UI buttons in LangSmith next to
# every trace. Eric (or anyone with access) can score a trace as it's
# being reviewed, and those scores flow into datasets / analytics over
# time — building up a quality signal without hand-rolled tooling.
# ---------------------------------------------------------------------------


class FeedbackConfig(TypedDict):
    feedback_key: str
    """Stable key — referenced in code and in the API."""

    description: str
    """Shown to humans rating the trace."""

    feedback_score_spec: dict
    """LangSmith API shape: ``{"type": "categorical|continuous", ...}``."""


FEEDBACK_CONFIGS: list[FeedbackConfig] = [
    {
        "feedback_key": "recall_accuracy",
        "description": (
            "Did the agent's answer match what was actually saved in memory? "
            "1 = exact match, 0.5 = partial / plausible, 0 = wrong or hallucinated."
        ),
        "feedback_score_spec": {
            "type": "continuous",
            "min": 0.0,
            "max": 1.0,
        },
    },
    {
        "feedback_key": "naturalness",
        "description": (
            "How conversational did the agent sound? Penalises formulaic / "
            'robotic phrasing ("As an AI assistant…", "Let me check that '
            'for you…" without context). 1 = friend texture, 5 = robot. '
            "(LangSmith categorical configs require numeric values; the "
            "label carries the semantic meaning operators see in the UI.)"
        ),
        "feedback_score_spec": {
            "type": "categorical",
            "categories": [
                {"value": 1, "label": "1 — sounds like a friend"},
                {"value": 2, "label": "2 — warm but slightly stiff"},
                {"value": 3, "label": "3 — neutral assistant"},
                {"value": 4, "label": "4 — clearly robotic"},
                {"value": 5, "label": "5 — actively off-putting"},
            ],
        },
    },
    {
        "feedback_key": "tool_choice",
        "description": (
            "Did the agent call the RIGHT tool for the question? E.g., "
            "musubi_search for 'remember X' vs musubi_recent for 'what's new'. "
            "0=correct, 1=wrong tool, 2=missing tool (should have called one), "
            "3=extra tool (called one unnecessarily)."
        ),
        "feedback_score_spec": {
            "type": "categorical",
            "categories": [
                {"value": 0, "label": "Correct tool"},
                {"value": 1, "label": "Wrong tool chosen"},
                {"value": 2, "label": "Should have called a tool, didn't"},
                {"value": 3, "label": "Called a tool unnecessarily"},
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Online evaluators — auto-run scoring on every new trace.
#
# These are NOT yet provisioned by code because LangSmith's online-eval
# REST API isn't stable as of vendor-time. Documented here so the intent
# survives until the API is stable / the SDK exposes it; configure in
# the LangSmith dashboard for now and copy the JSON config into a
# follow-up PR when ready to migrate to code.
# ---------------------------------------------------------------------------

ONLINE_EVAL_NOTES = """
TODO once LangSmith online-eval API stabilises: provision these via code.

1. SLOW_TURN_FLAG
   Trigger: any user_turn span where langsmith.metadata.e2e_latency_ms > 5000
   Action: tag trace with `slow-turn`, write to annotation queue `slow-turns`
   Rationale: surfaces every "felt slow" call without manual searching

2. ENDPOINTING_BLOCKER
   Trigger: user_turn spans where langsmith.metadata.endpointing_delay_ms > 1500
   Action: tag `endpointing-blocked`, route to annotation queue
   Rationale: catches the silence_duration_ms regression Eric is tracking

3. TOOL_ERROR_ALERT
   Trigger: any function_tool span with tag `error`
   Action: post Slack/Discord alert (configured in UI)
   Rationale: real-time tool failure detection

4. RECALL_ACCURACY_JUDGE (LLM-as-judge)
   Trigger: any function_tool span where tool_name == 'musubi_search'
   Judge: GPT-4o-mini compares tool result to agent's spoken response
          and scores recall_accuracy 0-1
   Cost: budget cap before enabling — every musubi_search adds ~$0.001
"""


# ---------------------------------------------------------------------------
# Annotation queues — manual-review surfaces for traces that need eyes
# ---------------------------------------------------------------------------


class AnnotationQueueConfig(TypedDict):
    name: str
    description: str


ANNOTATION_QUEUES: list[AnnotationQueueConfig] = [
    {
        "name": "slow-turns",
        "description": (
            "Turns where end-to-end latency exceeded 5s. Auto-routed by the "
            "SLOW_TURN_FLAG online eval (when stable; manual until then)."
        ),
    },
    {
        "name": "tool-errors",
        "description": (
            "Tool calls that returned with `error` tag. Useful for tracking "
            "Musubi/Whisper/ElevenLabs reliability over time."
        ),
    },
    {
        "name": "weekly-quality",
        "description": (
            "Random sample of 10 calls/week for human review against the "
            "naturalness + recall_accuracy + tool_choice feedback configs."
        ),
    },
]
