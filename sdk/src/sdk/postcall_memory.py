"""Post-call memory extraction — turn the transcript into focused memories.

After a call ends, the on-call LLM only saved what the user explicitly
asked for. Everything else — the texture, the side-threads, the
half-formed ideas — sits in the transcript and disappears unless we
extract it.

This module reads the saved transcript file, sends it to Gemini Flash
for *faithful chunked extraction* (preserve detail, don't summarise),
and posts each extracted moment as a normal episodic memory. Maturation
handles importance/topic enrichment on its hourly tick like any other
row.

Wire it into the agent's session via :func:`wire_postcall_memory`. That
registers a ``close`` handler which **spawns a detached subprocess** to
run the extraction. Subprocess (not asyncio task) because the LiveKit
worker process tears down after the job ends, killing any in-flight
coroutines on its event loop — which silently lost extractions on short
calls. The subprocess survives parent shutdown and runs to completion
in its own process group.

Failure modes:
- ``LIVEKIT_VOICE_LOGS`` unset → no-op (no transcript path to read).
- Gemini errors / malformed JSON → log and skip; explicit saves still
  landed during the call. The user loses texture for that one call.
- Capture errors (transient Musubi outage) → per-memory; we keep going.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import google.genai as genai
from google.genai import types as genai_types
from livekit.agents import AgentSession

from .musubi_client import (
    MusubiClient,
    MusubiClientConfig,
    MusubiError,
)
from .trace import trace

logger = logging.getLogger("voice.agent")


# --- controlled vocabulary --------------------------------------------------

CATEGORIES: tuple[str, ...] = (
    "personal",
    "work",
    "project",
    "idea",
    "decision",
    "health",
    "household",
    "learning",
    "planning",
    "reflection",
    "general",
)
"""Closed set of category tags. Each extracted memory gets exactly one,
attached as ``category:<value>``. Closed set keeps tag bloat bounded."""


_GEMINI_MODEL = "gemini-2.5-flash-lite"
"""Structured extraction model. Flash Lite is cheap, fast, and good
enough at structured-output extraction over a 4-8k token transcript.
Bump to Pro if quality drifts."""


_EXTRACTION_PROMPT = """You are processing a transcript from a phone call between Eric and one of his AI partners (Nyla, Aoi, Yua, or Sumi).

**STT transcription notes — important.** Phone STT systems struggle with the assistants' names because they're not common English words. When you see the user addressing the assistant in the transcript, the name may have been corrupted by speech recognition. Common substitutions:
- "Nyla" may appear as "Inla", "Milo", "Niala", "Nila", "Nyla", or similar phonetic neighbors.
- "Aoi" may appear as "Owie", "Howie", "Ali", "Oui", or similar.
- "Yua" may appear as "Youa", "Yuwa", "Yua", "Yuma", "Yuna", or similar.
- Other agent names (Hana, Yumi, Rin, Tama, Sumi, Momo, Mizuki, Reika, Yua, Nana, Shiori) may be transcribed inconsistently too.

When extracting memories, **always render assistant names in their canonical spelling** (Nyla, Aoi, Hana, Yumi, Rin, Tama, Sumi, Momo, Mizuki, Reika, Yua, Nana, Shiori). This keeps memories searchable and consistent across calls regardless of STT quality on any given day.

Extract distinct memories from this transcript. Each memory should preserve a coherent moment of the conversation as it actually happened. **Do NOT summarise.** Preserve the specific details, the texture, the why — what Eric was anxious about, excited about, the specific context.

For each memory, return:
- `content`: 1-3 sentences in natural prose, written from the assistant's perspective ("Eric mentioned that...", "We discussed..."). Preserve specifics. Don't abstract down to a bullet point.
- `summary`: one-line headline (≤80 chars) — the topic, not the conclusion.
- `topics`: 1-3 specific noun-phrases for retrieval. Lowercase. Hyphens for spaces.
- `category`: ONE of: personal, work, project, idea, decision, health, household, learning, planning, reflection, general.

A coherent moment is a continuous stretch of conversation about one thing. If Eric talked about three different things, that's three memories.

