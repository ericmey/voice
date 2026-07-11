"""The deploy gate must run from ANY shell — not only from the one I happened to be in.

`make verify-bearers` is what `make deploy` and `make cycle` hard-depend on. From a clean
shell it exited 2 with `uv: command not found`. The binary was right there in ~/.local/bin;
a non-interactive shell (`ssh host '<cmd>'`, cron, a CI runner) simply never reads the login
profile that puts it on PATH.

So the gate guarding the deploy could not itself run on the documented operator surface — and
the ONLY reason it ever appeared to work is that I hand-exported PATH in front of every ssh
command I typed, all day, dozens of times. I fixed it in my shell over and over and never
once in the product. The workaround became invisible to me *because* it was so habitual.

docs/AGENT-LESSONS.md, first entry, 2026-05-22, names this exact failure down to the PATH:
"Do not rely on an interactive shell's PATH." / "Health checks that fail from a clean shell
train operators to ignore them."

The lesson was already written at the top of the file I am meant to read before doing
non-trivial work here. Writing a lesson down is not the same as carrying it — so this file
is the part that carries it, because a test runs whether or not I remember. (Yua, round 6.)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "tool-path.sh"

# A shell with NO idea where anything lives: exactly what `ssh host 'make ...'` and cron give
# you. The ops scripts must cope with this or say why they cannot.
BARE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _clean_env(**extra: str) -> dict[str, str]:
    env = {
        "PATH": BARE_PATH,
        "HOME": os.environ["HOME"],
        "SHELL": "/bin/bash",
        **extra,
    }
    return env


def _run(script: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script, "bash", *args],
        capture_output=True,
        text=True,
        env=env or _clean_env(),
        cwd=REPO_ROOT,
        timeout=60,
    )


@pytest.mark.skipif(not shutil.which("uv"), reason="uv not installed in this environment")
def test_the_tool_bootstrap_finds_uv_with_no_login_path() -> None:
    """THE REGRESSION. A clean shell has no ~/.local/bin, and `uv` must still be found."""
    proc = _run(f'source "{LIB}"; ensure_tool uv; command -v uv')

    assert proc.returncode == 0, (
        f"the ops surface cannot find uv from a non-interactive shell — this is the exact "
        f"state in which `make verify-bearers` exited 2 and the deploy gate could not run:\n"
        f"{proc.stderr}"
    )
    assert proc.stdout.strip(), "ensure_tool returned success but uv is still not on PATH"


def test_a_genuinely_missing_tool_fails_explicitly_not_as_command_not_found() -> None:
    """`command not found` from three subshells down is not an error an operator can act on.

    It must name the tool, show where we looked, and say how to install it — and exit 78
    (EX_CONFIG: the environment is wrong, not the code).
    """
    proc = _run(f'source "{LIB}"; ensure_tool definitely-not-a-real-tool')

    assert proc.returncode == 78, f"expected EX_CONFIG (78), got {proc.returncode}"
    assert "definitely-not-a-real-tool not found" in proc.stderr
    assert "also searched" in proc.stderr, "the operator is not told where we looked"
    assert "non-interactive" in proc.stderr, (
        "the message does not name the most likely cause — that the tool IS installed and "
        "this shell never read the profile. That hint is the whole point."
    )


@pytest.mark.skipif(not shutil.which("uv"), reason="uv not installed in this environment")
def test_verify_bearers_runs_from_a_clean_shell() -> None:
    """Yua's exact repro, as a test: `cd ~/voice && make verify-bearers` in a bare shell.

    It must not die on the environment. It is allowed to fail on a MISSING SECRETS FILE — that
    is a real, explicit, actionable condition (exit 78) and it is what a dev checkout without
    rendered secrets legitimately has. What it must never do again is fall over on PATH.
    """
    proc = _run(
        'scripts/verify-bearers.sh "$1"',
        str(REPO_ROOT / "secrets" / "livekit-agents.env"),
    )

    assert "command not found" not in proc.stderr, (
        f"the deploy gate still cannot run from a clean shell:\n{proc.stderr}"
    )

    if proc.returncode == 78:
        # No rendered secrets in this checkout — the honest, explicit failure.
        assert "no rendered secrets file" in proc.stderr, proc.stderr
    else:
        assert proc.returncode == 0, proc.stderr


def test_no_ops_script_calls_uv_bare() -> None:
    """The class fix, asserted structurally.

    Fixing `verify-bearers.sh` alone would leave the next script free to make the same call.
    Every operator script that shells out to `uv` must first source the bootstrap — otherwise
    it works on my laptop, works over my ssh (because I keep typing the export), and fails for
    whoever runs it next.
    """
    # Exempt, each for a reason I VERIFIED rather than assumed — an unexplained skip list is
    # how a real offender hides among the excused:
    #
    #   agent-entrypoint.sh — runs INSIDE the container, where uv is at /usr/local/bin and on
    #       the image's default PATH (checked: `docker exec voice-agent-aoi command -v uv`).
    #       There is no login profile involved, so there is nothing to bootstrap.
    #   bootstrap.sh — this is the script that INSTALLS uv. It cannot require uv to find uv.
    exempt = {"agent-entrypoint.sh", "bootstrap.sh"}

    offenders: list[str] = []

    for script in sorted((REPO_ROOT / "scripts").glob("*.sh")):
        if script.name in exempt:
            continue
        body = script.read_text()
        if "uv run" not in body and "uv sync" not in body:
            continue
        if "tool-path.sh" not in body:
            offenders.append(script.name)

    assert not offenders, (
        f"these ops scripts call `uv` without sourcing scripts/lib/tool-path.sh: {offenders}. "
        f"They will die with `uv: command not found` in any non-interactive shell — ssh, cron, "
        f"CI — while working perfectly for whoever has it on their login PATH."
    )


def test_the_makefile_ops_path_does_not_call_uv_bare() -> None:
    """`make deploy` / `make cycle` / `make health` are the documented stable surface.

    They may not depend on the caller's PATH. (Developer targets — lint, typecheck, test,
    sync-venvs — are exempt: they are run from a dev shell by someone who just installed uv,
    and they do not gate a production deploy.)
    """
    makefile = (REPO_ROOT / "Makefile").read_text()

    ops_targets = ("verify-bearers", "health", "register-sip")
    lines = makefile.splitlines()

    offenders: list[str] = []
    current: str | None = None
    for line in lines:
        if line and not line[0].isspace() and ":" in line:
            current = line.split(":", 1)[0].strip()
        elif line.startswith("\t") and current in ops_targets:
            stripped = line.lstrip("\t@ ")
            if stripped.startswith(("uv ", "uv\t")):
                offenders.append(f"{current}: {stripped}")

    assert not offenders, (
        f"these operator targets call `uv` directly: {offenders}. They must go through a "
        f"script that bootstraps its own PATH, or they fail from a clean shell — which is "
        f"how the bearer gate that `make deploy` depends on came to be unrunnable."
    )
