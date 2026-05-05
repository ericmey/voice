"""Tests for cli_spawner — dry-run gate + binary resolution."""

from __future__ import annotations

import pytest

from sdk import cli_spawner


@pytest.fixture
def no_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the dry-run env var is absent (some dev shells set it)."""
    monkeypatch.delenv(cli_spawner.DRY_RUN_ENV, raising=False)


def test_dry_run_env_var_name_matches_docs():
    """The exported name is the one the docs tell users to set."""
    assert cli_spawner.DRY_RUN_ENV == "OPENCLAW_VOICE_TOOLS_DRY_RUN"


def test_is_dry_run_false_when_unset(monkeypatch: pytest.MonkeyPatch, no_dry_run: None) -> None:
    assert cli_spawner.is_dry_run() is False


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "YES"])
def test_is_dry_run_true_for_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(cli_spawner.DRY_RUN_ENV, value)
    assert cli_spawner.is_dry_run() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe"])
def test_is_dry_run_false_for_non_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(cli_spawner.DRY_RUN_ENV, value)
    assert cli_spawner.is_dry_run() is False


def test_fire_and_forget_dry_run_does_not_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the dry-run env var set, subprocess.Popen is never called."""
    monkeypatch.setenv(cli_spawner.DRY_RUN_ENV, "1")

    calls: list = []

    def fake_popen(*args, **kwargs):  # pragma: no cover - must not run
        calls.append((args, kwargs))
        raise AssertionError("Popen should not be invoked in dry-run mode")

    monkeypatch.setattr(cli_spawner.subprocess, "Popen", fake_popen)

    cli_spawner.fire_and_forget(["message", "send", "--target", "x"])

    assert calls == []


def test_fire_and_forget_spawns_when_not_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path, no_dry_run: None
) -> None:
    """Default path — Popen IS invoked with the resolved binary + argv."""
    recorded: list = []
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("#!/bin/sh\n", encoding="utf-8")
    openclaw.chmod(0o755)

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            recorded.append((argv, kwargs))

    monkeypatch.setattr(cli_spawner.subprocess, "Popen", _FakePopen)
    monkeypatch.setenv("OPENCLAW_BIN", str(openclaw))
    # Invalidate the cached binary path so the env var wins this call.
    cli_spawner._openclaw_bin_cache = None

    cli_spawner.fire_and_forget(["message", "send", "--target", "x"])

    assert len(recorded) == 1
    argv, kwargs = recorded[0]
    assert argv[0] == str(openclaw)
    assert argv[1:] == ["message", "send", "--target", "x"]
    assert kwargs.get("start_new_session") is True


def test_fire_and_forget_rejects_relative_openclaw_bin(
    monkeypatch: pytest.MonkeyPatch, no_dry_run: None
) -> None:
    monkeypatch.setenv("OPENCLAW_BIN", "openclaw")
    cli_spawner._openclaw_bin_cache = None

    with pytest.raises(ValueError, match="absolute path"):
        cli_spawner.fire_and_forget(["agent", "--agent", "rin", "--message", "x"])


def test_fire_and_forget_rejects_world_writable_openclaw_bin(
    monkeypatch: pytest.MonkeyPatch, tmp_path, no_dry_run: None
) -> None:
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("#!/bin/sh\n", encoding="utf-8")
    openclaw.chmod(0o777)
    monkeypatch.setenv("OPENCLAW_BIN", str(openclaw))
    cli_spawner._openclaw_bin_cache = None

    try:
        with pytest.raises(PermissionError, match="world-writable"):
            cli_spawner.fire_and_forget(["agent", "--agent", "rin", "--message", "x"])
    finally:
        openclaw.chmod(0o755)


def test_fire_and_forget_rejects_unknown_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path, no_dry_run: None
) -> None:
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("#!/bin/sh\n", encoding="utf-8")
    openclaw.chmod(0o755)
    monkeypatch.setenv("OPENCLAW_BIN", str(openclaw))
    cli_spawner._openclaw_bin_cache = None

    with pytest.raises(ValueError, match="not allowed"):
        cli_spawner.fire_and_forget(["danger", "--message", "x"])


def test_fire_and_forget_rejects_nul_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path, no_dry_run: None
) -> None:
    openclaw = tmp_path / "openclaw"
    openclaw.write_text("#!/bin/sh\n", encoding="utf-8")
    openclaw.chmod(0o755)
    monkeypatch.setenv("OPENCLAW_BIN", str(openclaw))
    cli_spawner._openclaw_bin_cache = None

    with pytest.raises(ValueError, match="NUL"):
        cli_spawner.fire_and_forget(["agent", "--message", "bad\x00"])


@pytest.mark.asyncio
async def test_fire_and_forget_async_uses_worker_thread(
    monkeypatch: pytest.MonkeyPatch, no_dry_run: None
) -> None:
    recorded: list[list[str]] = []

    def fake_fire_and_forget(args: list[str]) -> None:
        recorded.append(args)

    monkeypatch.setattr(cli_spawner, "fire_and_forget", fake_fire_and_forget)

    await cli_spawner.fire_and_forget_async(["message", "send", "--target", "x"])

    assert recorded == [["message", "send", "--target", "x"]]
