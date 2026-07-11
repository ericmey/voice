"""The SIP dispatch contract — validated once, by one implementation, for everyone.

``config/sip-dispatch-<agent>.json`` decides **which sister answers which phone
number.** It is the only place that mapping exists. A mistake here does not crash
anything: the call connects, and Eric talks to the wrong person — or to no one.

Two holes made that reachable, and they compounded:

**The tests certified the wrong files.** Only ``*.json.example`` is tracked; the
registrar consumes the gitignored ``*.json``. So a green CI proved the examples
were well-formed and said *nothing* about the four files production actually reads.
The test suite and the deployment were looking at different documents.

**The registrar deleted before it validated.** ``register_rule`` looked up the live
rule by name, DELETED it, and only then created the replacement from a file it had
never inspected. A truncated file, a bad ``agentName``, a DID copy-pasted from a
sister — every one of those lands as: working route destroyed, broken route (or no
route) in its place. The failure is discovered by a caller.

So this module is the single validator both consumers call, and it enforces the
rule that makes delete-then-create survivable: **validate the COMPLETE candidate set
before mutating anything.** Not per-file, as each is about to be written — the whole
set, up front. A duplicate DID or a missing sister is only visible when you hold all
four at once, and by the time you are deleting rule three, rule one is already gone.

Nothing here talks to the network. It is a pure function of the files on disk, which
is exactly why the tests and the registrar can run the identical code.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# The fleet. Exactly these four — not "at least", not "whatever files are present".
# An unexpected fifth dispatch file is a routing rule nobody has reviewed.
AGENTS: tuple[str, ...] = ("nyla", "aoi", "yua", "sumi")

# E.164. Twilio hands us this shape; a DID in any other form silently matches nothing.
E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class SipPreflightError(Exception):
    """The candidate set is not safe to register. Carries EVERY problem, not the first.

    One-problem-at-a-time turns a five-minute fix into five deploys, and each aborted
    deploy is another window where routing is half-written.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        body = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"SIP dispatch preflight FAILED ({len(problems)} problem(s)):\n{body}")


@dataclass(frozen=True)
class DispatchRule:
    """One validated agent→DID mapping."""

    agent: str
    path: Path
    rule_name: str
    agent_name: str  # the "phone-<agent>" literal LiveKit routes on
    numbers: tuple[str, ...]


def registration_name(agent: str) -> str:
    """``phone-<agent>`` — derived, never hand-typed.

    Kept in lockstep with ``AgentConfig.registration_name`` by
    ``test_the_registration_name_has_exactly_one_spelling``. Not imported from
    ``AgentConfig`` because that requires constructing a config per agent; the
    string law is the thing being asserted, and a test pins the two together.
    """
    return f"phone-{agent}"


def load_rule_json(path: Path) -> dict:
    """Read a dispatch file, dropping the operator ``_comment`` LiveKit's parser rejects."""
    data = json.loads(path.read_text())
    data.pop("_comment", None)
    return data


def dispatch_path(config_dir: Path, agent: str, *, allow_example: bool = False) -> Path | None:
    """The file the registrar would consume for ``agent``, or None.

    Real config wins over the example. ``allow_example`` exists ONLY so tests can
    validate the tracked examples in a clean checkout — the registrar never sets it,
    because registering the examples into a live LiveKit would route Eric's real DIDs
    to the placeholder ``+1555…`` numbers.
    """
    real = config_dir / f"sip-dispatch-{agent}.json"
    if real.is_file():
        return real
    example = config_dir / f"sip-dispatch-{agent}.json.example"
    if allow_example and example.is_file():
        return example
    return None


