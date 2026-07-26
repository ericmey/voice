#!/usr/bin/env python3
"""eval-call-agent — a CALL-SHAPED eval for a phone-agent LLM.

Built 2026-07-26 for the Qwen finetune POC. The question this answers is not
"is the model good" — it is the only question that decides the phone product:

    Under realistic concurrency, does it still emit VALID tool calls fast
    enough to stay ahead of TTS, and when it fails, HOW does it fail?

Speaks OpenAI /v1/chat/completions. The intent is that the SAME cases run
unchanged against llama.cpp (--jinja), vLLM, and a hosted L4 target, because the
comparison is only meaningful if the inputs are byte-identical.

PORTABILITY IS AN INTENT, NOT A VERIFIED PROPERTY (2026-07-26). This has only
ever been run against llama.cpp. The docstring previously asserted the
cross-engine claim as fact, which is the same fault this file's own history
records twice: true of what was tested, asserted about what was not. The claim
matters precisely because next week's L4/vLLM run depends on it, so it must be
PROVEN on the first alternative endpoint rather than assumed. Known risk areas
when it is: streamed tool_calls delta shape and index handling, the
reasoning_content field name, and whether chat_template_kwargs is accepted or
rejected. None of those are exotic; all of them differ between servers.

Four measurements, per case and aggregate:
  1. TOOL-CALL VALIDITY  schema-valid, right tool, right args — not "plausible"
  2. INSTRUCTION ADHERENCE  must-contain / must-not-contain, checked literally
  3. LATENCY  TTFT and decode tok/s at the concurrency you ask for, not at 1
  4. FAILURE SHAPE  a silent wrong-arg is far worse than a refusal; they are
     counted separately and never merged into one "pass rate"

Deliberately stdlib-only so it runs on any box without a venv.

  eval-call-agent.py --base-url http://127.0.0.1:8088/v1 --model sumi-local
  eval-call-agent.py --cases cases/phone-agent.json --concurrency 8 --out run.json
  eval-call-agent.py ... --compare baseline.json     # diff two runs
"""
from __future__ import annotations

import argparse
import contextlib
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_CASES = Path(__file__).parent / "cases" / "phone-agent.json"


# --------------------------------------------------------------------------
# result types


@dataclass
class CaseResult:
    id: str
    category: str
    ok: bool
    # failure_shape is the field that must never be collapsed into `ok`.
    #   none            passed
    #   refused         model declined — visible, recoverable, least bad
    #   wrong_tool      called something else — visible in logs
    #   bad_args        called the right tool with wrong/missing args — SILENT
    #   malformed       emitted tool-call-shaped text that will not parse
    #   no_tool_call    answered in prose where a tool was required
    #   instruction     content constraint violated
    #   transport       HTTP/stream error — not the model's fault
    failure_shape: str = "none"
    detail: str = ""
    ttft_s: float | None = None       # to first AUDIBLE token
    ttfr_s: float | None = None       # to first reasoning token
    decode_tps: float | None = None
    total_s: float | None = None
    completion_tokens: int = 0        # audible only
    reasoning_tokens: int = 0         # silent — never folded into the above
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# transport


class InFlight:
    """OBSERVE how many requests were genuinely outstanding, never compute it.

    min(workers, tasks) is an upper BOUND, not a measurement: workers stagger,
    and a short request can finish before a later one has opened its socket. So
    a run can report "24 in flight" having never had more than 9. Naming a
    computed ceiling `actual_` is the same fault one layer down — caught by Yua
    on review 2026-07-26, immediately after the first fix.

    The barrier makes the claim honest at the start: every worker waits until
    all N have arrived, so the first wave is genuinely simultaneous. If the
    SERVER then queues internally, fine — the claim is about requests
    outstanding from the client, and that is what this counts.
    """

    def __init__(self, expected: int) -> None:
        self._lock = threading.Lock()
        self._now = 0
        self.peak = 0
        # FIRST WAVE ONLY. A plain threading.Barrier is CYCLIC: with 24 tasks and
        # 16 parties, the first 16 release and the remaining 8 enter generation
        # two, then block for the full timeout waiting on 8 workers that will
        # never arrive. That is not server latency — it is the instrument — and
        # it produced a 120s TTFT p95 that I was one message away from reporting
        # as a capacity finding. Caught by Yua reading the diff, 2026-07-26.
        self._released = threading.Event()
        self._barrier = (
            threading.Barrier(expected, timeout=60, action=self._released.set)
            if expected > 1 else None
        )

    def release_wave(self) -> None:
        """Block only until the first wave has formed. Later tasks pass straight
        through — they are not part of the wave and must not wait for one."""
        if self._barrier is None or self._released.is_set():
            return
        try:
            self._barrier.wait()
        except threading.BrokenBarrierError:
            self._released.set()   # wave never formed; do not trap anyone else
        except threading.ThreadError:
            self._released.set()

    def __enter__(self):
        with self._lock:
            self._now += 1
            self.peak = max(self.peak, self._now)
        return self

    def __exit__(self, *exc):
        with self._lock:
            self._now -= 1
        return False


