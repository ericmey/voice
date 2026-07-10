"""Verify all public SDK modules import cleanly."""

import importlib

import pytest


def test_import_tools_package():
    from tools import CoreToolsMixin, MusubiToolsMixin  # noqa: F401


def test_import_env():
    from sdk.env import load_env  # noqa: F401


def test_import_trace():
    from sdk.trace import trace  # noqa: F401


def test_import_transcript():
    from sdk.transcript import wire_transcript_logging  # noqa: F401


@pytest.mark.parametrize(
    "module",
    [
        "sdk.gateway_client",
        "sdk.cli_spawner",
        "sdk.openclaw_hooks",
        "tools.sessions",
        "sdk.constants",  # only held Discord constants nothing read
    ],
)
def test_gateway_modules_are_gone(module):
    """Retired. Re-adding any of these should be deliberate, not accidental."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)
