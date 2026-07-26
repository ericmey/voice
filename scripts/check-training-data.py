#!/usr/bin/env python3
"""check-training-data — gate a finetune dataset before it costs GPU hours.

Source-agnostic ON PURPOSE. It does not care whether examples were hand-written,
synthesised, or lifted from real calls — the source decision is Eric's and this
tool must not imply one. What it does care about is that the dataset cannot
silently invalidate the evaluation we plan to judge the finetune with.

FOUR GATES, most important first:

  1. EVAL CONTAMINATION.  If a training example contains an eval case's caller
     line, the eval stops measuring generalisation and starts measuring recall.
     The finetune then scores well on exactly the cases we would use to approve
     it. This is the failure that cannot be detected after the fact by looking
     at the model — only by looking at the data, and only before training.

  2. PROVENANCE.  Every example must declare where it came from. Not for
     bookkeeping: a dataset whose origin is unknown cannot be audited for
     consent, cannot be selectively withdrawn, and cannot be defended. An
     example with no `source` is not a cheap example, it is an unusable one.

  3. PII.  Real-call material carries names, numbers, addresses, card
     fragments. Detection here is a FLOOR, not a guarantee — a clean report
     means "these patterns did not fire", never "this data is safe to train on".

  4. SHAPE AND COVERAGE.  Valid chat structure, and how the examples distribute
     across the behaviours the eval actually tests. A dataset that teaches
     warmth and never once demonstrates refusing to invent an order number will
     produce exactly the model our cases already catch.

  check-training-data.py data.jsonl --cases scripts/cases/customer-service.json
  check-training-data.py data.jsonl --cases c1.json --cases c2.json --json out.json
  check-training-data.py --selftest

Exit 0 only if every gate passes. A dataset that fails gate 1 must not be
trained on; the others are judgement calls this tool reports rather than makes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

# Contamination is checked on normalised word n-grams rather than exact strings.
# An example that paraphrases an eval prompt leaks almost as much as one that
# copies it, and exact-match would report a clean dataset either way.
NGRAM = 8

PII_PATTERNS = {
    "email": r"[\w.+-]+@[\w-]+\.[\w.]+",
    "phone_us": r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}(?!\d)",
    # 7-digit local form. Added after --selftest caught the 10-digit pattern
    # missing "555-0142" — exactly the shape a caller says on a real call. It
    # will occasionally fire on a dashed reference number; for a FLOOR detector
    # that reports rather than blocks, a false positive costs a glance and a
    # false negative costs a person's phone number in a training set.
    "phone_local": r"(?<![\d-])\d{3}[-. ]\d{4}(?![\d-])",
    "card_like": r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)",
    "ssn_like": r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)",
    "postcode_uk": r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b",
    "street": r"\b\d{1,5}\s+[A-Z][a-z]+\s+(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr)\b",
}

REQUIRED_PROVENANCE = ("source",)
KNOWN_SOURCES = ("handwritten", "synthetic", "real_call", "adapted")


def normalise(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return text.split()


def ngrams(words: list[str], n: int = NGRAM) -> set[str]:
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append({"_line": lineno, **json.loads(line)})
        except json.JSONDecodeError as e:
            raise SystemExit(
                f"check-training-data: {path}:{lineno} is not valid JSON: {e}") from e
    return rows


def eval_fingerprints(case_files: list[Path]) -> tuple[set[str], dict[str, str]]:
    """n-grams from every eval case's caller and system text, plus their owners."""
    grams: set[str] = set()
    owner: dict[str, str] = {}
    for f in case_files:
        if not f.is_file():
            raise SystemExit(f"check-training-data: no such cases file {f}")
        for case in json.loads(f.read_text()):
            for m in case.get("messages", []):
                if m.get("role") in ("user", "system") and m.get("content"):
                    for g in ngrams(normalise(m["content"])):
                        grams.add(g)
                        owner.setdefault(g, case["id"])
    return grams, owner


