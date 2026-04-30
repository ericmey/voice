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


# ---------------------------------------------------------------------------
# Workspace secrets — provider API keys loaded into LangSmith for online
# evaluators (LLM-as-judge, etc.) to authenticate with.
#
# These secrets are consumed by LangSmith's online-evaluator runtime when
# it spins up a judge LLM. Without them, LLM-as-judge evals fail with
# auth errors at run time.
#
# Source: each entry's ``key`` is looked up in the ``~/.openclaw/.env``
# file (override via ``OPENCLAW_ENV_PATH``). The actual key VALUE is
# never checked into this repo — only the NAMES we forward.
#
# Stored on LangSmith side via POST /api/v1/workspaces/current/secrets.
# Once stored, you select these provider credentials in the LangSmith
# UI when configuring an online evaluator's model.
# ---------------------------------------------------------------------------


class WorkspaceSecret(TypedDict):
    key: str
    """The name LangSmith stores it under. Convention is the standard
    provider env-var name (OPENAI_API_KEY, etc.) so evaluators can
    pick it up by environment-variable convention."""

    description: str
    """Operator-facing context — why this key is loaded, what model
    families it covers."""


class WorkspacePrompt(TypedDict):
    name: str
    """LangSmith prompt identifier — appears in the UI sidebar.
    Convention: ``<agent>-system`` for the main persona prompt."""

    source_path: str
    """Path under the repo root to the prompt's source file."""

    description: str
    """Operator-facing context shown on the prompt's detail page."""

    tags: list[str]
    """Tags attached to the prompt artifact for filtering. Convention:
    ``agent:<name>``, ``role:system``, ``pipeline:<realtime|chained>``."""


# ---------------------------------------------------------------------------
# Workspace prompts — agent system prompts pushed to LangSmith's prompt
# library so operators can play with them in the playground, version
# diffs across edits, and use them as reference for online evaluators.
#
# Source files live in this repo under ``agents/<name>/prompts/system.md``.
# The provisioner reads the file content and pushes a new commit only
# when the content differs from the current LangSmith commit. Re-running
# with no edits is a no-op.
#
# Why this matters:
# - The playground lets you tweak a prompt and replay it against
#   datasets without touching the deployed agent — perfect for
#   "would this opener work better?" experiments.
# - LangSmith's online evaluators can reference these versioned
#   prompts ("compare current agent response to what THIS prompt
#   would say"), enabling A/B style judging.
# - Version history is the audit trail for "when did Aoi's persona
#   start sounding stiff?" — diff commits over time.
#
# IMPORTANT: edits in the LangSmith UI playground DO NOT round-trip
# back to this repo. The source-of-truth is always the file in
# ``agents/<name>/prompts/system.md``. Treat the LangSmith artifact
# as a read-mostly mirror; iterate on a copy in the UI, then promote
# good edits back to the source file via PR.
# ---------------------------------------------------------------------------


WORKSPACE_PROMPTS: list[WorkspacePrompt] = [
    {
        "name": "nyla-system",
        "source_path": "agents/nyla/prompts/system.md",
        "description": (
            "Phone-Nyla system prompt — household router persona on the "
            "realtime voice pipeline (Gemini 2.5 Flash Native Audio). "
            "Source-of-truth is agents/nyla/prompts/system.md in the repo. "
            "Edits here don't affect production until the source file is "
            "updated and agents are redeployed."
        ),
        "tags": ["agent:nyla", "role:system", "pipeline:realtime"],
    },
    {
        "name": "aoi-system",
        "source_path": "agents/aoi/prompts/system.md",
        "description": (
            "Phone-Aoi system prompt — code-partner persona on the "
            "realtime voice pipeline (Gemini 2.5 Flash Native Audio). "
            "Source-of-truth is agents/aoi/prompts/system.md in the repo. "
            "Edits here don't affect production until the source file is "
            "updated and agents are redeployed."
        ),
        "tags": ["agent:aoi", "role:system", "pipeline:realtime"],
    },
    {
        "name": "recall-accuracy-judge",
        "source_path": "ops/langsmith/prompts/recall_accuracy_judge.md",
        "description": (
            "LLM-as-judge prompt for the RECALL_ACCURACY_JUDGE online "
            "evaluator. Scores 0.0–1.0 how accurately the agent's "
            "spoken response used a recall-tool's returned rows. "
            "Referenced by the recall-accuracy-judge evaluator via "
            "prompt_repo_handle. Variable mapping: user_question, "
            "tool_name, tool_result, agent_response — the evaluator "
            "fills these from the function_tool span attributes."
        ),
        "tags": ["role:judge", "metric:recall_accuracy", "model-target:gpt-4o-mini"],
    },
]


