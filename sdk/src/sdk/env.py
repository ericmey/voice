"""dev-mode .env loading for the agent process.

Loads ``./.env`` (next to the agent script) for agent-specific knobs.
In production, the launchd plist exports all vars directly before
spawning the worker, so .env files are a dev-mode convenience.

Also wires OTel tracing for an OTLP/HTTP backend if
``VOICE_OTEL_ENABLED=true`` — the
hook lives here because every agent calls ``load_env()`` at module
top, before LiveKit's ``AgentServer`` is instantiated. LiveKit caches
the tracer provider at server-construction time; missing this window
means traces won't flow.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from sdk.tracing import setup_otel_tracing


def load_env() -> None:
    """Load agent-local ``./.env`` and wire tracing.

    The ElevenLabs key alias (``ELEVENLABS_API_KEY`` -> ``ELEVEN_API_KEY``) lived here and
    was removed 2026-07-11: no agent depends on the ElevenLabs plugin. aoi/nyla/yua are
    ``livekit-agents[google]``; sumi is ``[openai,silero]`` + nvidia. The alias was
    forwarding a credential to a plugin that is not installed.
    """
    load_dotenv(Path.cwd() / ".env")

    # No-op when VOICE_OTEL_ENABLED is unset or false — keeps unit
    # tests and CI hermetic. Idempotent across re-imports.
    setup_otel_tracing()
