"""`make health` must go RED when an agent is not actually serving.

This file exists because `scripts/health-check.sh` had never been executed by the
gate — not once. It is the script that answers "are the four of them alive," it is
what cron runs, it is what I quoted to Eric as proof the fleet was healthy, and
nothing verified it could say "no".

It could not. At the reviewed head it checked three things: the container is
running, SOME registration line exists anywhere in the container's whole log, and
the restart count is low. So:

- An agent that registered and then WEDGED — running, health=unhealthy, not
  answering its endpoint — reported ``ok``. Forever. And Docker does not save you:
  ``restart: unless-stopped`` acts on container EXIT, not on health status, so
  nothing restarts it either. Unreported AND unrecovered.

- A registration line from BEFORE a crash-and-restart still satisfied the proof,
  because ``docker logs`` with no ``--since`` returns the container's entire life.
  A worker that never re-registered after its last start looked identical to one
  that had.

Both are the same class of defect: THE INSTRUMENT COULD NOT REPORT THE FAILURE IT
EXISTED TO REPORT. So these tests do not assert the script's internals — they run
the real script against a mocked Docker and require it to go red. A monitor with
no red path is not a monitor.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HEALTH_CHECK = REPO_ROOT / "scripts" / "health-check.sh"

AGENTS = ("nyla", "aoi", "yua", "sumi")

# A registration line in the shape the script greps for.
REGISTERED = 'INFO livekit.agents - registered worker {"id": "AW_abc123"}'

STARTED_AT = "2026-07-11T18:00:00.000000000Z"


def _healthy_fleet() -> dict:
    """Every service up, every agent healthy and registered since its start."""
    return {
        "containers": {
            name: {"running": True, "health": "healthy", "restarts": 0, "started": STARTED_AT}
            for name in [f"voice-agent-{a}" for a in AGENTS]
            + ["voice-redis", "voice-livekit-server", "voice-livekit-sip", "voice-livekit-egress"]
        },
        # Logs the script sees when it asks for lines since the current start.
        "logs_since_start": {f"voice-agent-{a}": REGISTERED for a in AGENTS},
        # Logs from the container's whole life — what the OLD check read.
        "logs_all": {f"voice-agent-{a}": REGISTERED for a in AGENTS},
        "redis_ping": "PONG",
        "trunk_present": 1,
        # The live dispatch state — who LiveKit will actually route a call to.
        "live_dispatch_agents": [f"phone-{a}" for a in AGENTS],
    }


# The fake docker. It answers exactly the inspect/logs/exec calls the script makes,
# from a scenario file. If the script ever asks docker something this does not
# understand, we exit non-zero rather than silently returning empty — an
# accommodating mock is how a test certifies a script that does not work.
FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys

scenario = json.loads(open(os.environ["HEALTH_SCENARIO"]).read())
argv = sys.argv[1:]

def die(msg):
    sys.stderr.write("fake-docker: unhandled: %s (%s)\n" % (msg, " ".join(argv)))
    sys.exit(97)

if argv[:1] == ["ps"]:
    sys.exit(0)

if argv[:1] == ["inspect"]:
    fmt, name = argv[2], argv[3]
    c = scenario["containers"].get(name)
    if c is None:
        sys.exit(1)  # docker's own behaviour for an unknown container
    if ".State.Running" in fmt:
        print("true" if c["running"] else "false")
    elif ".State.Health" in fmt:
        print(c.get("health") or "none")
    elif ".RestartCount" in fmt:
        print(c["restarts"])
    elif ".State.StartedAt" in fmt:
        print(c["started"])
    else:
        die("inspect format")
    sys.exit(0)

if argv[:1] == ["logs"]:
    if "--since" in argv:
        name = argv[-1]
        print(scenario["logs_since_start"].get(name, ""))
    else:
        name = argv[-1]
        print(scenario["logs_all"].get(name, ""))
    sys.exit(0)

if argv[:1] == ["exec"]:
    if "redis-cli" in argv and "ping" in argv:
        print(scenario["redis_ping"]); sys.exit(0)
    if "redis-cli" in argv and "exists" in argv:
        print(scenario["trunk_present"]); sys.exit(0)
    if "redis-cli" in argv and "hvals" in argv:
        # LiveKit stores these protobuf-encoded; the agentName literals are legible
        # inside the blob, which is what the script greps for.
        for name in scenario["live_dispatch_agents"]:
            print('\\x12\\x05phone%s"{"route":"default"}' % name.replace("phone", ""))
        sys.exit(0)
    if "wget" in argv:
        sys.exit(0)  # livekit-server :7880 answers
    die("exec")

die("verb")
"""


