"""Verify all public SDK modules import cleanly."""

import importlib

import pytest


def test_import_tools_package():
    from tools import CoreToolsMixin, MemoryToolsMixin  # noqa: F401


def test_import_env():
    from sdk.env import load_env  # noqa: F401


def test_import_trace():
    from sdk.trace import trace  # noqa: F401


def test_import_transcript():
    from sdk.transcript import wire_transcript_logging  # noqa: F401


def test_import_musubi_client():
    from sdk.musubi_client import async_embed_text  # noqa: F401


def test_import_constants():
    from sdk.constants import NYLA_DISCORD_ROOM  # noqa: F401


@pytest.mark.parametrize(
    "module",
    ["sdk.gateway_client", "sdk.cli_spawner", "sdk.openclaw_hooks", "tools.sessions"],
)
def test_gateway_modules_are_gone(module):
    """Retired with the OpenClaw gateway. Re-adding one should be deliberate."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)