SKIP:
- Greetings, sign-offs, "are you there", "can you hear me", confirmation chatter.
- Things the assistant explicitly confirmed it stored (look for "got it, stored" or similar acks — those are already saved).
- Pure off-topic asides or noise.

CATEGORY GUIDE:
- `project` — Musubi, voice agents, code/infrastructure, side projects.
- `work` — day-job context (Salesforce / Salesai).
- `personal` — life, family, friends, hobbies, daily routines.
- `idea` — new directions or hypotheses.
- `decision` — choices made or pending.
- `health` — body, fitness, sleep, food.
- `household` — Bridget, kids, house operations.
- `learning` — things observed/understood.
- `planning` — schedules, deadlines, next-actions.
- `reflection` — thoughts about how things are going.
- `general` — fallback only.

Return JSON only. Schema:
{"memories": [{"content": "...", "summary": "...", "topics": [...], "category": "..."}]}

If the transcript has no extractable memories (too short, all greetings, etc.), return: {"memories": []}

Transcript:
"""


# --- data shapes ------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedMemory:
    """One faithful chunk pulled from a transcript."""

    content: str
    summary: str
    topics: list[str]
    category: str


# --- transcript discovery ---------------------------------------------------


def _voice_logs() -> Path | None:
    logs = os.environ.get("LIVEKIT_VOICE_LOGS")
    return Path(logs) if logs else None


def _transcript_path(call_sid: str) -> Path | None:
    base = _voice_logs()
    return base / "phone-transcripts" / f"{call_sid}.txt" if base else None


def _read_transcript(call_sid: str) -> str | None:
    """Read the per-call transcript file, or None if missing/unreadable."""
    path = _transcript_path(call_sid)
    if path is None or not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.error("postcall_memory: transcript read failed: %s", exc)
        return None


# --- extraction -------------------------------------------------------------


def _gemini_api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _validate_memory(raw: Any) -> ExtractedMemory | None:
    """Coerce a raw dict from Gemini into a clean :class:`ExtractedMemory`,
    or return None if the shape is bad enough to drop.

    Forgiving: missing fields fill with sensible defaults. Non-string
    values for the string fields (``content``, ``summary``, ``category``)
    are rejected explicitly so a payload like ``{"content": 123}``
    drops the row cleanly instead of raising ``AttributeError`` on
    ``.strip()`` — that crash used to propagate up through the
    validation loop and abort the whole extraction, an unclassified
    failure that bypassed the typed-status surface.
    """
    if not isinstance(raw, dict):
        return None
    raw_content = raw.get("content")
    if not isinstance(raw_content, str):
        return None
    content = raw_content.strip()
    if not content:
        return None
    raw_summary = raw.get("summary")
    summary = (raw_summary if isinstance(raw_summary, str) else content[:80]).strip()
    raw_topics = raw.get("topics") or []
    topics: list[str] = []
    if isinstance(raw_topics, list):
        for t in raw_topics[:5]:
            if isinstance(t, str) and t.strip():
                topics.append(t.strip().lower())
    raw_category = raw.get("category")
    category = (raw_category if isinstance(raw_category, str) else "general").strip().lower()
    if category not in CATEGORIES:
        category = "general"
    return ExtractedMemory(
        content=content,
        summary=summary,
        topics=topics,
        category=category,
    )


# Status hints emitted by `_extract_memories` so the caller's
# completion log distinguishes "Gemini auth broke" from "transcript was
# genuinely empty" — the conflation that hid the 2026-05-15 voice-path
# silent-loss for three days (see #29).
#
# `extracted` = memories list non-empty, capture step runs next.
# `empty_extraction` = valid transcript reached Gemini, Gemini returned
#   no memory objects. Genuinely uneventful call.
# `no_transcript_text` = transcript was whitespace-only (handled here;
#   `run_extraction` separately handles the "no transcript file" case
#   as `no_transcript`).
# `no_api_key` = GEMINI_API_KEY / GOOGLE_API_KEY unset.
# `auth_failed` = Gemini returned 401 / 403 / UNAUTHENTICATED. The
#   typical credential-rotation-not-propagated failure (see
#   wiki/gotchas/voice-deploy-traps §1).
# `transport_failed` = catch-all non-auth Gemini call failure (network
#   error, timeout, connection refused, unexpected SDK-side exception).
#   The provider may or may not have been reached — distinguishing them
#   requires more SDK-specific exception introspection than is worth
#   the complexity for the operator-side signal. Treat as "something
#   went wrong outside the auth check; look at the ERROR log line above
#   the completion line for specifics."
# `parse_failed` = Gemini returned non-JSON or non-conformant JSON
#   (no `memories` array, wrong shape).
ExtractionStatus = Literal[
    "extracted",
    "empty_extraction",
    "no_transcript_text",
    "no_api_key",
    "auth_failed",
    "transport_failed",
    "parse_failed",
]


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome of one Gemini extraction call.

    ``memories`` is empty for every status except ``extracted``. The
    ``status`` field carries the cause when ``memories`` is empty so
    the completion-log line can distinguish auth failure from
    genuine no-extraction (the silent-loss class fixed in
    #29).
    """

    memories: list[ExtractedMemory]
    status: ExtractionStatus


