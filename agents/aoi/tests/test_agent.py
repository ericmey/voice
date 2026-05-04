"""Tests for the Aoi voice agent (Gemini 2.5 Flash Live)."""

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
        assert hasattr(agent_module, "AoiAgent")

    def test_server_is_agent_server(self, agent_module):
        from livekit.agents.worker import AgentServer

        assert isinstance(agent_module.server, AgentServer)


class TestAgentClass:
    """Verify the AoiAgent class is properly composed."""

    def test_inherits_core_tools(self, agent_module):
        from tools.core import CoreToolsMixin

        assert issubclass(agent_module.AoiAgent, CoreToolsMixin)

    def test_inherits_memory_tools(self, agent_module):
        from tools.memory import MemoryToolsMixin

        assert issubclass(agent_module.AoiAgent, MemoryToolsMixin)

    def test_inherits_sessions_tools(self, agent_module):
        from tools.sessions import SessionsToolsMixin

        assert issubclass(agent_module.AoiAgent, SessionsToolsMixin)

    def test_inherits_academy_tools(self, agent_module):
        from tools.academy import AcademyToolsMixin

        assert issubclass(agent_module.AoiAgent, AcademyToolsMixin)

    def test_construction_with_defaults(self, agent_module):
        agent = agent_module.AoiAgent(instructions="test")
        assert agent._caller_from is None

    def test_construction_with_caller(self, agent_module):
        agent = agent_module.AoiAgent(
            instructions="test",
            caller_from="+13175551234",
        )
        assert agent._caller_from == "+13175551234"

    def test_inherits_household_tools(self, agent_module):
        from tools.household import HouseholdToolsMixin

        assert issubclass(agent_module.AoiAgent, HouseholdToolsMixin)

    def test_household_status_tool_present(self, agent_module):
        agent = agent_module.AoiAgent(instructions="test")
        assert hasattr(agent, "household_status")

    def test_active_tools_present(self, agent_module):
        """Tools currently exposed to the voice model. schedule_callback
        is deliberately OFF this list — the cron path isn't wired; see
        SDK TODO.md for the re-enable plan."""
        agent = agent_module.AoiAgent(instructions="test")
        expected = [
            "get_current_time",
            "get_weather",
            "musubi_recent",
            "musubi_remember",
            "sessions_send",
            "sessions_spawn",
            "academy_selfie",
            "academy_send",
            "household_status",
        ]
        for tool in expected:
            assert hasattr(agent, tool), f"Missing tool: {tool}"

    def test_openclaw_request_absent(self, agent_module):
        agent = agent_module.AoiAgent(instructions="test")
        attr = getattr(agent, "openclaw_request", None)
        assert not callable(attr), "openclaw_request was deleted in SDK cleanup"

    def test_config_is_aoi_identity(self, agent_module):
        """Aoi's config must tag memories to aoi-voice and set her own agent name."""
        cfg = agent_module.AoiAgent.config
        assert cfg.agent_name == "aoi"
        assert cfg.memory_agent_tag == "aoi-voice"
        assert cfg.discord_room.startswith("channel:")

    def test_config_delegation_allowlist_matches_persona(self, agent_module):
        """Aoi's prompt commits to specific default routing — research to
        Yumi, ops to Rin, code to herself, handoff to Nyla. Inbox / email
        is deliberately OFF the list (those conversations belong with
        Nyla, who routes them to Momo). Creative/image work (Hana, Tama)
        is also off so she's forced to route those back through Nyla
        instead of doing them."""
        cfg = agent_module.AoiAgent.config
        allowed = cfg.allowed_delegation_targets
        assert allowed is not None, "Aoi should have a bounded delegation set"
        assert {"yumi", "rin", "aoi", "nyla"} <= allowed
        assert "momo" not in allowed
        assert "hana" not in allowed
        assert "tama" not in allowed


class TestPersona:
    """Verify persona loading from prompts/system.md."""

    def test_prompt_file_exists(self):
        prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "system.md"
        assert prompt_path.exists(), f"Persona file missing: {prompt_path}"

    def test_prompt_file_not_empty(self):
        prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "system.md"
        content = prompt_path.read_text(encoding="utf-8").strip()
        assert len(content) > 100, "Persona file seems too short"

    def test_prompt_contains_aoi_identity(self):
        """Aoi's prompt must establish her own identity."""
        prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "system.md"
        content = prompt_path.read_text(encoding="utf-8")
        assert "You are Aoi" in content, "Persona must establish Aoi's identity"

    def test_prompt_routes_household_to_household_status(self):
        prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "system.md"
        content = prompt_path.read_text(encoding="utf-8")
        assert "household_status()" in content, (
            "Prompt must route household queries to household_status"
        )
        assert "household_status" in content.split("## Call Flow")[1], (
            "Call Flow must reference household_status for cross-agent queries"
        )

    def test_prompt_keeps_musubi_recent_for_self(self):
        prompt_path = Path(__file__).resolve().parent.parent / "prompts" / "system.md"
        content = prompt_path.read_text(encoding="utf-8")
        assert "musubi_recent()" in content, (
            "Prompt must still reference musubi_recent for self queries"
        )

    def test_load_persona_function(self):
        from _shared import load_persona

        persona = load_persona()
        assert isinstance(persona, str)
        assert len(persona) > 100


class TestSDKImports:
    """Verify SDK dependencies import cleanly."""

    def test_import_core_tools(self):
        from tools.core import CoreToolsMixin

        assert CoreToolsMixin is not None

    def test_import_memory_tools(self):
        from tools.memory import MemoryToolsMixin

        assert MemoryToolsMixin is not None

    def test_import_sessions_tools(self):
        from tools.sessions import SessionsToolsMixin

        assert SessionsToolsMixin is not None

    def test_import_academy_tools(self):
        from tools.academy import AcademyToolsMixin

        assert AcademyToolsMixin is not None


class TestProviderImports:
    """Verify Gemini provider imports for this agent."""

    def test_import_google_plugin(self):
        from livekit.plugins import google as google_plugin

        assert google_plugin is not None

    def test_import_google_search(self):
        from livekit.plugins.google.tools import GoogleSearch

        assert GoogleSearch is not None

    def test_import_gemini_types(self):
        from google.genai import types as genai_types

        assert genai_types is not None

    def test_native_audio_model_is_current_preview(self):
        from tools.base_agent import GEMINI_NATIVE_AUDIO_MODEL

        assert GEMINI_NATIVE_AUDIO_MODEL == "gemini-2.5-flash-native-audio-preview-12-2025"

    def test_import_end_call_tool(self):
        from livekit.agents.beta import EndCallTool

        assert EndCallTool is not None