def stream_chat(base_url: str, model: str, case: dict, timeout: int,
                api_key: str | None, no_think: bool = False,
                inflight: InFlight | None = None) -> tuple[dict, float, float, int]:
    """POST a streamed completion. Returns (assembled, ttft_s, total_s, n_tok).

    Streaming is not a nicety here — TTFT is a product metric for voice and
    cannot be derived from a non-streamed response.
    """
    body = {
        "model": model,
        "messages": case["messages"],
        "stream": True,
        # Deterministic by default: a phone agent that varies run to run cannot
        # be regression-tested, and we are comparing MODELS not samplers.
        "temperature": case.get("temperature", 0.0),
        "max_tokens": case.get("max_tokens", 256),
    }
    if case.get("tools"):
        body["tools"] = case["tools"]
        body["tool_choice"] = case.get("tool_choice", "auto")
    if no_think:
        # Qwen3.5's template defaults to thinking. On a phone that is dead air
        # the caller pays for and never hears. Off by request, so the same
        # server can be measured both ways without a restart.
        body["chat_template_kwargs"] = {"enable_thinking": False}

    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
    )

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    finish_reason = None
    t0 = time.perf_counter()
    ttft = None          # to first AUDIBLE token — content or tool call
    ttfr = None          # to first REASONING token
    n_tok = 0            # audible tokens
    n_reason = 0         # silent reasoning tokens

    if inflight is not None:
        inflight.release_wave()

    ctx = inflight if inflight is not None else contextlib.nullcontext()
    with ctx, urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            finish_reason = choices[0].get("finish_reason") or finish_reason

            # Reasoning arrives on a SEPARATE field and is never spoken. Counting
            # it as output inflates tok/s and hides the only latency the caller
            # actually experiences. Measured separately, never merged.
            think = delta.get("reasoning_content") or (delta.get("reasoning") if
                                                       isinstance(delta.get("reasoning"), str) else None)
            if think:
                if ttfr is None:
                    ttfr = time.perf_counter() - t0
                reasoning_parts.append(think)
                n_reason += 1

            piece = delta.get("content")
            if piece:
                if ttft is None:
                    ttft = time.perf_counter() - t0
                content_parts.append(piece)
                n_tok += 1

            for tc in delta.get("tool_calls") or []:
                if ttft is None:
                    ttft = time.perf_counter() - t0
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(
                    idx, {"name": "", "arguments": "", "id": tc.get("id", "")}
                )
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
                    n_tok += 1

    total = time.perf_counter() - t0
    assembled = {
        "content": "".join(content_parts),
        "reasoning": "".join(reasoning_parts),
        "tool_calls": [tool_calls[k] for k in sorted(tool_calls)],
        "finish_reason": finish_reason,
        "reasoning_tokens": n_reason,
        "ttfr_s": round(ttfr, 4) if ttfr is not None else None,
    }
    return assembled, (ttft if ttft is not None else total), total, n_tok


# --------------------------------------------------------------------------
# grading
#
# Grade against the FAILURE first — assume fail and hunt the specific flaw —
# rather than scanning for a reason to pass. A grader that looks for success
# finds it.


