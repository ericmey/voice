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
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HEALTH_CHECK = REPO_ROOT / "scripts" / "health-check.sh"

AGENTS = ("nyla", "aoi", "yua", "sumi")

# The DIDs in the tracked examples — the candidate set health compares the live table to.
DID = {
    "nyla": "+15550000001",
    "aoi": "+15550000002",
    "sumi": "+15550000003",
    "yua": "+15550000004",
}


def _live_rule(agent: str, *, name: str | None = None, dids: list[str] | None = None) -> dict:
    """One rule as LiveKit reports it."""
    return {
        "sipDispatchRuleId": f"SDR_{agent}",
        "name": name or f"twilio-to-phone-{agent}",
        "numbers": dids if dids is not None else [DID[agent]],
        "roomConfig": {"agents": [{"agentName": f"phone-{agent}"}]},
    }


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
        # The live dispatch table, as `lk sip dispatch list --json` returns it.
        "live_rules": [_live_rule(a) for a in AGENTS],
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
    if "wget" in argv:
        sys.exit(0)  # livekit-server :7880 answers
    die("exec")

die("verb")
"""


FAKE_LK = r"""#!/usr/bin/env python3
import json, os, sys

scenario = json.loads(open(os.environ["HEALTH_SCENARIO"]).read())
if sys.argv[1:4] == ["sip", "dispatch", "list"]:
    # Print a COMPLETE, VALID document and then fail — the exact shape that slipped past a
    # pipeline whose status came only from the comparator.
    print(json.dumps({"items": scenario["live_rules"]}))
    sys.exit(scenario.get("lk_exit", 0))
sys.stderr.write("fake-lk: unhandled: " + " ".join(sys.argv[1:]) + chr(10))
sys.exit(97)
"""


@pytest.fixture
def run_health_check(tmp_path):
    """Run the REAL scripts/health-check.sh — real comparator, mocked docker + lk."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("lk", FAKE_LK)):
        fake = bin_dir / name
        fake.write_text(body)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    # The candidate set health compares against: the tracked examples.
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for agent in AGENTS:
        src = REPO_ROOT / "config" / f"sip-dispatch-{agent}.json.example"
        shutil.copy(src, config_dir / f"sip-dispatch-{agent}.json")

    secrets = tmp_path / "livekit-agents.env"
    secrets.write_text("LIVEKIT_API_KEY=devkey\nLIVEKIT_API_SECRET=devsecret\n")

    def _run(scenario: dict, **overrides: str) -> tuple[int, dict]:
        spec = tmp_path / "scenario.json"
        spec.write_text(json.dumps(scenario))
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HEALTH_SCENARIO": str(spec),
            "LIVEKIT_CONFIG_DIR": str(config_dir),
            "VOICE_SECRETS_ENV": str(secrets),
            **overrides,
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

        report = json.loads(proc.stdout)

        # health-check sends lk's stderr to /dev/null, so a CRASHED mock lk is indistinguishable
        # from a real routing failure: the comparator just gets empty stdin and reports "not
        # JSON". Every test that expects a red routing check would then pass for entirely the
        # wrong reason. (It did. That is how this assertion got written.)
        routing = next((c for c in report["checks"] if c["name"] == "sip-routing"), None)
        if routing and "not JSON" in routing["detail"]:
            raise AssertionError(
                "the mocked `lk` produced no JSON — the live-routing comparison never ran, so "
                "this test proves nothing about routing:\n" + routing["detail"]
            )

        return proc.returncode, report

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


def test_a_starting_agent_is_not_ready(run_health_check):
    """`starting` is not `unhealthy` — and it is not READY either, so it is not `ok`.

    An agent still in prewarm cannot take a call. Reporting her green means `make health`
    says yes at the exact moment the honest answer is "not yet". (Yua, round 2.)
    """
    scenario = _healthy_fleet()
    scenario["containers"]["voice-agent-nyla"]["health"] = "starting"

    code, report = run_health_check(scenario)

    assert code == 1
    assert _check(report, "agent-nyla")["status"] == "fail"
    assert "starting" in _check(report, "agent-nyla")["detail"].lower()


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


# --- SIP routing: PRESENCE IS NOT CORRECTNESS -------------------------------------
#
# Every test below keeps all four expected `phone-<agent>` names live. The previous
# postcondition asked only "are the four names present?" — so it passed every one of these
# while the routing was ambiguous or plainly wrong. (Yua, round 2.)


@pytest.mark.parametrize("dropped", AGENTS)
def test_a_sister_missing_from_live_dispatch_is_reported(run_health_check, dropped):
    scenario = _healthy_fleet()
    scenario["live_rules"] = [_live_rule(a) for a in AGENTS if a != dropped]

    code, report = run_health_check(scenario)

    assert code == 1, f"phone-{dropped} is not live and `make health` exited 0"
    assert _check(report, "sip-routing")["status"] == "fail"


