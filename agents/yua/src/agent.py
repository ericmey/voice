"""Yua voice agent — Gemini 2.5 Flash Native Audio.

Registers as "phone-yua" with LiveKit. See src/_shared.py for the agent
class, model, tools, and persona loading.
"""

from __future__ import annotations

import logging

from _shared import YUA_CONFIG, YuaAgent, build_model, build_tools, load_env_once, load_persona
from livekit.agents import AgentSession, JobContext, cli
from livekit.agents.worker import AgentServer
from sdk.audio_recording import (
    annotate_call_audio_recording,
    start_call_audio_recording,
    wire_call_audio_attachment,
)
from sdk.config import assert_agent_identity
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
assert_agent_identity(YUA_CONFIG)

logger = logging.getLogger("voice.agent")

# --- server + session --------------------------------------------------
server = AgentServer(port=8085)


@server.rtc_session(
    agent_name=YUA_CONFIG.registration_name,
    # Post-call extraction runs here, NOT in a shutdown callback: on_session_end
    # fires BEFORE `ShuttingDown` starts the 10s kill clock, and gets the 300s
    # `session_end_timeout`. It replaces a detached subprocess whose only job was
    # to outlive a kill that no longer happens.
    on_session_end=run_postcall_memory,
)
async def entrypoint(ctx: JobContext) -> None:
    reg = YUA_CONFIG.registration_name
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

    agent = YuaAgent(
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
    arm_postcall_memory(
        ctx,
        call_sid=transcript_sid,
        namespace=f"{YUA_CONFIG.musubi_v2_namespace}/episodic"
        if YUA_CONFIG.musubi_v2_namespace
        else None,
        speaker_tag=YUA_CONFIG.memory_agent_tag,
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