def check(rows: list[dict], eval_grams: set[str], owner: dict[str, str]) -> dict:
    contamination, bad_shape, missing_prov, unknown_src, pii_hits = [], [], [], [], []
    categories: Counter[str] = Counter()
    sources: Counter[str] = Counter()

    for r in rows:
        line = r["_line"]
        msgs = r.get("messages")

        if not isinstance(msgs, list) or not msgs:
            bad_shape.append((line, "no messages list"))
            continue
        roles = [m.get("role") for m in msgs]
        if not all(x in ("system", "user", "assistant", "tool") for x in roles):
            bad_shape.append((line, f"unknown role in {roles}"))
        if "assistant" not in roles:
            bad_shape.append((line, "no assistant turn — nothing to learn from"))

        src = r.get("source")
        if not all(r.get(k) for k in REQUIRED_PROVENANCE):
            missing_prov.append(line)
        elif src not in KNOWN_SOURCES:
            unknown_src.append((line, src))
        if src:
            sources[src] += 1
        categories[r.get("category", "(uncategorised)")] += 1

        text = " ".join(m.get("content") or "" for m in msgs)
        hit = eval_grams & ngrams(normalise(text))
        if hit:
            g = sorted(hit)[0]
            contamination.append((line, owner.get(g, "?"), g))

        for name, pat in PII_PATTERNS.items():
            for m in re.finditer(pat, text):
                pii_hits.append((line, name, m.group()[:24]))

    return {
        "examples": len(rows),
        "contamination": contamination,
        "bad_shape": bad_shape,
        "missing_provenance": missing_prov,
        "unknown_source": unknown_src,
        "pii": pii_hits,
        "categories": dict(categories),
        "sources": dict(sources),
    }


def report(res: dict, coverage_ref: set[str] | None) -> bool:
    ok = True
    print(f"\n  {res['examples']} examples\n")

    n = len(res["contamination"])
    print(f"  1. EVAL CONTAMINATION   {'FAIL' if n else 'pass'}  ({n} example(s))")
    if n:
        ok = False
        for line, cid, gram in res["contamination"][:8]:
            print(f"       line {line}: overlaps case {cid!r}")
            print(f"         shared: ...{gram}...")
        if n > 8:
            print(f"       ... and {n - 8} more")
        print("       A model trained on these is being tested on its own training set.")

    n = len(res["missing_provenance"]) + len(res["unknown_source"])
    print(f"  2. PROVENANCE           {'FAIL' if n else 'pass'}  ({n} example(s))")
    if n:
        ok = False
        if res["missing_provenance"]:
            print(f"       no 'source' field: lines {res['missing_provenance'][:10]}")
        for line, src in res["unknown_source"][:6]:
            print(f"       line {line}: source={src!r} not in {KNOWN_SOURCES}")

    n = len(res["pii"])
    print(f"  3. PII PATTERNS         {'FLAGGED' if n else 'none found'}  ({n} hit(s))")
    if n:
        seen = Counter(k for _, k, _ in res["pii"])
        for kind, count in seen.most_common():
            print(f"       {kind}: {count}")
        print("       Detection is a FLOOR. 'none found' is not 'safe to train on'.")

    n = len(res["bad_shape"])
    print(f"  4. SHAPE                {'FAIL' if n else 'pass'}  ({n} example(s))")
    if n:
        ok = False
        for line, why in res["bad_shape"][:8]:
            print(f"       line {line}: {why}")

    print("\n  sources:", res["sources"] or "(none declared)")
    print("  categories:")
    for cat, count in sorted(res["categories"].items(), key=lambda kv: -kv[1]):
        print(f"    {count:>5}  {cat}")

    if coverage_ref:
        missing = sorted(coverage_ref - set(res["categories"]))
        if missing:
            print("\n  COVERAGE GAP — behaviours the eval tests with no training examples:")
            for c in missing:
                print(f"    {c}")
            print("  Not a failure. But the finetune cannot learn what it never sees,")
            print("  and these are exactly the cases it will be judged on.")

    return ok