@pytest.fixture
def run_health_check(tmp_path):
    """Run the REAL scripts/health-check.sh against a mocked docker."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def _run(scenario: dict) -> tuple[int, dict]:
        spec = tmp_path / "scenario.json"
        spec.write_text(json.dumps(scenario))
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HEALTH_SCENARIO": str(spec),
        }
        proc = subprocess.run(
            ["bash", str(HEALTH_CHECK), "--json"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert "fake-docker: unhandled" not in proc.stderr, (
            f"the script asked docker something the mock does not model — the test is "
            f"lying about coverage:\n{proc.stderr}"
        )
        return proc.returncode, json.loads(proc.stdout)

    return _run


def _check(report: dict, name: str) -> dict:
    return next(c for c in report["checks"] if c["name"] == name)


def test_a_healthy_fleet_is_green(run_health_check):
    """The control. Without this, every red below could be red for the wrong reason."""
    code, report = run_health_check(_healthy_fleet())
    assert code == 0, report
    assert report["failed"] == 0
    for a in AGENTS:
        assert _check(report, f"agent-{a}")["status"] == "ok"


@pytest.mark.parametrize("wedged", AGENTS)
def test_a_running_but_unhealthy_agent_is_reported(run_health_check, wedged):
    """THE BUG. She is up. She registered. She is not answering. She was reported ok.

    This is the exact shape of the ``$AGENT_PORT`` drift I shipped and caught on
    deploy: the process is fine, the health endpoint is not reachable, Docker marks
    her unhealthy — and then does NOTHING, because restart policies fire on exit,
    not on health. If `make health` also says nothing, the outage is invisible from
    both sides and we find out when Eric's call rings into silence.
    """
    scenario = _healthy_fleet()
    scenario["containers"][f"voice-agent-{wedged}"]["health"] = "unhealthy"

    code, report = run_health_check(scenario)

    assert code == 1, f"{wedged} is UNHEALTHY and `make health` exited 0"
    assert _check(report, f"agent-{wedged}")["status"] == "fail"
    detail = _check(report, f"agent-{wedged}")["detail"].lower()
    assert "unhealthy" in detail

    for other in AGENTS:
        if other != wedged:
            assert _check(report, f"agent-{other}")["status"] == "ok", (
                "one sister's wedge must not smear the others — a monitor that goes "
                "red everywhere at once is a monitor nobody reads"
            )


def test_an_agent_with_no_healthcheck_is_reported(run_health_check):
    """health=none was the state of ALL FOUR until yesterday, and nothing said so.

    LiveKit had been serving a health endpoint on each agent's port the entire time.
    Nobody asked it anything. If a compose edit ever drops the healthcheck again, the
    fleet must not quietly return to being unobservable.
    """
    scenario = _healthy_fleet()
    scenario["containers"]["voice-agent-sumi"]["health"] = "none"

    code, report = run_health_check(scenario)

    assert code == 1
    assert _check(report, "agent-sumi")["status"] == "fail"
    assert "healthcheck" in _check(report, "agent-sumi")["detail"].lower()


def test_a_stale_registration_does_not_prove_the_current_process_registered(run_health_check):
    """She registered in a PREVIOUS life, crashed, came back, and never re-registered.

    ``docker logs`` with no ``--since`` returns the container's entire history, so the
    old check found that ancient line and called her registered. She is not on
    LiveKit's roster. Calls do not route to her. The log still says she is fine.

    An unbounded log read is not evidence about the process running NOW.
    """
    scenario = _healthy_fleet()
    scenario["containers"]["voice-agent-yua"]["restarts"] = 1
    # The proof exists — but only from before the restart.
    scenario["logs_all"]["voice-agent-yua"] = REGISTERED
    scenario["logs_since_start"]["voice-agent-yua"] = ""

    code, report = run_health_check(scenario)

    assert code == 1, (
        "yua has not registered since her current start — she is not on the roster — "
        "and `make health` exited 0 on the strength of a log line from her last life"
    )
    assert _check(report, "agent-yua")["status"] == "fail"
    assert "registration" in _check(report, "agent-yua")["detail"].lower()


def test_a_starting_agent_is_not_a_failure(run_health_check):
    """Prewarm loads Silero VAD per job process. `starting` is not `unhealthy`.

    A monitor that screams during every deploy is a monitor that gets muted, and a
    muted monitor is the one that misses the real outage.
    """
    scenario = _healthy_fleet()
    scenario["containers"]["voice-agent-nyla"]["health"] = "starting"

    code, report = run_health_check(scenario)

    assert code == 0, report
    assert _check(report, "agent-nyla")["status"] == "ok"


def test_a_crash_looping_agent_is_reported(run_health_check):
    scenario = _healthy_fleet()
    scenario["containers"]["voice-agent-aoi"]["restarts"] = 99

    code, report = run_health_check(scenario)

    assert code == 1
    assert _check(report, "agent-aoi")["status"] == "fail"


def test_a_stopped_agent_is_reported(run_health_check):
    scenario = _healthy_fleet()
    scenario["containers"]["voice-agent-sumi"]["running"] = False

    code, report = run_health_check(scenario)

    assert code == 1
    assert _check(report, "agent-sumi")["status"] == "fail"


# --- SIP routing: who actually picks up -------------------------------------------


@pytest.mark.parametrize("dropped", AGENTS)
def test_a_sister_missing_from_live_dispatch_is_reported(run_health_check, dropped):
    """The old check asked "does the routing table exist". It does. She is not in it.

    Three rules instead of four passes ``redis-cli exists``. So does four rules that all
    point at Nyla. The call to the missing sister does not error — it reaches no agent, or
    reaches the wrong one, and the monitor stays green through all of it.
    """
    scenario = _healthy_fleet()
    scenario["live_dispatch_agents"] = [f"phone-{a}" for a in AGENTS if a != dropped]

    code, report = run_health_check(scenario)

    assert code == 1, f"phone-{dropped} is not in the live dispatch and `make health` exited 0"
    routing = _check(report, "sip-routing")
    assert routing["status"] == "fail"
    assert f"phone-{dropped}" in routing["detail"]


def test_a_missing_inbound_trunk_is_reported(run_health_check):
    """No trunk means no call reaches us at all."""
    scenario = _healthy_fleet()
    scenario["trunk_present"] = 0

    code, report = run_health_check(scenario)

    assert code == 1
    assert _check(report, "sip-trunk")["status"] == "fail"