def _validate_one(agent: str, path: Path, problems: list[str]) -> DispatchRule | None:
    """Structure + identity of a single file. Appends to ``problems``; never raises."""
    where = path.name

    try:
        data = load_rule_json(path)
    except json.JSONDecodeError as exc:
        problems.append(f"{where}: not valid JSON ({exc}) — would have replaced a live rule")
        return None

    rule = data.get("dispatch_rule")
    if not isinstance(rule, dict):
        problems.append(f"{where}: missing top-level 'dispatch_rule' object")
        return None

    rule_name = rule.get("name")
    if not isinstance(rule_name, str) or not rule_name.strip():
        problems.append(f"{where}: dispatch_rule.name is missing or empty")
        rule_name = ""

    if not isinstance(rule.get("rule"), dict) or not rule["rule"]:
        problems.append(f"{where}: dispatch_rule.rule is missing — LiveKit would not route")

    # --- the DIDs -----------------------------------------------------
    numbers = rule.get("numbers")
    if not isinstance(numbers, list) or not numbers:
        problems.append(
            f"{where}: dispatch_rule.numbers is missing or empty — this rule matches NO "
            f"inbound call. It registers successfully and {agent} never rings."
        )
        numbers = []
    else:
        for n in numbers:
            if not isinstance(n, str) or not E164.match(n):
                problems.append(f"{where}: {n!r} is not an E.164 DID (want +15551234567)")

    # --- WHO ANSWERS. The literal LiveKit routes the call on. ----------
    agents_block = rule.get("room_config", {}).get("agents")
    agent_name = ""
    if not isinstance(agents_block, list) or len(agents_block) != 1:
        problems.append(
            f"{where}: room_config.agents must name exactly one agent "
            f"(got {len(agents_block) if isinstance(agents_block, list) else 'none'}) — "
            f"a call has one sister on the other end of it"
        )
    else:
        agent_name = (
            agents_block[0].get("agentName", "") if isinstance(agents_block[0], dict) else ""
        )
        expected = registration_name(agent)
        if agent_name != expected:
            problems.append(
                f"{where}: agentName={agent_name!r} but this file is {agent}'s, so it must be "
                f"{expected!r}. THIS IS THE WRONG-SISTER BUG: the file name says {agent}, the "
                f"routing says {agent_name or 'nobody'}. It does not crash — the caller simply "
                f"reaches the wrong person, or silence."
            )

    if rule_name and agent_name and agent_name not in rule_name:
        problems.append(
            f"{where}: rule name {rule_name!r} does not mention {agent_name!r} — the registrar "
            f"matches live rules BY NAME to decide what to delete, so a name that drifts from "
            f"its agent orphans the old rule and leaves two live rules fighting over one DID"
        )

    if not rule_name or not agent_name:
        return None

    return DispatchRule(
        agent=agent,
        path=path,
        rule_name=rule_name,
        agent_name=agent_name,
        numbers=tuple(numbers),
    )


def validate_dispatch_set(config_dir: Path, *, allow_example: bool = False) -> list[DispatchRule]:
    """Validate the WHOLE candidate set. Raise before anything is mutated.

    This is the function the registrar calls before its first delete, and the function
    the tests call. Same code, same verdict — which is what makes a green test mean
    something about production.
    """
    config_dir = Path(config_dir)
    problems: list[str] = []
    rules: list[DispatchRule] = []

    # --- exactly the four sisters, no more, no less --------------------
    for agent in AGENTS:
        path = dispatch_path(config_dir, agent, allow_example=allow_example)
        if path is None:
            problems.append(
                f"sip-dispatch-{agent}.json is MISSING from {config_dir} — {agent} has no "
                f"inbound route at all; every call to her DID dies"
            )
            continue
        found = _validate_one(agent, path, problems)
        if found is not None:
            rules.append(found)

    known = {f"sip-dispatch-{a}.json" for a in AGENTS}
    for stray in sorted(config_dir.glob("sip-dispatch-*.json")):
        if stray.name not in known:
            problems.append(
                f"{stray.name}: unknown dispatch file — it is not one of the four agents. "
                f"A routing rule nobody reviewed is a call going somewhere nobody chose."
            )

    # --- cross-file laws. Only visible holding all four at once. -------
    by_did: dict[str, list[str]] = {}
    by_rule_name: dict[str, list[str]] = {}
    for r in rules:
        for n in r.numbers:
            by_did.setdefault(n, []).append(r.agent)
        by_rule_name.setdefault(r.rule_name, []).append(r.agent)

    for did, owners in sorted(by_did.items()):
        if len(owners) > 1:
            problems.append(
                f"DID {did} is claimed by {', '.join(sorted(owners))} — a phone number has ONE "
                f"owner. LiveKit picks one; which one is not something we get to decide, and it "
                f"can change between registrations."
            )

    for name, owners in sorted(by_rule_name.items()):
        if len(owners) > 1:
            problems.append(
                f"rule name {name!r} is used by {', '.join(sorted(owners))} — the registrar "
                f"deletes live rules by name, so registering the second would delete the first"
            )

    if problems:
        raise SipPreflightError(problems)

    return rules


def main(argv: list[str]) -> int:
    """CLI the registrar shells out to: ``python -m sdk.sip_preflight <config_dir>``."""
    if len(argv) < 2:
        print("usage: python -m sdk.sip_preflight <config_dir> [--allow-example]", file=sys.stderr)
        return 2

    config_dir = Path(argv[1])
    allow_example = "--allow-example" in argv

    try:
        rules = validate_dispatch_set(config_dir, allow_example=allow_example)
    except SipPreflightError as exc:
        print(str(exc), file=sys.stderr)
        print(
            "\nNothing was registered. The live routing is untouched — which is the point: "
            "the old rules are still working.",
            file=sys.stderr,
        )
        return 1

    for r in sorted(rules, key=lambda r: r.agent):
        print(f"  ok  {r.path.name:28} {r.agent_name:12} {' '.join(r.numbers)}")
    print(f"preflight OK — {len(rules)} dispatch rules, distinct DIDs, identities agree")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