def selftest() -> int:
    """Plant a contaminated example and require the gate to catch it.

    A contamination check that has never caught a contaminated example is a
    claim. This also verifies the NEGATIVE side — that clean, differently-worded
    examples do NOT trip it — because a detector that fires on everything is
    equally useless and much more annoying.
    """
    import tempfile

    cases = [{
        "id": "planted-case",
        "messages": [
            {"role": "system", "content": "You are a phone agent. Never invent an order number."},
            {"role": "user", "content": "This is the third time I have called about order 44821 and nobody has done anything at all."},
        ],
    }]
    d = Path(tempfile.mkdtemp())
    cf = d / "cases.json"
    cf.write_text(json.dumps(cases))

    dirty = {"source": "synthetic", "category": "range", "messages": [
        {"role": "user", "content": "This is the third time I have called about order 44821 and nobody has done anything at all."},
        {"role": "assistant", "content": "Let me look that up."}]}
    clean = {"source": "synthetic", "category": "range", "messages": [
        {"role": "user", "content": "My package still has not turned up and I am losing patience with this."},
        {"role": "assistant", "content": "That is genuinely frustrating. What is the order number?"}]}
    noprov = {"category": "range", "messages": [
        {"role": "user", "content": "Where is my delivery, it was due last Tuesday afternoon."},
        {"role": "assistant", "content": "Let me check that for you."}]}

    grams, owner = eval_fingerprints([cf])
    ok = True

    r = check([{"_line": 1, **dirty}], grams, owner)
    hit = len(r["contamination"]) == 1
    print(f"  planted contamination detected      {'PASS' if hit else 'FAIL'}")
    ok &= hit

    r = check([{"_line": 1, **clean}], grams, owner)
    quiet = len(r["contamination"]) == 0
    print(f"  clean example NOT flagged           {'PASS' if quiet else 'FAIL'}")
    ok &= quiet

    r = check([{"_line": 1, **noprov}], grams, owner)
    caught = len(r["missing_provenance"]) == 1
    print(f"  missing provenance detected         {'PASS' if caught else 'FAIL'}")
    ok &= caught

    r = check([{"_line": 1, "source": "synthetic", "messages": [
        {"role": "user", "content": "Call me back on 555-0142 or email me at a@b.com please."},
        {"role": "assistant", "content": "Noted."}]}], grams, owner)
    pii = len(r["pii"]) >= 2
    print(f"  PII patterns fire on planted data   {'PASS' if pii else 'FAIL'}")
    ok &= pii

    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("data", type=Path, nargs="?", help="training data, JSONL")
    p.add_argument("--cases", type=Path, action="append", default=[],
                   help="eval case file to check contamination against (repeatable)")
    p.add_argument("--json", type=Path, default=None, help="write the full report")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()

    if a.selftest:
        print("\ncheck-training-data selftest — gates must fire on planted faults\n")
        return selftest()

    if not a.data:
        print("check-training-data: a data file is required", file=sys.stderr)
        return 2
    if not a.data.is_file():
        print(f"check-training-data: no such file {a.data}", file=sys.stderr)
        return 2
    if not a.cases:
        print("check-training-data: --cases is required.\n"
              "  Without the eval cases there is no contamination check, and the\n"
              "  contamination check is the reason this tool exists.", file=sys.stderr)
        return 2

    grams, owner = eval_fingerprints(a.cases)
    coverage_ref = set()
    for f in a.cases:
        for case in json.loads(f.read_text()):
            coverage_ref.add(case.get("category", "(uncategorised)"))

    res = check(load_jsonl(a.data), grams, owner)
    ok = report(res, coverage_ref)

    if a.json:
        a.json.write_text(json.dumps(res, indent=2))
        print(f"\n  wrote {a.json}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
