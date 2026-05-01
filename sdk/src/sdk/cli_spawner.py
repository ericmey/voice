"""openclaw CLI spawner — fire-and-forget subprocess for tool methods.

Python equivalent of vcr's fireAndForgetArgs(): find the openclaw binary,
spawn it with an explicit argv list (no shell, no injection surface),
fully detach so the phone call ending doesn't kill the subprocess.

Reaps child processes via a ``SIGCHLD`` handler so exited children don't
accumulate as zombies over a long-running agent lifetime.

Side-effect gate: setting ``OPENCLAW_VOICE_TOOLS_DRY_RUN=1`` turns
``fire_and_forget`` into a no-op that just logs the would-be argv. Use
this in integration tests that exercise tool wiring without sending
real Discord messages, scheduling real cron jobs, or kicking off real
agent sessions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import types

logger = logging.getLogger("openclaw-livekit.agent")


# --- zombie reaper ------------------------------------------------------
# fire_and_forget spawns detached subprocesses that we never wait() on.
# Without reaping, exited children become zombies. A SIGCHLD handler
# non-blockingly collects their status.
# ------------------------------------------------------------------------
def _reap_children(signum: int, frame: object) -> None:
    """Non-blocking child reaper — call repeatedly until no more zombies."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
        except ChildProcessError:
            break


def _install_sigchld_handler() -> None:
    """Best-effort SIGCHLD registration that preserves an existing handler."""
    if not hasattr(signal, "SIGCHLD"):
        return

    try:
        previous_handler = signal.getsignal(signal.SIGCHLD)

        if callable(previous_handler) and previous_handler is not _reap_children:

            def _chained_reap_children(signum: int, frame: types.FrameType | None) -> None:
                _reap_children(signum, frame)
                previous_handler(signum, frame)

            signal.signal(signal.SIGCHLD, _chained_reap_children)
        elif previous_handler is not _reap_children:
            signal.signal(signal.SIGCHLD, _reap_children)
    except (OSError, ValueError):
        # Not available in this runtime or registration is disallowed here.
        pass


_install_sigchld_handler()

#: Env var name — set to a truthy value ("1", "true", "yes") to suppress
#: all CLI actuations. Defined here (not hardcoded in checks) so callers
#: and tests can import the name instead of duplicating the string.
DRY_RUN_ENV = "OPENCLAW_VOICE_TOOLS_DRY_RUN"

_openclaw_bin_cache: str | None = None


def _resolve_openclaw_bin() -> str:
    """Locate the ``openclaw`` CLI. Env var wins, then PATH, then bare name."""
    global _openclaw_bin_cache
    if _openclaw_bin_cache is not None:
        return _openclaw_bin_cache
    env_path = os.environ.get("OPENCLAW_BIN")
    if env_path:
        _openclaw_bin_cache = env_path
        return env_path
    which = shutil.which("openclaw")
    if which:
        _openclaw_bin_cache = which
        return which
    _openclaw_bin_cache = "openclaw"
    return _openclaw_bin_cache


def is_dry_run() -> bool:
    """Is the voice-tools dry-run gate active right now?

    Read the env var each call — tests may set/clear it between cases.
    """
    return os.environ.get(DRY_RUN_ENV, "").strip().lower() in {"1", "true", "yes"}


def fire_and_forget(args: list[str]) -> None:
    """Spawn ``openclaw <args...>`` fully detached. Raises on spawn failure.

    Spawn failure (FileNotFoundError when the binary can't be found, or
    PermissionError on a non-executable) IS raised. Tool callers MUST wrap
    in try/except and return an honest error to the model.

    When the ``OPENCLAW_VOICE_TOOLS_DRY_RUN`` env var is set, this logs
    the would-be argv and returns without spawning — the tool caller
    sees success and produces a normal response. The point is that
    tests and local debugging can exercise the full tool path without
    sending real Discord messages or creating real cron jobs.
    """
    if is_dry_run():
        logger.info(
            "[voice-tools][DRY RUN] would spawn: openclaw %s",
            " ".join(args),
        )
        return

    bin_path = _resolve_openclaw_bin()
    subprocess.Popen(
        [bin_path, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    logger.info("[voice-tools] spawned: openclaw %s", " ".join(args[:3]))


async def fire_and_forget_async(args: list[str]) -> None:
    """Async subprocess spawn for tool methods running on the voice event loop."""
    await asyncio.to_thread(fire_and_forget, args)