def _classify_gemini_exception(exc: BaseException) -> ExtractionStatus:
    """Map a Gemini SDK exception to an `ExtractionStatus`.

    The google-genai SDK doesn't expose typed auth/transport exceptions
    we can isinstance-match cleanly. Falls back to substring matching
    on the formatted message — the same shape the production 2026-05-15
    incident surfaced (``"401 UNAUTHENTICATED"`` substring). Order
    matters: auth check before transport so a 401 reaching us via a
    transport-shaped exception still classifies correctly.
    """
    msg = str(exc)
    if "401" in msg or "403" in msg or "UNAUTHENTICATED" in msg or "PERMISSION_DENIED" in msg:
        return "auth_failed"
    return "transport_failed"


async def _extract_memories(transcript: str) -> ExtractionResult:
    """Send the transcript to Gemini Flash and parse the JSON response.

    Returns an :class:`ExtractionResult` whose ``status`` field
    distinguishes the failure modes that used to collapse to ``[]``:
    auth failure, transport failure, parse failure, missing API key,
    or genuinely empty extraction. The caller uses the status hint
    on the completion log line.
    """
    if not transcript.strip():
        return ExtractionResult(memories=[], status="no_transcript_text")
    api_key = _gemini_api_key()
    if not api_key:
        logger.warning("postcall_memory: no Gemini API key, skipping extraction")
        return ExtractionResult(memories=[], status="no_api_key")

    client = genai.Client(api_key=api_key)

    def _call() -> str:
        # Synchronous call wrapped via to_thread below. The genai client
        # has both sync and async surfaces; sync is simpler here and we
        # don't care about latency in the post-call window.
        resp = client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=_EXTRACTION_PROMPT + transcript,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
        return resp.text or ""

    try:
        raw_text = await asyncio.to_thread(_call)
    except Exception as exc:
        logger.error("postcall_memory: Gemini call failed: %s", exc)
        return ExtractionResult(memories=[], status=_classify_gemini_exception(exc))

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.error("postcall_memory: malformed JSON from Gemini: %s", exc)
        return ExtractionResult(memories=[], status="parse_failed")

    raw_memories = data.get("memories") if isinstance(data, dict) else None
    if not isinstance(raw_memories, list):
        return ExtractionResult(memories=[], status="parse_failed")

    out: list[ExtractedMemory] = []
    for r in raw_memories:
        m = _validate_memory(r)
        if m is not None:
            out.append(m)
    if not out:
        # Gemini reached + parsed but produced 0 valid memories. This is
        # the genuine "uneventful call" path, distinct from auth/parse
        # failures above.
        return ExtractionResult(memories=[], status="empty_extraction")
    return ExtractionResult(memories=out, status="extracted")


# --- capture ----------------------------------------------------------------


