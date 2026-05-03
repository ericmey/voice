"""Aoi voice agent — Gemini 2.5 Flash Native Audio, voice "Leda".

Registers as "phone-aoi" with LiveKit. Mirrors phone-nyla's setup until
Aoi gets her own specialized configuration; see src/_shared.py for the
agent class, model, tools, and persona loading.
"""

from __future__ import annotations

import logging

from _shared import AOI_CONFIG, AoiAgent, build_model, build_tools, load_env_once, load_persona
from livekit.agents import AgentSession, JobContext, cli
from livekit.agents.worker import AgentServer
from sdk.audio_recording import (
    annotate_call_audio_recording,
    start_call_audio_recording,
    wire_call_audio_attachment,
)
from sdk.postcall import wire_postcall_review
from sdk.postcall_memory import wire_postcall_memory
from sdk.telemetry import wire_telemetry_capture
from sdk.telephony import resolve_caller
from sdk.trace import trace
from sdk.tracing import attach_current_span_metadata, wire_otel_shutdown_flush
from sdk.transcript import wire_transcript_logging

# --- env ---------------------------------------------------------------
load_env_once()

logger = logging.getLogger("openclaw-livekit.agent")

# --- server + session --------------------------------------------------
server = AgentServer(port=8082)


@server.rtc_session(agent_name="phone-aoi")
async def entrypoint(ctx: JobContext) -> None:
    logger.info("phone-aoi entrypoint: room=%s", ctx.room.name)
    trace(f"entrypoint room={ctx.room.name}")

    await ctx.connect()

    caller = await resolve_caller(ctx)
    caller_from = caller.caller_from
    call_sid = caller.call_id
    logger.info(
        "phone-aoi caller resolved: from=%s call_id=%s source=%s",
        caller_from,
        call_sid,
        caller.source,
    )
    trace(f"caller source={caller.source} from={caller_from!r} call_id={call_sid!r}")

    agent = AoiAgent(
        instructions=load_persona(),
        caller_from=caller_from,
        extra_tools=build_tools(),
    )

    transcript_sid = call_sid
    if not transcript_sid and ctx.room.name.startswith("phone-"):
        transcript_sid = ctx.room.name.removeprefix("phone-")

    audio_recording = await start_call_audio_recording(
        ctx, call_sid=transcript_sid, agent_name="phone-aoi"
    )
    wire_call_audio_attachment(ctx, audio_recording)

    session = AgentSession(llm=build_model())
    wire_transcript_logging(session, transcript_sid, agent_name="phone-aoi")
    wire_telemetry_capture(session, transcript_sid, agent_name="phone-aoi")
    wire_postcall_review(session, transcript_sid, agent_name="phone-aoi")
    wire_postcall_memory(
        session,
        call_sid=transcript_sid,
        namespace=f"{AOI_CONFIG.musubi_v2_namespace}/episodic"
        if AOI_CONFIG.musubi_v2_namespace
        else None,
        speaker_tag=AOI_CONFIG.memory_agent_tag,
    )
    wire_otel_shutdown_flush(ctx)
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
