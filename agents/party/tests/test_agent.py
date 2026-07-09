"""Tests for the Party voice agent (chained STT/LLM/TTS, Harem World line)."""

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def agent_module():
    return importlib.import_module("agent")


class TestModuleExports:
    """Verify the agent module exports what LiveKit expects."""

    def test_server_exists(self, agent_module):
        assert hasattr(agent_module, "server")

    def test_entrypoint_exists(self, agent_module):
        assert hasattr(agent_module, "entrypoint")
        assert callable(agent_module.entrypoint)

    def test_agent_class_exists(self, agent_module):
        assert hasattr(agent_module, "PartyAgent")

    def test_server_is_agent_server(self, agent_module):
        from livekit.agents.worker import AgentServer

        assert isinstance(agent_module.server, AgentServer)


class TestAgentClass:
    """Verify the PartyAgent class composition."""

    def test_inherits_core_tools(self, agent_module):
        from tools.core import CoreToolsMixin

        assert issubclass(agent_module.PartyAgent, CoreToolsMixin)

    def test_inherits_memory_tools(self, agent_module):
        from tools.memory import MemoryToolsMixin

        assert issubclass(agent_module.PartyAgent, MemoryToolsMixin)

    def test_does_not_inherit_sessions_tools(self, agent_module):
        """Retired with the OpenClaw gateway."""
        import pytest

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("tools.sessions")

    def test_config_is_nyla_identity(self, agent_module):
        """Harem World line uses Nyla's operational identity — same person,
        different voice engine."""
        cfg = agent_module.PartyAgent.config
        assert cfg.agent_name == "nyla"
        assert cfg.memory_agent_tag == "nyla-voice"
        assert cfg.discord_room.startswith("channel:")

    def test_construction_with_defaults(self, agent_module):
        agent = agent_module.PartyAgent(instructions="test")
        assert agent._caller_from is None

    def test_does_not_inherit_household_tools(self, agent_module):
        """Party is a voice channel, not a surveying persona —
        she uses musubi_recent (self-only) and does not have
        household_status."""
        from tools.household import HouseholdToolsMixin

        assert not issubclass(agent_module.PartyAgent, HouseholdToolsMixin)

    def test_household_status_absent(self, agent_module):
        agent = agent_module.PartyAgent(instructions="test")
        assert not hasattr(agent, "household_status")

    def test_active_tools_present(self, agent_module):
        """Tools currently exposed to the voice model."""
        agent = agent_module.PartyAgent(instructions="test")
        expected = [
            "get_current_time",
            "get_weather",
            "musubi_recent",
            "musubi_remember",
        ]
        for tool in expected:
            assert hasattr(agent, tool), f"Missing tool: {tool}"

    def test_chained_llm_model_is_current_gemini_lite(self, agent_module):
        assert agent_module._CHAINED_LLM_MODEL == "gemini-3.1-flash-lite-preview"

    def test_retired_gateway_tools_absent(self, agent_module):
        """The OpenClaw gateway is gone. A prompt that promises a tool the
        runtime does not register is a fabrication generator."""
        agent = agent_module.PartyAgent(instructions="test")
        for name in (
            "openclaw_request",
            "openclaw_delegate",
            "sessions_send",
            "sessions_spawn",
            "schedule_callback",
        ):
            assert getattr(agent, name, None) is None, f"{name} is back"


class TestPersona:
    """Verify persona loading from prompts/system.md."""

    def test_prompt_file_exists(self):
        prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "system.md"
        assert prompt_path.exists(), f"Persona file missing: {prompt_path}"

    def test_prompt_file_not_empty(self):
        prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "system.md"
        content = prompt_path.read_text(encoding="utf-8").strip()
        assert len(content) > 100, "Persona file seems too short"

    def test_prompt_does_not_route_to_household_status(self):
        """Party doesn't compose HouseholdToolsMixin; household queries
        fall back to self-only musubi_recent."""
        prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "system.md"
        content = prompt_path.read_text(encoding="utf-8")
        assert "household_status()" not in content, (
            "Party prompt must not reference household_status"
        )

    def test_prompt_keeps_musubi_recent(self):
        prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "system.md"
        content = prompt_path.read_text(encoding="utf-8")
        assert "musubi_recent()" in content, "Prompt must still reference musubi_recent"

    def test_load_persona_function(self, agent_module):
        persona = agent_module._load_persona()
        assert isinstance(persona, str)
        assert len(persona) > 100


class TestSDKImports:
    """Verify SDK dependencies import cleanly."""

    def test_import_env(self):
        from sdk.env import load_env

        assert callable(load_env)

    def test_import_telephony(self):
        from sdk.telephony import resolve_caller

        assert callable(resolve_caller)

    def test_import_trace(self):
        from sdk.trace import trace

        assert callable(trace)

    def test_import_transcript(self):
        from sdk.transcript import wire_transcript_logging

        assert callable(wire_transcript_logging)


class TestProviderImports:
    """Verify chained pipeline provider imports."""

    def test_import_openai_stt(self):
        from livekit.plugins import openai as openai_plugin

        assert openai_plugin is not None

    def test_import_silero_vad(self):
        from livekit.plugins import silero as silero_plugin

        assert silero_plugin is not None

    def test_import_google_llm(self):
        from livekit.plugins import google as google_plugin

        assert google_plugin is not None

    def test_import_elevenlabs_tts(self):
        from livekit.plugins import elevenlabs as elevenlabs_plugin

        assert elevenlabs_plugin is not None

    def test_import_end_call_tool(self):
        from livekit.agents.beta import EndCallTool

        assert EndCallTool is not None