def grade(case: dict, got: dict) -> tuple[bool, str, str]:
    """Return (ok, failure_shape, detail)."""
    expect = case.get("expect") or {}
    calls = got.get("tool_calls") or []
    content = got.get("content") or ""
    low = content.lower()

    # Check this BEFORE anything else. A model that spent its whole budget
    # thinking produced no audible output at all — calling that "answered in
    # prose" or "missing required text" describes a reply that never existed.
    # Named separately because the fix is a server flag, not a prompt.
    if not calls and not content.strip():
        if got.get("reasoning_tokens"):
            return (False, "thinking_overrun",
                    f"{got['reasoning_tokens']} silent reasoning tokens, "
                    f"finish={got.get('finish_reason')}, zero audible output")
        return False, "empty", f"no output at all, finish={got.get('finish_reason')}"

    want_tool = expect.get("tool")

    if want_tool:
        if not calls:
            refusal_markers = expect.get(
                "refusal_markers",
                ["i can't", "i cannot", "i'm unable", "i am unable", "sorry"],
            )
            if any(m in low for m in refusal_markers):
                return False, "refused", f"declined instead of calling {want_tool!r}"
            return False, "no_tool_call", f"answered in prose; expected {want_tool!r}"

        call = calls[0]
        if call["name"] != want_tool:
            return False, "wrong_tool", f"called {call['name']!r}, expected {want_tool!r}"

        try:
            args = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError as e:
            return False, "malformed", f"arguments not JSON: {e}; raw={call['arguments'][:160]!r}"
        if not isinstance(args, dict):
            return False, "malformed", f"arguments not an object: {type(args).__name__}"

        missing = [k for k in expect.get("required_args", []) if k not in args]
        if missing:
            return False, "bad_args", f"missing required args {missing}; got {sorted(args)}"

        for key, want_val in (expect.get("arg_equals") or {}).items():
            if key not in args:
                return False, "bad_args", f"missing arg {key!r}"
            if str(args[key]).strip().lower() != str(want_val).strip().lower():
                return False, "bad_args", f"{key}={args[key]!r}, expected {want_val!r}"

        for key, needle in (expect.get("arg_contains") or {}).items():
            if needle.lower() not in str(args.get(key, "")).lower():
                return False, "bad_args", f"{key}={args.get(key)!r} lacks {needle!r}"

    else:
        if calls and expect.get("forbid_tool_call", True):
            return False, "wrong_tool", f"called {calls[0]['name']!r} where prose was required"

    for needle in expect.get("must_contain", []):
        if needle.lower() not in low:
            return False, "instruction", f"missing required text {needle!r}"

    for needle in expect.get("must_not_contain", []):
        if needle.lower() in low:
            return False, "instruction", f"contains forbidden text {needle!r}"

    max_words = expect.get("max_words")
    if max_words and not calls and len(content.split()) > max_words:
        return False, "instruction", f"{len(content.split())} words > max {max_words}"

    return True, "none", ""


# --------------------------------------------------------------------------
# runner


def run_case(base_url: str, model: str, case: dict, timeout: int,
             api_key: str | None, no_think: bool = False,
             inflight: InFlight | None = None) -> CaseResult:
    try:
        got, ttft, total, n_tok = stream_chat(base_url, model, case, timeout, api_key,
                                              no_think, inflight)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return CaseResult(
            id=case["id"], category=case.get("category", "-"), ok=False,
            failure_shape="transport", detail=f"{type(e).__name__}: {e}",
        )

    ok, shape, detail = grade(case, got)
    decode_window = max(total - ttft, 1e-6)
    return CaseResult(
        id=case["id"],
        category=case.get("category", "-"),
        ok=ok,
        failure_shape=shape,
        detail=detail,
        ttft_s=round(ttft, 4),
        decode_tps=round(n_tok / decode_window, 2) if n_tok else 0.0,
        total_s=round(total, 4),
        completion_tokens=n_tok,
        reasoning_tokens=got.get("reasoning_tokens", 0),
        ttfr_s=got.get("ttfr_s"),
        raw=got,
    )