def test_a_stale_rule_for_a_retired_agent_is_reported(run_health_check):
    """THE ONE PRESENCE-CHECKING CANNOT SEE. All four are live. So is `phone-party`.

    The registrar only ever deletes the four rule names it knows about, so it will NEVER
    clean this up — it cannot remove what it does not look for. It sits there claiming
    inbound calls for a worker nobody runs, and every check we had said green.
    """
    scenario = _healthy_fleet()
    scenario["live_rules"].append(
        {
            "sipDispatchRuleId": "SDR_party",
            "name": "twilio-to-phone-party",
            "numbers": ["+15559999999"],
            "roomConfig": {"agents": [{"agentName": "phone-party"}]},
        }
    )

    code, report = run_health_check(scenario)

    assert code == 1, "a stale phone-party rule is live and `make health` exited 0"
    detail = _check(report, "sip-routing")["detail"]
    assert "party" in detail, detail


def test_a_duplicate_rule_for_one_agent_is_reported(run_health_check):
    """Two live rules for Aoi. Which one routes the call is not ours to decide."""
    scenario = _healthy_fleet()
    scenario["live_rules"].append(
        _live_rule("aoi", name="twilio-to-phone-aoi-old", dids=["+15558888888"])
    )

    code, report = run_health_check(scenario)

    assert code == 1, "aoi has two live dispatch rules and `make health` exited 0"
    assert _check(report, "sip-routing")["status"] == "fail"


def test_a_live_rule_with_the_wrong_did_is_reported(run_health_check):
    """All four names present, and Aoi's live rule carries a DID we never validated.

    The rule that is actually routing is not the rule we approved.
    """
    scenario = _healthy_fleet()
    scenario["live_rules"] = [
        _live_rule("aoi", dids=["+15551110000"]) if a == "aoi" else _live_rule(a) for a in AGENTS
    ]

    code, report = run_health_check(scenario)

    assert code == 1, "aoi's live DID is not the one we validated and `make health` exited 0"
    assert _check(report, "sip-routing")["status"] == "fail"


def test_a_stale_rule_stealing_a_real_did_is_reported(run_health_check):
    """The worst one: a leftover rule holding Nyla's real number. Two owners, one DID."""
    scenario = _healthy_fleet()
    scenario["live_rules"].append(
        {
            "sipDispatchRuleId": "SDR_old",
            "name": "twilio-legacy",
            "numbers": [DID["nyla"]],
            "roomConfig": {"agents": [{"agentName": "phone-sumi"}]},
        }
    )

    code, report = run_health_check(scenario)

    assert code == 1, "two agents are live for one DID and `make health` exited 0"
    assert _check(report, "sip-routing")["status"] == "fail"


def test_routing_that_cannot_be_verified_is_not_reported_as_healthy(run_health_check):
    """UNVERIFIABLE IS NOT VERIFIED. No credentials => the check did not run => not green.

    The failure mode this forecloses is the one that has bitten us most: a check that cannot
    run, quietly not running, and its silence reading as health.
    """
    code, report = run_health_check(
        _healthy_fleet(), VOICE_SECRETS_ENV="/nonexistent/livekit-agents.env"
    )

    assert code == 1
    routing = _check(report, "sip-routing")
    assert routing["status"] == "fail"
    assert "cannot verify" in routing["detail"].lower()


def test_a_missing_inbound_trunk_is_reported(run_health_check):
    """No trunk means no call reaches us at all."""
    scenario = _healthy_fleet()
    scenario["trunk_present"] = 0

    code, report = run_health_check(scenario)

    assert code == 1
    assert _check(report, "sip-trunk")["status"] == "fail"


def test_an_agentless_extra_rule_is_reported(run_health_check):
    """THE INVISIBLE RULE. It names no agent, so an agent-keyed comparison cannot see it.

    My comparator indexed the live table by `roomConfig.agents[].agentName` and then made
    claims about the live TABLE. A rule with an empty agents list never enters that index: not
    an extra, not a duplicate, and with an unrelated DID it owns nothing anyone checks. Five
    live rules, zero problems, and the gate printed "exactly 4 rules, no extras".

    A verdict about a set requires looking at the whole set, not a projection of it.
    (Yua, round 3.)
    """
    scenario = _healthy_fleet()
    scenario["live_rules"].append(
        {
            "sipDispatchRuleId": "SDR_ghost",
            "name": "twilio-orphan",
            "numbers": ["+15557770000"],  # unrelated to all four of ours
            "roomConfig": {"agents": []},
        }
    )

    code, report = run_health_check(scenario)

    assert code == 1, "a fifth, agentless live rule exists and `make health` exited 0"
    assert _check(report, "sip-routing")["status"] == "fail"


def test_a_failed_authoritative_query_is_not_a_green_verdict(run_health_check):
    """`lk` prints the PERFECT healthy document and exits nonzero.

    Without pipefail, the pipeline's status was the comparator's status — so the comparator
    validated a document whose query had failed, and health went green. A correct answer
    computed from an unreliable reading is not a correct answer.
    """
    scenario = _healthy_fleet()
    scenario["lk_exit"] = 3  # the JSON is exactly right; the query failed anyway

    code, report = run_health_check(scenario)

    assert code == 1, "the authoritative routing query FAILED and `make health` exited 0"
    routing = _check(report, "sip-routing")
    assert routing["status"] == "fail"
    assert "cannot verify" in routing["detail"].lower()