async def _capture_one(
    *,
    client: MusubiClient,
    namespace: str,
    memory: ExtractedMemory,
    speaker_tag: str | None,
    call_sid: str,
) -> bool:
    """Capture one extracted memory. Returns True on success.

    Tags are: the extracted topics, plus ``category:<value>``, plus the
    agent's speaker tag (e.g. ``nyla-voice``), plus a ``source:transcript``
    marker so we can tell extracted rows from explicit saves at audit
    time. Importance defaults to 5; maturation rescores hourly.
    """
    tags = list(memory.topics)
    tags.append(f"category:{memory.category}")
    tags.append("source:transcript")
    if speaker_tag:
        tags.append(speaker_tag)

    idem = f"livekit-postcall:{call_sid}:{uuid.uuid4().hex}"
    try:
        ack = await client.capture_memory(
            namespace=namespace,
            content=memory.content,
            tags=tags,
            importance=5,
            idempotency_key=idem,
        )
    except MusubiError as exc:
        logger.warning(
            "postcall_memory: capture failed for call_sid=%s: %s",
            call_sid,
            exc,
        )
        return False

    object_id = ack.get("object_id") or "<unknown>"
    trace(
        f"postcall_memory: captured object_id={object_id} category={memory.category} "
        f"call_sid={call_sid}"
    )
    return True


async def run_extraction(
    *,
    call_sid: str,
    namespace: str,
    speaker_tag: str | None,
    client: MusubiClient | None = None,
) -> int:
    """Read the transcript, extract memories, capture them all.

    Returns the count of memories successfully captured. 0 means either
    no transcript, no extraction, or all captures failed. Designed to be
    called via ``asyncio.create_task`` from the close handler — never
    blocks the caller.

    If ``client`` is None, builds one from environment via
    :meth:`MusubiClientConfig.from_env`.
    """
    started = time.monotonic()

    def _complete(status: str, *, extracted: int = 0, captured: int = 0) -> int:
        """Single completion log line so audit/Rin can grep one shape.

        Status is one of:
        - ``no_transcript`` (transcript file missing / unreadable)
        - ``no_transcript_text`` (file present, body whitespace-only)
        - ``no_api_key`` (GEMINI_API_KEY/GOOGLE_API_KEY unset)
        - ``auth_failed`` (Gemini 401/403 — most often a stale key
          per wiki/gotchas/voice-deploy-traps §1)
        - ``transport_failed`` (Gemini network/timeout error)
        - ``parse_failed`` (Gemini returned non-JSON or wrong shape)
        - ``empty_extraction`` (Gemini reached + parsed but produced
          zero memories — genuinely uneventful call)
        - ``captured`` (memories extracted AND at least one capture
          succeeded)
        - ``no_captures`` (memories extracted but every capture failed)

        Pre-#29, ``auth_failed`` / ``transport_failed`` /
        ``parse_failed`` / ``empty_extraction`` all logged as
        ``empty_extraction`` — silently hid the 2026-05-15 voice path
        breakage for three days.
        """
        total_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "postcall_memory: completed call_sid=%s status=%s extracted=%d captured=%d total_ms=%d",
            call_sid,
            status,
            extracted,
            captured,
            total_ms,
        )
        trace(
            f"postcall_memory: completed call_sid={call_sid} status={status} "
            f"extracted={extracted} captured={captured} total_ms={total_ms}"
        )
        return captured

    transcript = _read_transcript(call_sid)
    if transcript is None:
        return _complete("no_transcript")

    result = await _extract_memories(transcript)
    if result.status != "extracted":
        # Propagate the typed status from _extract_memories — distinguishes
        # auth/transport/parse failures from genuine empty extraction.
        return _complete(result.status)

    if client is None:
        cfg = MusubiClientConfig.from_env()
        client = MusubiClient(cfg)

    captured = 0
    for memory in result.memories:
        ok = await _capture_one(
            client=client,
            namespace=namespace,
            memory=memory,
            speaker_tag=speaker_tag,
            call_sid=call_sid,
        )
        if ok:
            captured += 1

    status = "captured" if captured > 0 else "no_captures"
    return _complete(status, extracted=len(result.memories), captured=captured)


# --- wiring -----------------------------------------------------------------


