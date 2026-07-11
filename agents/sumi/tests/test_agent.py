"""Tests for the Sumi voice agent (chained STT/LLM/TTS, Harem World line)."""

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
        assert hasattr(agent_module, "SumiAgent")

    def test_server_is_agent_server(self, agent_module):
        from livekit.agents.worker import AgentServer

        assert isinstance(agent_module.server, AgentServer)


class TestAgentClass:
    """Verify the SumiAgent class composition."""

    def test_inherits_core_tools(self, agent_module):
        from tools.core import CoreToolsMixin

        assert issubclass(agent_module.SumiAgent, CoreToolsMixin)

    def test_inherits_memory_tools(self, agent_module):
        from tools.memory import MusubiToolsMixin

        assert issubclass(agent_module.SumiAgent, MusubiToolsMixin)

    def test_does_not_inherit_sessions_tools(self, agent_module):
        """Retired with the OpenClaw gateway."""
        import pytest

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("tools.sessions")

    def test_config_has_own_memory_identity(self, agent_module):
        """Sumi's voice memory is her own channel (sumi/voice / sumi-voice),
        distinct from her fleet presence (sumi/hermes). One Sumi, two
        channels."""
        cfg = agent_module.SumiAgent.config
        assert cfg.agent_name == "sumi"
        assert cfg.memory_agent_tag == "sumi-voice"
        assert cfg.musubi_v2_namespace == "sumi/voice"
        assert cfg.registration_name == "phone-sumi"

    def test_construction_with_defaults(self, agent_module):
        agent = agent_module.SumiAgent(instructions="test")
        assert agent._caller_from is None

    def test_household_status_absent(self, agent_module):
        agent = agent_module.SumiAgent(instructions="test")
        assert not hasattr(agent, "household_status")

    def test_active_tools_present(self, agent_module):
        """Tools currently exposed to the voice model."""
        agent = agent_module.SumiAgent(instructions="test")
        expected = [
            "get_current_time",
            "get_weather",
            "musubi_recent",
            "musubi_remember",
        ]
        for tool in expected:
            assert hasattr(agent, tool), f"Missing tool: {tool}"

    def test_sumi_local_endpoints_configured(self, agent_module):
        """Sumi's chain points at the fully-local inference services on mizuki:
        Riva ASR (:50051), Nemo/llama.cpp (:8090), Orpheus TTS (:5005)."""
        assert agent_module._RIVA_ASR_SERVER.endswith(":50051")
        assert "8090" in agent_module._NEMO_BASE_URL
        assert "5005" in agent_module._ORPHEUS_BASE_URL

    def test_retired_gateway_tools_absent(self, agent_module):
        """The OpenClaw gateway is gone. A prompt that promises a tool the
        runtime does not register is a fabrication generator."""
        agent = agent_module.SumiAgent(instructions="test")
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
        """The household tool is deleted; household queries
        fall back to self-only musubi_recent."""
        prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "system.md"
        content = prompt_path.read_text(encoding="utf-8")
        assert "household_status()" not in content, (
            "Sumi prompt must not reference household_status"
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

    def test_import_nvidia_stt(self):
        from livekit.plugins import nvidia as nvidia_plugin

        assert nvidia_plugin is not None

    def test_import_end_call_tool(self):
        from livekit.agents.beta import EndCallTool

        assert EndCallTool is not None
