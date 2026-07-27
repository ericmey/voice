#!/usr/bin/env python3
"""Fail-closed probe for the OpenAI streaming contract used by the phone agent.

The probe checks behavior, not merely HTTP compatibility:

* thinking is actually disabled (no reasoning field and no ``<think>`` text),
* audible time to first token is below the supplied voice bound, and
* streamed tool-call fragments carry integer indexes and reassemble into the
  expected function and arguments.

The API key is read only from ``SUMI_VLLM_API_KEY`` and is never accepted on the
command line. The receipt contains raw response chunks but never request headers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class ProbeFailure(RuntimeError):
    """A named protocol failure that should block qualification."""

    def __init__(self, shape: str, detail: str):
        super().__init__(detail)
        self.shape = shape
        self.detail = detail


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _post_stream(
    base_url: str,
    api_key: str,
    body: dict[str, Any],
    timeout: float,
) -> tuple[list[dict[str, Any]], float, float | None]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    chunks: list[dict[str, Any]] = []
    started = time.perf_counter()
    audible_ttft: float | None = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    raise ProbeFailure("malformed_stream", f"non-SSE line: {line[:120]}")
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise ProbeFailure("malformed_stream", f"invalid JSON chunk: {exc}") from exc
                chunks.append(chunk)
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                content = delta.get("content")
                if audible_ttft is None and isinstance(content, str) and content:
                    audible_ttft = time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ProbeFailure("http_error", f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ProbeFailure("transport", str(exc.reason)) from exc
    return chunks, time.perf_counter() - started, audible_ttft


def _deltas(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    for chunk in chunks:
        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            if isinstance(delta, dict):
                deltas.append(delta)
    return deltas


def _reject_reasoning(deltas: list[dict[str, Any]]) -> None:
    for delta in deltas:
        for field in ("reasoning_content", "reasoning"):
            value = delta.get(field)
            if value not in (None, "", []):
                raise ProbeFailure(
                    "thinking_not_disabled",
                    f"server emitted non-empty {field}; request flag/default was ineffective",
                )
        content = delta.get("content")
        if isinstance(content, str) and "<think" in content.lower():
            raise ProbeFailure("thinking_not_disabled", "audible content contains <think> markup")


def _check_prose(chunks: list[dict[str, Any]], ttft: float | None, bound: float) -> dict[str, Any]:
    deltas = _deltas(chunks)
    _reject_reasoning(deltas)
    content = "".join(
        delta.get("content", "") for delta in deltas if isinstance(delta.get("content"), str)
    ).strip()
    if not content:
        raise ProbeFailure("empty_audible", "prose request produced no audible content")
    if ttft is None or ttft > bound:
        shown = "none" if ttft is None else f"{ttft:.3f}s"
        raise ProbeFailure("ttft_bound", f"audible TTFT {shown} exceeds {bound:.3f}s")
    return {"content": content, "audible_ttft_s": round(ttft, 4)}


def _check_tool(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    deltas = _deltas(chunks)
    _reject_reasoning(deltas)
    assembled: dict[int, dict[str, str]] = {}
    saw_tool_delta = False
    for delta in deltas:
        tool_calls = delta.get("tool_calls") or []
        for call in tool_calls:
            saw_tool_delta = True
            index = call.get("index")
            if not isinstance(index, int):
                raise ProbeFailure(
                    "tool_delta_index", f"tool-call delta lacks integer index: {call}"
                )
            target = assembled.setdefault(index, {"name": "", "arguments": ""})
            function = call.get("function") or {}
            name = function.get("name")
            arguments = function.get("arguments")
            if isinstance(name, str):
                target["name"] += name
            if isinstance(arguments, str):
                target["arguments"] += arguments
    if not saw_tool_delta:
        raise ProbeFailure("no_tool_call", "tool request emitted no streamed tool_calls delta")
    if len(assembled) != 1:
        raise ProbeFailure("wrong_tool", f"expected one tool call, got indexes {sorted(assembled)}")
    call = next(iter(assembled.values()))
    if call["name"] != "lookup_order":
        raise ProbeFailure("wrong_tool", f"expected lookup_order, got {call['name']!r}")
    try:
        arguments = json.loads(call["arguments"])
    except json.JSONDecodeError as exc:
        raise ProbeFailure("malformed_tool_args", f"tool arguments are not JSON: {exc}") from exc
    if "44821" not in str(arguments.get("order_number", "")):
        raise ProbeFailure("bad_args", f"expected order 44821, got {arguments!r}")
    return {"name": call["name"], "arguments": arguments}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8088/v1")
    parser.add_argument("--model", default="qwen3.5-9b")
    parser.add_argument("--max-ttft", type=float, default=1.5)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    api_key = os.environ.get("SUMI_VLLM_API_KEY")
    if not api_key:
        print("probe-openai-stream-contract: SUMI_VLLM_API_KEY is required", file=sys.stderr)
        return 2

    common: dict[str, Any] = {
        "model": args.model,
        "stream": True,
        "temperature": 0,
        "max_tokens": 96,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    prose_body = {
        **common,
        "messages": [
            {
                "role": "system",
                "content": "You are a concise phone support agent. Reply in one plain spoken sentence.",
            },
            {"role": "user", "content": "Say that you are ready to help with an order."},
        ],
    }
    tool_body = {
        **common,
        "messages": [
            {
                "role": "system",
                "content": "Use the lookup tool when the caller provides an order number.",
            },
            {"role": "user", "content": "Please look up order 44821."},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup_order",
                    "description": "Look up an order by number.",
                    "parameters": {
                        "type": "object",
                        "properties": {"order_number": {"type": "string"}},
                        "required": ["order_number"],
                    },
                },
            }
        ],
        "tool_choice": "auto",
    }

    receipt: dict[str, Any] = {
        "endpoint": args.base_url,
        "model": args.model,
        "max_ttft_s": args.max_ttft,
        "requests": {
            "prose_sha256": _sha256_json(prose_body),
            "tool_sha256": _sha256_json(tool_body),
        },
    }
    try:
        # Warm only the prose path. It is retained in the receipt but excluded
        # from the measured TTFT gate.
        warm_chunks, warm_wall, _ = _post_stream(args.base_url, api_key, prose_body, args.timeout)
        prose_chunks, prose_wall, prose_ttft = _post_stream(
            args.base_url, api_key, prose_body, args.timeout
        )
        tool_chunks, tool_wall, _ = _post_stream(args.base_url, api_key, tool_body, args.timeout)
        receipt.update(
            {
                "warmup": {"wall_s": round(warm_wall, 4), "chunks": warm_chunks},
                "prose": {
                    **_check_prose(prose_chunks, prose_ttft, args.max_ttft),
                    "wall_s": round(prose_wall, 4),
                    "chunks": prose_chunks,
                },
                "tool": {
                    **_check_tool(tool_chunks),
                    "wall_s": round(tool_wall, 4),
                    "chunks": tool_chunks,
                },
                "ok": True,
            }
        )
    except ProbeFailure as exc:
        receipt.update({"ok": False, "failure_shape": exc.shape, "detail": exc.detail})
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(receipt, indent=2) + "\n")
        print(f"probe-openai-stream-contract: FAIL [{exc.shape}] {exc.detail}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        "probe-openai-stream-contract: PASS "
        f"audible_ttft={receipt['prose']['audible_ttft_s']:.4f}s "
        f"tool={receipt['tool']['name']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
