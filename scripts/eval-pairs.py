#!/usr/bin/env python3
"""eval-pairs — put two runs side by side for a BLIND human read.

Built 2026-07-26 to close a hole in `eval-call-agent.py` that I would rather
name than leave for someone to discover next week.

    Every case in the suite is a CONSTRAINT check: don't use boilerplate, don't
    invent authority, stay under N words, get the tool argument right. Those
    detect a finetune BREAKING something. They cannot detect a finetune WORKING.

A warmth finetune that succeeds perfectly would move the customer-service set
from 11/14 to at most 14/14 — three points of headroom, and every one of them
earned by *not failing* rather than by being better. **"Warmer" is not a
constraint, so it cannot be scored by a constraint suite.**

The calibrated instrument for warmth is a person. So this does not attempt a
number: it emits the same case answered by two models, **unlabelled and in
randomised order**, for a blind A/B. Which is the same move as handing the
phone-rendered audio to Eric's ear rather than reporting dBFS at him.

    eval-pairs.py base-run.json finetune-run.json --out pairs.md
    eval-pairs.py base.json ft.json --out pairs.md --key pairs-key.json
    eval-pairs.py --score pairs-key.json --answers "1A,2B,3A,..."

The KEY file is written separately so the reader can hold the comparison without
the answers being one scroll away. `--score` grades the answers afterwards and
reports which model was preferred per case and overall.

Order is derived from a caller-supplied `--seed` so a run is reproducible
without ever calling a random source the harness bans elsewhere.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _flip(case_id: str, seed: str) -> bool:
    """Deterministic per-case order. Same seed + id -> same layout, always.

    Not random: a blind read that cannot be reproduced is not evidence, and a
    reviewer who wants to re-derive the key must be able to.
    """
    h = hashlib.sha256(f"{seed}:{case_id}".encode()).digest()
    return bool(h[0] & 1)


def load(path: Path) -> tuple[dict, dict]:
    data = json.loads(path.read_text())
    return data, {c["id"]: c for c in data["cases"]}


def render_answer(case: dict) -> str:
    """What the model actually produced, in the form a reader should judge."""
    parts = []
    content = (case.get("raw") or {}).get("content", "").strip()
    if content:
        parts.append(content)
    for tc in (case.get("raw") or {}).get("tool_calls", []):
        parts.append(f"[calls {tc['name']}({tc['arguments']})]")
    if not parts:
        parts.append("[no output]")
    return "\n".join(parts)


def load_prompts(cases_path: Path | None) -> dict[str, str]:
    """Map case id -> the caller's last line.

    The run JSON stores RESULTS, not requests, so the prompt has to come from the
    case file. Without this the "Caller:" line renders empty and nobody notices —
    a reader would simply never learn it was meant to be there.
    """
    if not cases_path:
        return {}
    if not cases_path.is_file():
        raise SystemExit(f"eval-pairs: no such cases file {cases_path}")
    out: dict[str, str] = {}
    for case in json.loads(cases_path.read_text()):
        for m in reversed(case.get("messages", [])):
            if m.get("role") == "user":
                out[case["id"]] = m["content"]
                break
    return out


def build(base_path: Path, cand_path: Path, seed: str,
          prompts: dict[str, str]) -> tuple[str, dict]:
    base_run, base_by_id = load(base_path)
    cand_run, cand_by_id = load(cand_path)

    shared = [cid for cid in base_by_id if cid in cand_by_id]
    if not shared:
        raise SystemExit("eval-pairs: the two runs share no case ids")

    dropped = (set(base_by_id) | set(cand_by_id)) - set(shared)

    lines = [
        "# Blind A/B — which answer is better?",
        "",
        "Same caller, same instructions, two models. **The labels do not tell you",
        "which is which**, and the order is shuffled per case.",
        "",
        "For each case write **A** or **B**. Write **=** if they are equivalent —",
        "that is a real answer and often the correct one.",
        "",
        "You are judging **which one you would rather have said this on a real",
        "call**: warmth, plausibility as a person, and whether it commits to",
        "anything it shouldn't. Not grammar, not length.",
        "",
        "---",
        "",
    ]
    key: dict[str, dict] = {}

    for n, cid in enumerate(shared, 1):
        b, c = base_by_id[cid], cand_by_id[cid]
        flip = _flip(cid, seed)
        first, second = (c, b) if flip else (b, c)
        key[str(n)] = {
            "case_id": cid,
            "A": "candidate" if flip else "base",
            "B": "base" if flip else "candidate",
            # Constraint verdicts are recorded but deliberately NOT shown to the
            # reader — a "FAIL" badge would drive the judgement it is meant to
            # be independent of.
            "A_constraints_ok": (c if flip else b)["ok"],
            "B_constraints_ok": (b if flip else c)["ok"],
        }

        prompt = prompts.get(cid, "")

        lines += [f"## {n}. `{cid}`", ""]
        if prompt:
            lines += [f"> **Caller:** {prompt}", ""]
        lines += [
            "**A**", "", "```", render_answer(first), "```", "",
            "**B**", "", "```", render_answer(second), "```", "",
            "Your pick: `___`", "", "---", "",
        ]

    if dropped:
        lines += ["", f"_{len(dropped)} case(s) present in only one run and omitted: "
                      f"{', '.join(sorted(dropped))}_", ""]

    return "\n".join(lines), key


def score(key_path: Path, answers: str) -> int:
    key = json.loads(key_path.read_text())
    picks = [a.strip().upper() for a in answers.split(",") if a.strip()]

    tally = {"base": 0, "candidate": 0, "tie": 0}
    rows = []
    for pick in picks:
        num = "".join(ch for ch in pick if ch.isdigit())
        letter = "".join(ch for ch in pick if ch in "AB=")
        entry = key.get(num)
        if not entry or not letter:
            print(f"eval-pairs: cannot parse {pick!r}", file=sys.stderr)
            continue
        winner = "tie" if letter == "=" else entry[letter]
        tally[winner] += 1
        rows.append((num, entry["case_id"], winner))

    width = max((len(r[1]) for r in rows), default=10)
    print("\n  per case:")
    for num, cid, winner in rows:
        print(f"    {num:>3}  {cid:<{width}}  {winner}")

    total = sum(tally.values())
    print(f"\n  candidate preferred  {tally['candidate']}/{total}")
    print(f"  base preferred       {tally['base']}/{total}")
    print(f"  equivalent           {tally['tie']}/{total}")

    if tally["candidate"] == tally["base"]:
        print("\n  No preference. On this set the change is not audible to the reader.")
    else:
        better = "candidate" if tally["candidate"] > tally["base"] else "base"
        margin = abs(tally["candidate"] - tally["base"])
        print(f"\n  {better} preferred by {margin} of {total}.")
        if margin <= max(1, total // 8):
            print("  Margin is within one or two flips — treat as no clear result.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("base", type=Path, nargs="?", help="baseline run.json")
    p.add_argument("candidate", type=Path, nargs="?", help="candidate run.json")
    p.add_argument("--out", type=Path, default=None, help="markdown for the reader")
    p.add_argument("--key", type=Path, default=None, help="answer key (write separately)")
    p.add_argument("--seed", default="phone-agent", help="fixes the A/B layout")
    p.add_argument("--cases", type=Path, default=None,
                   help="case file, so each pair can show the caller's line")
    p.add_argument("--score", type=Path, default=None, help="key file to grade against")
    p.add_argument("--answers", default="", help='e.g. "1A,2B,3=,4A"')
    a = p.parse_args()

    if a.score:
        if not a.answers:
            print("eval-pairs: --score needs --answers", file=sys.stderr)
            return 2
        return score(a.score, a.answers)

    if not a.base or not a.candidate:
        print("eval-pairs: base and candidate run files are required", file=sys.stderr)
        return 2
    for f in (a.base, a.candidate):
        if not f.is_file():
            print(f"eval-pairs: no such file {f}", file=sys.stderr)
            return 2

    prompts = load_prompts(a.cases)
    if a.cases and not prompts:
        print(f"eval-pairs: {a.cases} yielded no user messages", file=sys.stderr)
        return 2
    md, key = build(a.base, a.candidate, a.seed, prompts)

    out = a.out or Path("pairs.md")
    out.write_text(md)
    key_path = a.key or out.with_name(out.stem + "-key.json")
    key_path.write_text(json.dumps(key, indent=2))

    print(f"\n  wrote {out}   ({len(key)} pairs, seed={a.seed!r})")
    print(f"  wrote {key_path}")
    print("\n  Read the markdown WITHOUT opening the key. Then:")
    print(f"    eval-pairs.py --score {key_path} --answers \"1A,2B,3=,...\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
