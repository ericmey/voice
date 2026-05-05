#!/usr/bin/env python3
"""Text-to-text agent simulation harness.

Dispatches a LiveKit voice agent into a fresh room, joins as a test
participant, and drives the conversation via the built-in lk.chat text
stream. The agent responds exactly as it would on a phone call — same
persona, same tools, same model — just text in, transcription out.

Usage:
    # Run against phone-nyla (default)
    python text_simulator.py

    # Run against phone-aoi
    python text_simulator.py --agent phone-aoi

    # Custom LiveKit server
    python text_simulator.py --lk-url ws://10.0.10.55:7880

Requires LIVEKIT_API_KEY and LIVEKIT_API_SECRET in the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass

from livekit import api, rtc
from livekit.protocol.agent_dispatch import CreateAgentDispatchRequest

# ── Config ────────────────────────────────────────────────────────────


@dataclass
class SimConfig:
    lk_url: str = "ws://127.0.0.1:7880"
    api_key: str = ""
    api_secret: str = ""
    agent_name: str = "phone-nyla"
    identity: str = "eric-test"
    room_prefix: str = "sim-test"
    # How long to wait for the agent to respond to each message (seconds).
    # Must be generous — tool calls (musubi, embedding) can take a few seconds
    # before the agent even starts generating its text response.
    response_timeout: float = 45.0
    # How long to wait for agent to join after dispatch (seconds)
    agent_join_timeout: float = 20.0


# ── Conversation script ──────────────────────────────────────────────
# Natural conversational patter with tool calls woven in organically.
# Each entry is (message, expected_behavior_note).

CONVERSATION: list[tuple[str, str]] = [
    # 1. Warm opener — no tool call, just personality check
    (
        "Hey! How's it going?",
        "greeting — personality/warmth check",
    ),
    # 2. Casual follow-up — still just chatting
    (
        "Yeah I'm doing good. Been a long day though. What have you been up to?",
        "conversational patter — agent should stay in character",
    ),
    # 3. Natural lead-in to time tool
    (
        "Oh man, I totally lost track of time working on this project. What time is it anyway?",
        "TOOL: get_current_time — should call tool and narrate the time",
    ),
    # 4. React to the time, then pivot to memories
    (
        "Wow, that late already? Hey, have we talked about anything interesting lately? I feel like my brain is mush right now.",
        "TOOL: musubi_recent — should recall recent conversation memories",
    ),
    # 5. More patter — test for agent staying grounded, not hallucinating tools
    (
        "Ha, that's right. You know, sometimes I wonder if we spend too much time on technical stuff and not enough just hanging out.",
        "conversational — should respond naturally, NO tool call expected",
    ),
    # 6. Delegate to another agent — fire-and-forget via openclaw_delegate
    (
        "Speaking of the other girls, can you ping Hana and ask her how the livekit migration is going? I want to make sure she's in the loop.",
        "TOOL: openclaw_delegate — should fire-and-forget a message to Hana",
    ),
    # 7. Casual wind-down
    (
        "Alright cool, thanks for checking. I should probably get some rest soon.",
        "conversational — natural wind-down, no tool call",
    ),
    # 8. Goodbye — should trigger EndCallTool
    (
        "Alright, goodnight! Talk to you tomorrow.",
        "TOOL: EndCallTool — should say goodbye and end the session",
    ),
]


# ── Color output ─────────────────────────────────────────────────────


def _c(code: int, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def cyan(t: str) -> str:
    return _c(36, t)


def green(t: str) -> str:
    return _c(32, t)


def yellow(t: str) -> str:
    return _c(33, t)


def red(t: str) -> str:
    return _c(31, t)


def dim(t: str) -> str:
    return _c(2, t)


def bold(t: str) -> str:
    return _c(1, t)


# ── Simulator ────────────────────────────────────────────────────────


class TextSimulator:
    def __init__(self, config: SimConfig) -> None:
        self.config = config
        self.room_name = f"{config.room_prefix}-{uuid.uuid4().hex[:8]}"
        self.room = rtc.Room()
        self._agent_joined = asyncio.Event()
        self._agent_identity: str | None = None
        self._response_chunks: list[str] = []
        self._response_complete = asyncio.Event()
        self._connected = False
        self._pending_tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        """Run the full conversation script."""
        print(bold(f"\n{'=' * 60}"))
        print(bold("  Text-to-Text Agent Simulation"))
        print(bold(f"  Agent: {self.config.agent_name}"))
        print(bold(f"  Room:  {self.room_name}"))
        print(bold(f"{'=' * 60}\n"))

        try:
            await self._connect()
            await self._dispatch_agent()
            await self._wait_for_agent()
            await self._run_conversation()
        except KeyboardInterrupt:
            print(yellow("\n\n[interrupted by user]"))
        except Exception as e:
            print(red(f"\n[error] {e}"))
            raise
        finally:
            await self._cleanup()

    async def _connect(self) -> None:
        """Mint a token and connect to the LiveKit room."""
        print(dim(f"[sim] minting token for {self.config.identity} in {self.room_name}"))

        token = (
            api.AccessToken(self.config.api_key, self.config.api_secret)
            .with_identity(self.config.identity)
            .with_name("Eric (Test)")
            .with_grants(
                api.VideoGrants(
                    room=self.room_name,
                    room_join=True,
                    room_create=True,
                    can_publish=True,
                    can_publish_data=True,
                    can_subscribe=True,
                )
            )
        )
        jwt = token.to_jwt()

        # Wire event handlers before connect
        self.room.on("participant_connected", self._on_participant_connected)
        self.room.on("participant_disconnected", self._on_participant_disconnected)

        # Listen on BOTH output paths to determine which one the agent uses:
        # 1. Legacy: publish_transcription() → "transcription_received" event
        #    (used when agent has an audio track)
        # 2. Text stream: stream_text(topic="lk.transcription")
        #    (used when agent has no audio track / text-only mode)
        self.room.on("transcription_received", self._on_transcription_received)
        self.room.register_text_stream_handler("lk.transcription", self._on_text_stream)
        self.room.register_text_stream_handler("lk.chat", self._on_text_stream)

        print(dim(f"[sim] connecting to {self.config.lk_url}"))
        await self.room.connect(
            self.config.lk_url,
            jwt,
            options=rtc.RoomOptions(
                auto_subscribe=True,
                dynacast=False,
            ),
        )
        self._connected = True
        print(green(f"[sim] connected to room {self.room_name}"))

    async def _dispatch_agent(self) -> None:
        """Dispatch the agent into the room."""
        http_url = self.config.lk_url.replace("ws://", "http://").replace("wss://", "https://")
        lk_api = api.LiveKitAPI(
            url=http_url, api_key=self.config.api_key, api_secret=self.config.api_secret
        )

        metadata = json.dumps(
            {
                "callSid": f"SIM-{uuid.uuid4().hex[:12]}",
                "from": "+10000000000",
                "to": "+10000000001",
                "test": True,
            }
        )

        print(dim(f"[sim] dispatching {self.config.agent_name} into {self.room_name}"))
        await lk_api.agent_dispatch.create_dispatch(
            CreateAgentDispatchRequest(
                room=self.room_name,
                agent_name=self.config.agent_name,
                metadata=metadata,
            )
        )
        await lk_api.aclose()
        print(green(f"[sim] dispatch sent for {self.config.agent_name}"))

    async def _wait_for_agent(self) -> None:
        """Wait for the agent to join the room."""
        print(dim(f"[sim] waiting for agent to join (timeout={self.config.agent_join_timeout}s)"))

        # Check if agent already joined before we started listening
        for p in self.room.remote_participants.values():
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                self._agent_identity = p.identity
                self._agent_joined.set()
                break

        try:
            await asyncio.wait_for(
                self._agent_joined.wait(), timeout=self.config.agent_join_timeout
            )
        except TimeoutError as err:
            raise RuntimeError(
                f"agent {self.config.agent_name} did not join within {self.config.agent_join_timeout}s — "
                "is the agent worker running? Check launchctl print gui/$(id -u)/ai.openclaw.livekit-agent-*"
            ) from err
        print(green(f"[sim] agent joined: {self._agent_identity}"))

        # Give the agent a moment to set up its session and register handlers
        print(dim("[sim] waiting 3s for agent session to initialize..."))
        await asyncio.sleep(3)

    async def _run_conversation(self) -> None:
        """Run through the conversation script."""
        print(bold(f"\n{'─' * 60}"))
        print(bold("  Starting conversation"))
        print(bold(f"{'─' * 60}\n"))

        # Wait for agent's initial greeting (on_enter triggers generate_reply)
        print(dim("[sim] waiting for agent greeting..."))
        greeting = await self._wait_for_response(timeout=self.config.response_timeout)
        if greeting:
            print(green(f"  {self._agent_identity}: ") + greeting)
        else:
            print(yellow("  [no greeting received within timeout — continuing]"))

        print()

        for i, (message, expected) in enumerate(CONVERSATION, 1):
            print(dim(f"  [{i}/{len(CONVERSATION)}] {expected}"))
            print(cyan("  eric-test: ") + message)

            # Send the text via lk.chat topic
            await self.room.local_participant.send_text(
                message,
                topic="lk.chat",
            )

            # Wait for agent response
            response = await self._wait_for_response(timeout=self.config.response_timeout)
            if response:
                print(green(f"  {self._agent_identity}: ") + response)
            else:
                print(red(f"  [NO RESPONSE within {self.config.response_timeout}s]"))

            print()

            # Small pause between messages to feel natural
            await asyncio.sleep(1.5)

        print(bold(f"\n{'─' * 60}"))
        print(bold("  Conversation complete"))
        print(bold(f"{'─' * 60}"))
        logs_dir = os.environ.get("LIVEKIT_VOICE_LOGS", "$LIVEKIT_VOICE_LOGS")
        print(dim("\n  Check agent logs for tool call details:"))
        print(dim(f"    tail -100 {logs_dir}/agent-nyla.log"))
        print(dim(f"    tail -100 {logs_dir}/agent-aoi.log"))
        print()

    async def _wait_for_response(self, timeout: float) -> str | None:
        """Wait for the agent to finish its response."""
        self._response_chunks.clear()
        self._response_complete.clear()
        self._current_stream_id = None

        try:
            await asyncio.wait_for(self._response_complete.wait(), timeout=timeout)
            return "".join(self._response_chunks).strip()
        except TimeoutError:
            # Return whatever we got so far
            partial = "".join(self._response_chunks).strip()
            if partial:
                return partial + " [timeout — partial response]"
            return None

    def _on_participant_connected(self, participant: rtc.RemoteParticipant) -> None:
        print(dim(f"[sim] participant joined: {participant.identity} (kind={participant.kind})"))
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            self._agent_identity = participant.identity
            self._agent_joined.set()

    def _on_participant_disconnected(self, participant: rtc.RemoteParticipant) -> None:
        print(dim(f"[sim] participant left: {participant.identity}"))
        if participant.identity == self._agent_identity:
            # Agent left — signal any pending response wait
            self._response_complete.set()

    def _on_transcription_received(
        self,
        segments: list[rtc.TranscriptionSegment],
        participant: rtc.Participant | None,
        publication: rtc.TrackPublication | None,
    ) -> None:
        """Legacy path: publish_transcription() → transcription_received event."""
        identity = participant.identity if participant else "?"
        for seg in segments:
            print(dim(f"  [transcription] from={identity} final={seg.final} text={seg.text[:120]}"))
            if seg.text.strip():
                self._response_chunks.append(seg.text)
                if seg.final:
                    self._response_complete.set()

    def _on_text_stream(self, reader: rtc.TextStreamReader, participant_identity: str) -> None:
        """Text stream path: stream_text(topic="lk.transcription" or "lk.chat")."""
        topic = getattr(getattr(reader, "info", None), "topic", "?")
        print(dim(f"  [text-stream] topic={topic} from={participant_identity}"))
        task = asyncio.ensure_future(self._read_text_stream(reader, participant_identity))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _read_text_stream(
        self, reader: rtc.TextStreamReader, participant_identity: str
    ) -> None:
        """Read a text stream to completion, respecting lk.transcription_final."""
        try:
            text = await reader.read_all()
            # After read_all(), trailer attributes have been merged into reader.info
            attrs = reader.info.attributes or {}
            is_final = attrs.get("lk.transcription_final", "false") == "true"
            print(
                dim(
                    f"  [text-stream-read] got {len(text)} chars from {participant_identity} (final={is_final}): {text[:120]}"
                )
            )
            if text.strip():
                self._response_chunks.append(text)
            if is_final:
                # Agent is done with this turn — safe to send next message
                self._response_complete.set()
        except Exception as e:
            print(yellow(f"  [text-stream error: {e}]"))

    async def _cleanup(self) -> None:
        """Disconnect and delete the room."""
        if self._connected:
            print(dim("[sim] disconnecting..."))
            await self.room.disconnect()

        # Best-effort room deletion
        try:
            http_url = self.config.lk_url.replace("ws://", "http://").replace("wss://", "https://")
            lk_api = api.LiveKitAPI(
                url=http_url, api_key=self.config.api_key, api_secret=self.config.api_secret
            )
            await lk_api.room.delete_room(api.DeleteRoomRequest(room=self.room_name))
            await lk_api.aclose()
            print(dim(f"[sim] room {self.room_name} deleted"))
        except Exception:
            print(dim(f"[sim] room {self.room_name} cleanup failed (may already be gone)"))


# ── Main ─────────────────────────────────────────────────────────────


def parse_args() -> SimConfig:
    parser = argparse.ArgumentParser(description="Text-to-text agent simulation")
    parser.add_argument(
        "--agent", default="phone-nyla", help="Agent name to dispatch (default: phone-nyla)"
    )
    parser.add_argument("--lk-url", default="ws://127.0.0.1:7880", help="LiveKit server URL")
    parser.add_argument("--timeout", type=float, default=45.0, help="Response timeout in seconds")
    args = parser.parse_args()

    api_key = os.environ.get("LIVEKIT_API_KEY", "")
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "")
    if not api_key or not api_secret:
        print(red("[error] LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set"))
        sys.exit(1)

    return SimConfig(
        lk_url=args.lk_url,
        api_key=api_key,
        api_secret=api_secret,
        agent_name=args.agent,
        response_timeout=args.timeout,
    )


async def main() -> None:
    config = parse_args()
    sim = TextSimulator(config)
    await sim.run()


if __name__ == "__main__":
    asyncio.run(main())