def summarize(results: list[CaseResult], meta: dict) -> dict:
    oks = [r for r in results if r.ok]
    ttfts = [r.ttft_s for r in results if r.ttft_s is not None]
    tpss = [r.decode_tps for r in results if r.decode_tps]

    shapes: dict[str, int] = {}
    for r in results:
        if not r.ok:
            shapes[r.failure_shape] = shapes.get(r.failure_shape, 0) + 1

    def pct(vals, p):
        if not vals:
            return None
        s = sorted(vals)
        return round(s[min(len(s) - 1, int(len(s) * p))], 4)

    return {
        "meta": meta,
        "totals": {
            "cases": len(results),
            "passed": len(oks),
            "failed": len(results) - len(oks),
            "pass_rate": round(len(oks) / len(results), 4) if results else 0.0,
        },
        # Never merged into pass_rate. bad_args is the dangerous one: it looks
        # like success to every layer above the model.
        "failure_shapes": shapes,
        "latency": {
            "ttft_p50": pct(ttfts, 0.50),
            "ttft_p95": pct(ttfts, 0.95),
            "ttft_max": round(max(ttfts), 4) if ttfts else None,
            "decode_tps_p50": pct(tpss, 0.50),
            "decode_tps_min": round(min(tpss), 2) if tpss else None,
            "decode_tps_mean": round(statistics.fmean(tpss), 2) if tpss else None,
        },
        "reasoning": {
            "cases_with_reasoning": sum(1 for r in results if r.reasoning_tokens),
            "reasoning_tokens_total": sum(r.reasoning_tokens for r in results),
            "reasoning_tokens_max": max((r.reasoning_tokens for r in results), default=0),
        },
        "cases": [asdict(r) for r in results],
    }


def print_report(rep: dict) -> None:
    t, lat = rep["totals"], rep["latency"]
    m = rep["meta"]
    print(f"\n  model={m['model']}  endpoint={m['base_url']}")
    peak = m.get("peak_in_flight")
    flag = "" if m.get("concurrency_honest", True) else "   <-- LABEL NOT EARNED"
    print(f"  {m.get('work_items', '?')} requests, "
          f"peak {peak} observed in flight (requested {m['concurrency']}){flag}")
    print(f"  {t['passed']}/{t['cases']} passed  ({t['pass_rate']*100:.1f}%)")

    if rep["failure_shapes"]:
        print("\n  failure shapes:")
        for shape, n in sorted(rep["failure_shapes"].items(), key=lambda kv: -kv[1]):
            mark = "  <-- SILENT, worst kind" if shape == "bad_args" else ""
            print(f"    {shape:14} {n}{mark}")

    print("\n  latency:")
    print(f"    TTFT   p50={lat['ttft_p50']}s  p95={lat['ttft_p95']}s  max={lat['ttft_max']}s"
          "   (to first AUDIBLE token)")
    print(f"    decode p50={lat['decode_tps_p50']} tok/s  min={lat['decode_tps_min']}  mean={lat['decode_tps_mean']}")

    rsn = rep.get("reasoning") or {}
    if rsn.get("reasoning_tokens_total"):
        print(f"\n  SILENT REASONING: {rsn['cases_with_reasoning']}/{rep['totals']['cases']} cases, "
              f"{rsn['reasoning_tokens_total']} tokens total, {rsn['reasoning_tokens_max']} max")
        print("    the caller hears none of these and waits through all of them")

    fails = [c for c in rep["cases"] if not c["ok"]]
    if fails:
        print("\n  failures:")
        for c in fails:
            print(f"    [{c['failure_shape']:12}] {c['id']:28} {c['detail'][:90]}")


