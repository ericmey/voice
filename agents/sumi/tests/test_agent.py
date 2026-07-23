"""Tests for the Sumi voice agent (Slice 2: identity/package fork of Party).

Load-bearing here: Sumi's identity is exactly her own, fails loud without a
prompt, selects only her Musubi token, and does NOT leak Party's config, token,
namespace, or greeting. The pipeline is the inherited chained SCAFFOLD; the
local swaps are Slices 3-5.
"""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENTRYPOINT = _REPO_ROOT / "scripts" / "agent-entrypoint.sh"


@pytest.fixture
def agent_module():
    return importlib.import_module("agent")


class TestModuleExports:
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


class TestIdentity:
    """Sumi's identity is exactly her own — and derived, not hand-typed."""

    def test_config_is_sumi(self, agent_module):
        cfg = agent_module.SumiAgent.config
        assert cfg.agent_name == "sumi"
        assert cfg.memory_agent_tag == "sumi-voice"
        assert cfg.musubi_v2_namespace == "sumi/voice"

    def test_registration_derives_phone_sumi(self, agent_module):
        assert agent_module.SUMI_CONFIG.registration_name == "phone-sumi"

    def test_inherits_core_and_memory_tools(self, agent_module):
        from tools.core import CoreToolsMixin
        from tools.memory import MusubiToolsMixin

        assert issubclass(agent_module.SumiAgent, CoreToolsMixin)
        assert issubclass(agent_module.SumiAgent, MusubiToolsMixin)

    def test_construction_with_defaults(self, agent_module):
        agent = agent_module.SumiAgent(instructions="test")
        assert agent._caller_from is None

    def test_active_tools_present(self, agent_module):
        agent = agent_module.SumiAgent(instructions="test")
        for tool in ("get_current_time", "get_weather", "musubi_recent", "musubi_remember"):
            assert hasattr(agent, tool), f"Missing tool: {tool}"

    def test_retired_gateway_tools_absent(self, agent_module):
        agent = agent_module.SumiAgent(instructions="test")
        for name in (
            "openclaw_request",
            "openclaw_delegate",
            "sessions_send",
            "sessions_spawn",
            "schedule_callback",
        ):
            assert getattr(agent, name, None) is None, f"{name} is back"


class TestNoPartyLeakage:
    """Zero bleed from the Party scaffold into Sumi's runtime identity."""

    def test_config_is_not_party(self, agent_module):
        cfg = agent_module.SUMI_CONFIG
        assert cfg.agent_name != "party"
        assert cfg.memory_agent_tag != "party-voice"
        assert cfg.musubi_v2_namespace != "party/voice"
        assert cfg.registration_name != "phone-party"

    def test_no_partyagent_symbol(self, agent_module):
        assert not hasattr(agent_module, "PartyAgent")

    def test_prompt_has_no_party_identity(self, agent_module):
        content = (Path(agent_module._PROMPTS_DIR) / "system.md").read_text(encoding="utf-8").lower()
        assert "party" not in content
        assert "nyla" not in content

    def test_greeting_is_sumi_not_party(self, agent_module):
        assert "what's going on" not in agent_module._GREETING.lower()
        assert agent_module._GREETING.strip() != ""


class TestPersonaFailLoud:
    """Missing OR empty prompt must fail loud — no generic fallback."""

    def test_prompt_file_exists(self, agent_module):
        assert (Path(agent_module._PROMPTS_DIR) / "system.md").exists()

    def test_prompt_not_empty_and_is_sumi(self, agent_module):
        content = (Path(agent_module._PROMPTS_DIR) / "system.md").read_text(encoding="utf-8")
        assert len(content.strip()) > 100
        low = content.lower()
        assert "sumi" in low
        assert "archive" in low  # her absolute: archive, never delete
        assert "musubi_recent()" in content
        assert "household_status()" not in content

    def test_load_persona_raises_on_missing(self, agent_module, tmp_path):
        with pytest.raises(RuntimeError):
            agent_module._load_persona(prompts_dir=tmp_path)  # no system.md in tmp_path

    def test_load_persona_raises_on_empty(self, agent_module, tmp_path):
        (tmp_path / "system.md").write_text("   \n", encoding="utf-8")
        with pytest.raises(RuntimeError):
            agent_module._load_persona(prompts_dir=tmp_path)

    def test_load_persona_ok_returns_sumi(self, agent_module):
        persona = agent_module._load_persona()
        assert isinstance(persona, str) and len(persona) > 100
        assert "Sumi" in persona


class TestEntrypointTokenSelection:
    """AGENT=sumi selects ONLY MUSUBI_V2_TOKEN_SUMI; missing it exits 78 even
    when a Party token is present (proven at the shell boundary)."""

    def _run(self, env_extra):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        env.update(env_extra)
        return subprocess.run(
            ["sh", str(_ENTRYPOINT)],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=20,
        )

    def test_missing_sumi_token_exits_78_even_with_party_token(self):
        r = self._run({"AGENT": "sumi", "MUSUBI_V2_TOKEN_PARTY": "party-bearer-should-not-count"})
        assert r.returncode == 78, f"expected exit 78, got {r.returncode}: {r.stderr}"
        assert "MUSUBI_V2_TOKEN_SUMI" in r.stderr  # selected Sumi's var, not Party's


class TestSDKImports:
    def test_import_config(self):
        from sdk.config import AgentConfig, assert_agent_identity

        assert callable(assert_agent_identity) and AgentConfig is not None

    def test_import_env(self):
        from sdk.env import load_env

        assert callable(load_env)


class TestProviderImports:
    """Chained SCAFFOLD providers still import (compile/test surface)."""

    def test_scaffold_plugins_import(self):
        from livekit.agents.beta import EndCallTool
        from livekit.plugins import elevenlabs, google, openai, silero

        assert all(x is not None for x in (openai, silero, google, elevenlabs, EndCallTool))


# keep sys import referenced for conftest path insertion clarity
assert sys is not None
