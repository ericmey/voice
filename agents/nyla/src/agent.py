"""Nyla voice agent — Gemini 2.5 Flash Native Audio, voice "Aoede".

Registers as "phone-nyla" with LiveKit.

The docstring used to say voice "Leda" — which is YUA'S voice. The one line a reader trusts
first, naming the wrong sister. (`NYLA_VOICE = "Aoede"` in _shared.py has always been right.)

It also trailed off mid-sentence — "For the text-only test variant," and then nothing. A
cleanup deleted `agent_text.py` and left the clause bleeding. There is no text variant; the
phantom is now gone from here, from _shared.py, and from the README.
"""

from __future__ import annotations

import logging

from _shared import NYLA_CONFIG, NylaAgent, build_model, build_tools, load_env_once, load_persona
from livekit.agents import AgentSession, JobContext, cli
from livekit.agents.worker import AgentServer
from sdk.audio_recording import (
    annotate_call_audio_recording,
    start_call_audio_recording,
    wire_call_audio_attachment,
)
from sdk.config import assert_agent_identity
from sdk.duplex import wire_half_duplex
from sdk.liveness import wire_liveness_watchdog
from sdk.musubi_client import wire_musubi_shutdown
from sdk.postcall import wire_postcall_review
from sdk.postcall_memory import arm_postcall_memory, run_postcall_memory
from sdk.telemetry import wire_telemetry_capture
from sdk.telephony import resolve_caller
from sdk.trace import trace
from sdk.tracing import attach_current_span_metadata, wire_otel_shutdown_flush
from sdk.transcript import wire_transcript_logging

# --- env ---------------------------------------------------------------
load_env_once()
# Fail loud at startup if $AGENT / VOICE_AGENT_NAME disagrees with the config.
assert_agent_identity(NYLA_CONFIG)

logger = logging.getLogger("voice.agent")

# --- server + session --------------------------------------------------
# Worker resource posture — set EXPLICITLY, not left to the library defaults.
#
# `num_idle_processes` defaults to 8 in prod. That is 8 pre-forked job processes PER AGENT —
# 32 across the four of them, on one 12-core box. And `job_memory_limit_mb` defaults to 0,
# i.e. DISABLED: a job that leaks has no ceiling and takes the host down with it, along with
# the other three agents and the phone line.
#
# That was an unbounded posture before prewarm. It is worse WITH prewarm, and that is my
# doing: `setup_fnc` runs per job PROCESS, so every idle process now loads its own copy of
# the model. Eight idle Silero VADs per agent is not a warm pool, it is a memory leak with a
# schedule.
#
# This is a personal phone line, not a call centre. ONE warm process per agent is the entire
# point of prewarm — the next call is instant, and the pool refills behind it. The memory
# ceiling is deliberately generous: it exists to kill a runaway, never to fire in normal
# operation. Sumi is the heavy one (Riva + Silero + Orpheus) and idles around 800MB.
#
# WATCH ON THE FIRST CALL: if the pool of 1 is exhausted by back-to-back calls, a second
# caller pays the prewarm cost. Raise it before raising the memory ceiling.
IDLE_PROCESSES = 1
JOB_MEMORY_LIMIT_MB = 4096
server = AgentServer(
    port=8081,
    num_idle_processes=IDLE_PROCESSES,
    job_memory_limit_mb=JOB_MEMORY_LIMIT_MB,
)


@server.rtc_session(
    agent_name=NYLA_CONFIG.registration_name,
    # Post-call extraction runs here, NOT in a shutdown callback: on_session_end
    # fires BEFORE `ShuttingDown` starts the 10s kill clock, and gets the 300s
    # `session_end_timeout`. It replaces a detached subprocess whose only job was
    # to outlive a kill that no longer happens.
    on_session_end=run_postcall_memory,
)
async def entrypoint(ctx: JobContext) -> None:
    reg = NYLA_CONFIG.registration_name
    logger.info("%s entrypoint: room=%s", reg, ctx.room.name)
    trace(f"entrypoint room={ctx.room.name}")

    await ctx.connect()

    caller = await resolve_caller(ctx)
    caller_from = caller.caller_from
    call_sid = caller.call_id
    logger.info(
        "%s caller resolved: from=%s call_id=%s source=%s",
        reg,
        caller_from,
        call_sid,
        caller.source,
    )
    trace(f"caller source={caller.source} from={caller_from!r} call_id={call_sid!r}")

    agent = NylaAgent(
        instructions=load_persona(),
        caller_from=caller_from,
        extra_tools=build_tools(),
    )

    transcript_sid = call_sid
    if not transcript_sid and ctx.room.name.startswith("phone-"):
        transcript_sid = ctx.room.name.removeprefix("phone-")

    audio_recording = await start_call_audio_recording(ctx, call_sid=transcript_sid, agent_name=reg)
    wire_call_audio_attachment(ctx, audio_recording)

    session = AgentSession(llm=build_model())
    wire_transcript_logging(session, transcript_sid, agent_name=reg)
    wire_telemetry_capture(session, transcript_sid, agent_name=reg)
    wire_postcall_review(session, transcript_sid, agent_name=reg)

    # HALF-DUPLEX: close the caller's mic while she speaks. On a speakerphone her own voice
    # returns through his microphone and arrives as caller input — that is what wedged the
    # 2026-07-11 call. Costs barge-in; prevents the loop. (sdk/duplex.py)
    wire_half_duplex(session, call_sid=transcript_sid, agent_name=reg)

    # DEAD-AIR WATCHDOG: a silent call must never look like a healthy one. Runs on its own
    # clock, and user VAD deliberately CANNOT satisfy it — on that call the VAD was being fed
    # by her own echo, so a watchdog trusting it would have been kept alive by the very fault
    # it exists to catch. (sdk/liveness.py)
    wire_liveness_watchdog(session, ctx, call_sid=transcript_sid, agent_name=reg)
    arm_postcall_memory(
        ctx,
        call_sid=transcript_sid,
        namespace=f"{NYLA_CONFIG.musubi_v2_namespace}/episodic"
        if NYLA_CONFIG.musubi_v2_namespace
        else None,
        speaker_tag=NYLA_CONFIG.memory_agent_tag,
    )
    wire_otel_shutdown_flush(ctx)
    wire_musubi_shutdown(ctx)
    await session.start(agent=agent, room=ctx.room)
    attach_current_span_metadata(
        session_id=transcript_sid,
        enduser_id=caller_from,
        dialed_number=caller.dialed_number,
        caller_source=caller.source,
        lk_job_id=getattr(ctx.job, "id", None),
    )
    annotate_call_audio_recording(audio_recording)

    trace(f"session started room={ctx.room.name}")


if __name__ == "__main__":
    cli.run_app(server)