def wire_postcall_memory(
    session: AgentSession,
    *,
    call_sid: str | None,
    namespace: str | None,
    speaker_tag: str | None,
) -> None:
    """Register a ``close`` handler that runs transcript extraction.

    Call this AFTER ``wire_postcall_review`` in the agent entrypoint.
    Both can register on the same session — livekit allows multiple
    listeners on the ``close`` event.

    No-ops if any of ``call_sid``, ``namespace``, or
    ``LIVEKIT_VOICE_LOGS`` is missing — those are required for
    extraction to make sense.
    """
    if not call_sid:
        trace("postcall_memory: no call_sid, not wiring")
        return
    if not namespace:
        trace("postcall_memory: no namespace, not wiring")
        return
    if _voice_logs() is None:
        trace("postcall_memory: LIVEKIT_VOICE_LOGS unset, not wiring")
        return

    @session.on("close")
    def _on_close(ev: Any) -> None:
        _spawn_extraction_subprocess(
            call_sid=call_sid,
            namespace=namespace,
            speaker_tag=speaker_tag,
        )

    trace(f"postcall_memory wired call_sid={call_sid} namespace={namespace}")


# --- subprocess spawn -------------------------------------------------------


def _postcall_logfile() -> Path | None:
    """Where extraction-subprocess stdout/stderr land. One shared file
    across all calls — the per-line ``call_sid=`` makes ``grep`` cheap."""
    base = _voice_logs()
    return base / "postcall-memory.log" if base else None


def _spawn_extraction_subprocess(
    *,
    call_sid: str,
    namespace: str,
    speaker_tag: str | None,
) -> None:
    """Spawn a detached Python subprocess to run :func:`run_extraction`.

    Uses ``sys.executable -m sdk.postcall_memory`` so the subprocess
    runs inside the same venv as the parent agent. The child inherits
    the parent's environment (Gemini key, Musubi base URL + token,
    voice-logs dir) so no extra wiring is needed.

    Failures to spawn (FileNotFoundError, PermissionError, OSError)
    log at ERROR but do not raise — Path B is best-effort.
    """
    logfile = _postcall_logfile()

    args = [
        sys.executable,
        "-m",
        "sdk.postcall_memory",
        "--call-sid",
        call_sid,
        "--namespace",
        namespace,
    ]
    if speaker_tag:
        args.extend(["--speaker-tag", speaker_tag])

    try:
        if logfile is not None:
            # Parent opens, passes fd to child via Popen's dup, closes its
            # own copy. Child keeps its dup until it exits naturally.
            with logfile.open("a", encoding="utf-8") as fp:
                subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=fp,
                    stderr=fp,
                    start_new_session=True,
                    close_fds=True,
                )
        else:
            subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        trace(f"postcall_memory: spawned subprocess call_sid={call_sid}")
        logger.info("postcall_memory: spawned subprocess call_sid=%s", call_sid)
    except Exception as exc:
        logger.error("postcall_memory: failed to spawn subprocess: %s", exc)
        trace(f"postcall_memory: spawn failed call_sid={call_sid}: {exc}")


# --- CLI entry --------------------------------------------------------------


def _cli_main() -> int:
    """``python -m sdk.postcall_memory --call-sid X --namespace Y [--speaker-tag Z]``

    Subprocess entry. Logs to whatever stdout/stderr was inherited by the
    spawn — typically ``$LIVEKIT_VOICE_LOGS/postcall-memory.log`` per
    :func:`_spawn_extraction_subprocess`. Exits 0 on completion (incl.
    no_transcript / empty_extraction); 2 on argparse errors.
    """
    parser = argparse.ArgumentParser(prog="sdk.postcall_memory")
    parser.add_argument("--call-sid", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--speaker-tag", default=None)
    args = parser.parse_args()

    # Subprocess inherits no logging handlers; configure a minimal one so
    # logger.info / logger.error lines actually reach the inherited stderr.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    asyncio.run(
        run_extraction(
            call_sid=args.call_sid,
            namespace=args.namespace,
            speaker_tag=args.speaker_tag,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(_cli_main())