def compare(cur: dict, base: dict) -> None:
    print("\n  === vs baseline ===")
    print(f"  baseline: {base['meta']['model']}   current: {cur['meta']['model']}")
    d = cur["totals"]["pass_rate"] - base["totals"]["pass_rate"]
    print(f"  pass rate {base['totals']['pass_rate']*100:.1f}% -> "
          f"{cur['totals']['pass_rate']*100:.1f}%  ({d*100:+.1f} pts)")

    bl, cl = base["latency"], cur["latency"]
    if bl["ttft_p95"] and cl["ttft_p95"]:
        print(f"  TTFT p95  {bl['ttft_p95']}s -> {cl['ttft_p95']}s")
    if bl["decode_tps_p50"] and cl["decode_tps_p50"]:
        print(f"  decode    {bl['decode_tps_p50']} -> {cl['decode_tps_p50']} tok/s")

    bmap = {c["id"]: c for c in base["cases"]}
    regressions = [c for c in cur["cases"]
                   if not c["ok"] and bmap.get(c["id"], {}).get("ok")]
    fixes = [c for c in cur["cases"]
             if c["ok"] and bmap.get(c["id"]) and not bmap[c["id"]]["ok"]]

    if regressions:
        print("\n  REGRESSIONS (passed on baseline, fail now):")
        for c in regressions:
            print(f"    [{c['failure_shape']:12}] {c['id']:28} {c['detail'][:80]}")
    if fixes:
        print("\n  newly passing:")
        for c in fixes:
            print(f"    {c['id']}")
    if not regressions and not fixes:
        print("\n  no per-case changes.")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8088/v1")
    p.add_argument("--model", default="sumi-local")
    p.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    p.add_argument("--concurrency", type=int, default=1,
                   help="simultaneous in-flight calls; 1 measures nothing about a server")
    p.add_argument("--repeat", type=int, default=1, help="run the case set N times")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--api-key", default=None)
    p.add_argument("--no-think", action="store_true",
                   help="send chat_template_kwargs.enable_thinking=false (Qwen3.x)")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--compare", type=Path, default=None, help="baseline run.json to diff against")
    a = p.parse_args()

    if not a.cases.is_file():
        print(f"eval-call-agent: no case file at {a.cases}", file=sys.stderr)
        return 2
    cases = json.loads(a.cases.read_text())
    if not isinstance(cases, list) or not cases:
        print(f"eval-call-agent: {a.cases} is not a non-empty list", file=sys.stderr)
        return 2

    work = [c for _ in range(a.repeat) for c in cases]

    # A ThreadPoolExecutor with more workers than tasks does not create load it
    # does not have. With 12 cases and repeat=1, --concurrency 24 issues at most
    # TWELVE simultaneous requests, and a report headlined "at c=24" is false.
    # Caught by Yua on review 2026-07-26 AFTER a full day of results had been
    # published under inflated concurrency labels. Never silent again.
    actual = min(len(work), a.concurrency)
    if actual < a.concurrency:
        need = -(-a.concurrency // len(cases))
        print(f"eval-call-agent: REFUSING a false concurrency label.\n"
              f"  requested --concurrency {a.concurrency} but only {len(work)} work items exist\n"
              f"  ({len(cases)} cases x repeat {a.repeat}), so at most {actual} requests can be\n"
              f"  in flight. Re-run with --repeat {need} for a true {a.concurrency}-way test,\n"
              f"  or --concurrency {actual} to measure what you actually have.",
              file=sys.stderr)
        return 2

    # `actual` is REQUESTED wave capacity — an upper bound. Only peak_in_flight,
    # measured around the request lifetime, may be called observed.
    print(f"eval-call-agent: {len(cases)} cases x{a.repeat} = {len(work)} requests, "
          f"wave capacity {actual} (requested) -> {a.model} @ {a.base_url}")

    inflight = InFlight(a.concurrency)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=a.concurrency) as pool:
        results = list(pool.map(
            lambda c: run_case(a.base_url, a.model, c, a.timeout, a.api_key, a.no_think,
                               inflight), work))
    wall = time.perf_counter() - t0

    rep = summarize(results, {
        "model": a.model,
        "base_url": a.base_url,
        "concurrency": a.concurrency,
        "peak_in_flight": inflight.peak,          # OBSERVED, not computed
        "concurrency_honest": inflight.peak >= a.concurrency,
        "work_items": len(work),
        "repeat": a.repeat,
        "cases_file": str(a.cases),
        "no_think": a.no_think,
        "wall_s": round(wall, 2),
    })
    print_report(rep)

    if a.compare:
        if not a.compare.is_file():
            print(f"\neval-call-agent: baseline {a.compare} not found", file=sys.stderr)
            return 2
        compare(rep, json.loads(a.compare.read_text()))

    if a.out:
        a.out.write_text(json.dumps(rep, indent=2))
        print(f"\n  wrote {a.out}")

    # Exit non-zero on ANY failure. A harness that always exits 0 is decoration.
    return 0 if rep["totals"]["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
