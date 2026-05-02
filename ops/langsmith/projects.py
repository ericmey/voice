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
        "LiveKit Agents 1.5+ emits gen_ai.* semantic-convention attributes natively "
        "(input_tokens, output_tokens, ttft, model). The previous custom enricher "
        "(sdk/src/sdk/livekit_otel_enricher.py) was removed on 2026-05-01 — see "
        "docs/LANGSMITH.md for the reactivation pathway."
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
            "LLM-as-judge prompt for the recall-accuracy-judge online "
            "evaluator. Scores 0.0–1.0 how accurately the agent's "
            "spoken response used a recall-tool's returned rows. "
            "Referenced by the recall-accuracy-judge evaluator via "
            "prompt_repo_handle. Variable mapping: user_question, "
            "tool_name, tool_result, agent_response — the evaluator "
            "fills these from the function_tool span attributes."
        ),
        "tags": ["role:judge", "metric:recall_accuracy", "model-target:gpt-4o-mini"],
    },
    {
        "name": "tool-choice-judge",
        "source_path": "ops/langsmith/prompts/tool_choice_judge.md",
        "description": (
            "LLM-as-judge prompt for the tool-choice-judge online "
            "evaluator. Scores 0.0–1.0 whether the agent called the "
            "right tool (or none) for the user's question. Encodes "
            "Nyla's and Aoi's tool inventories in the prompt itself so "
            "the judge knows what was available to choose from. "
            "Variable mapping: agent_name, user_question, tool_name, "
            "tool_result, agent_response."
        ),
        "tags": ["role:judge", "metric:tool_choice", "model-target:gpt-4o-mini"],
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


class CodeEvaluatorConfig(TypedDict):
    name: str
    """Display name in the LangSmith UI."""

    type: str  # "code"

    code: str
    """Python source the LangSmith evaluator runtime executes per run.
    The function is named ``perform_eval`` (LangSmith's required entry
    point — name mismatch returns a 400 at provision time) and
    receives a Run object with ``run.inputs``, ``run.outputs``,
    ``run.extra``, ``run.tags``, ``run.name``. Must return a dict (or
    list of dicts) with keys ``key``, ``score``, optionally ``value``
    and ``comment``. Return ``None`` to skip scoring (e.g. metric
    not present on this run)."""

    description: str
    """Operator-facing context — what this evaluator scores and why."""


EvaluatorConfig = LLMEvaluatorConfig | CodeEvaluatorConfig


# Historical (pre-2026-05-01): the deleted livekit_otel_enricher.py
# wrote `langsmith.metadata.{user_question, tool_name, tool_result,
# tool_error, agent}` onto every function_tool span and `e2e_latency_ms
# / endpointing_delay_ms / ttft_ms` onto user_turn / agent_session
# spans. Reactivating any legacy LangSmith evaluator below requires
# reverting the enricher removal — see docs/LANGSMITH.md.
EVALUATORS: list[EvaluatorConfig] = [
    {
        "name": "recall-accuracy-judge",
        "type": "llm",
        "prompt_repo_handle": "recall-accuracy-judge",
        "commit_hash_or_tag": "latest",
        "variable_mapping": {
            # All four come from the enriched function_tool span. Paths
            # follow LangSmith's run schema: ``extra.metadata.<key>`` for
            # span attributes prefixed ``langsmith.metadata.*``.
            "user_question": "extra.metadata.user_question",
            "tool_name": "extra.metadata.tool_name",
            "tool_result": "extra.metadata.tool_result",
            "agent_response": "extra.metadata.agent_response",
        },
    },
    {
        "name": "tool-choice-judge",
        "type": "llm",
        "prompt_repo_handle": "tool-choice-judge",
        "commit_hash_or_tag": "latest",
        "variable_mapping": {
            # Same four as recall, plus agent_name so the judge knows
            # which tool inventory to evaluate against (Nyla and Aoi
            # have different tool sets).
            "user_question": "extra.metadata.user_question",
            "tool_name": "extra.metadata.tool_name",
            "tool_result": "extra.metadata.tool_result",
            "agent_response": "extra.metadata.agent_response",
            "agent_name": "extra.metadata.agent",
        },
    },
    # Code evaluators — rule-based scoring without an LLM call. Cheap;
    # no per-run cost. Run on every span the rule targets in LangSmith.
    {
        "name": "slow-turn-flag",
        "type": "code",
        "description": (
            "Flags any turn with end-to-end latency > 5s. Score 1.0 = "
            "slow (bad), 0.0 = ok. Use to filter the slow-turns "
            "annotation queue and to chart degradations over time."
        ),
        "code": '''def perform_eval(run):
    """Score 1.0 if the turn took longer than 5s end-to-end."""
    extra = getattr(run, "extra", None) or {}
    md = extra.get("metadata", {}) if isinstance(extra, dict) else {}
    raw = md.get("e2e_latency_ms")
    if raw in (None, ""):
        return None
    try:
        e2e_ms = float(raw)
    except (TypeError, ValueError):
        return None
    return {
        "key": "slow_turn",
        "score": 1.0 if e2e_ms > 5000 else 0.0,
        "value": "slow" if e2e_ms > 5000 else "ok",
        "comment": f"e2e_latency_ms={e2e_ms:.0f}",
    }
''',
    },
    {
        "name": "endpointing-blocker",
        "type": "code",
        "description": (
            "Flags turns where the EOU detector sat on the user's audio "
            "longer than 1.5s before letting the LLM start. Score 1.0 = "
            "blocked (bad), 0.0 = responsive. Catches the silence_duration_ms "
            "regressions Eric is tracking on the realtime path."
        ),
        "code": '''def perform_eval(run):
    """Score 1.0 if endpointing held the turn for more than 1.5s."""
    extra = getattr(run, "extra", None) or {}
    md = extra.get("metadata", {}) if isinstance(extra, dict) else {}
    raw = md.get("endpointing_delay_ms")
    if raw in (None, ""):
        return None
    try:
        delay_ms = float(raw)
    except (TypeError, ValueError):
        return None
    return {
        "key": "endpointing_blocked",
        "score": 1.0 if delay_ms > 1500 else 0.0,
        "value": "blocked" if delay_ms > 1500 else "ok",
        "comment": f"endpointing_delay_ms={delay_ms:.0f}",
    }
''',
    },
    {
        "name": "tool-error-flag",
        "type": "code",
        "description": (
            "Flags every function_tool span that came back with "
            "lk.function_tool.is_error=True. Score 1.0 = errored, "
            "0.0 = ok. Pair with the tool-errors annotation queue for "
            "real-time reliability tracking."
        ),
        "code": '''def perform_eval(run):
    """Score 1.0 if this tool call returned an error."""
    if getattr(run, "name", "") != "function_tool":
        return None
    extra = getattr(run, "extra", None) or {}
    md = extra.get("metadata", {}) if isinstance(extra, dict) else {}
    is_error = str(md.get("tool_error", "false")).lower() == "true"
    tool_name = md.get("tool_name", "unknown")
    return {
        "key": "tool_error",
        "score": 1.0 if is_error else 0.0,
        "value": "error" if is_error else "ok",
        "comment": f"tool={tool_name}",
    }
''',
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