WORKSPACE_SECRETS: list[WorkspaceSecret] = [
    {
        "key": "OPENAI_API_KEY",
        "description": (
            "OpenAI access for LLM-as-judge (gpt-4o-mini default for "
            "RECALL_ACCURACY_JUDGE per ONLINE_EVAL_NOTES). Also covers "
            "Whisper-1 evals if we add transcript-quality scoring later."
        ),
    },
    {
        "key": "GOOGLE_API_KEY",
        "description": (
            "Gemini access for LLM-as-judge alternative + cheaper baseline. "
            "Same key already used by the live agents for Gemini 2.5 Flash "
            "Native Audio (realtime voice path)."
        ),
    },
    {
        "key": "XAI_API_KEY",
        "description": (
            "xAI (Grok) access — second-opinion judge candidate. Useful "
            "for diversity on contested LLM-as-judge calls where GPT and "
            "Gemini both come out of the same training-data lineage."
        ),
    },
    {
        "key": "OPENROUTER_API_KEY",
        "description": (
            "OpenRouter — hits any of ~100 providers via single key. "
            "Handy for evaluator experimentation (try Claude, Llama, etc.) "
            "without provisioning per-provider creds."
        ),
    },
]


# ---------------------------------------------------------------------------
# Annotation queues — manual-review surfaces for traces that need eyes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Online evaluators — automated scoring on every new trace.
#
# LangSmith's stable public API exposes evaluators at /v1/platform/evaluators.
# Two types:
#
#   - llm   References a prompt from the prompt library + variable mapping.
#           The variable mapping pulls fields from each run (span attributes,
#           inputs, outputs) into the prompt's template variables. Output is
#           parsed as JSON: {"score": float, "reasoning": str}.
#
#   - code  Inline Python that evaluates a run and returns a score dict.
#           Useful for rule-based scoring (e.g., "fail if e2e_latency > 5s")
#           without paying for an LLM call.
#
# Run rules (when does the evaluator fire?) are managed separately and
# are NOT yet in this IaC pass. Operator wires that in the LangSmith UI:
# Evaluators -> Online tab -> attach to project + add filter conditions.
# When the run-rules API stabilises in a way that's friendly to declarative
# config, we'll add a phase 7. For now, evaluators are CREATED here;
# attaching them to projects is one UI click.
# ---------------------------------------------------------------------------


class LLMEvaluatorConfig(TypedDict):
    name: str
    """Display name in the LangSmith UI."""

    type: str  # "llm"

    prompt_repo_handle: str
    """The prompt artifact in this workspace's prompt library that drives
    the judging. Convention: pushed to LangSmith via WORKSPACE_PROMPTS."""

    commit_hash_or_tag: str
    """Pin to a specific commit OR use ``latest``. Use ``latest`` for
    "always use the most recent push" — best for iteration."""

    variable_mapping: dict[str, str]
    """Maps prompt template variables (left) to span/run JSONPath
    expressions (right). The evaluator runtime fills the prompt
    by reading these from each run."""


EVALUATORS: list[LLMEvaluatorConfig] = [
    {
        "name": "recall-accuracy-judge",
        "type": "llm",
        "prompt_repo_handle": "recall-accuracy-judge",
        "commit_hash_or_tag": "latest",
        "variable_mapping": {
            # The evaluator runtime extracts these from each function_tool
            # run when the rule fires. Field paths follow LangSmith's run
            # schema: ``inputs.<key>`` / ``outputs.<key>`` / ``extra.metadata.<key>``.
            "user_question": "extra.metadata.user_question",
            "tool_name": "extra.metadata.tool_name",
            "tool_result": "outputs.output",
            "agent_response": "extra.metadata.agent_response",
        },
    },
]


# ---------------------------------------------------------------------------
# Annotation queues — manual-review surfaces for traces that need eyes
# ---------------------------------------------------------------------------


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
