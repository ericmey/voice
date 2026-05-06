#!/usr/bin/env python3
"""Check Grafana Loki for post-smoke-test failures.

The script talks to Loki through Grafana's datasource proxy so operators can
reuse a Grafana service-account token without exposing Loki directly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_GRAFANA_URL = "http://localhost:3000"
DEFAULT_DATASOURCE_UID = "loki"
DEFAULT_SERVICE_REGEX = (
    "openclaw-livekit-nyla|openclaw-livekit-aoi|openclaw-livekit-yua|"
    "openclaw-livekit-party|openclaw-.*"
)
FAILURE_REGEX = (
    "(?i)(error|exception|traceback|failed|rejected|denied|unauthorized|timeout|"
    "stalled session|active_work_without_progress)"
)
OTEL_FAILURE_REGEX = "(?i)(otlp|otel.*(failed|error|timeout)|export.*(failed|error|timeout))"


@dataclass(frozen=True)
class LokiQuery:
    name: str
    query: str
    fail_on_match: bool = True


def _parse_duration(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", value)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid duration: {value!r}")
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _request_json(url: str, token: str, timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected JSON response from {url}")
    return data


def _query_range(
    *,
    grafana_url: str,
    datasource_uid: str,
    token: str,
    query: str,
    start: int,
    end: int,
    limit: int,
    timeout: int,
) -> dict[str, Any]:
    base = grafana_url.rstrip("/")
    params = urllib.parse.urlencode(
        {
            "query": query,
            "start": f"{start}000000000",
            "end": f"{end}000000000",
            "limit": str(limit),
            "direction": "forward",
        }
    )
    url = f"{base}/api/datasources/proxy/uid/{datasource_uid}/loki/api/v1/query_range?{params}"
    data = _request_json(url, token, timeout)
    if data.get("status") != "success":
        raise RuntimeError(f"Loki query failed: {data}")
    return data


def _entries(data: dict[str, Any]) -> list[tuple[dict[str, Any], str, str]]:
    result = data.get("data", {}).get("result", [])
    if not isinstance(result, list):
        return []
    found: list[tuple[dict[str, Any], str, str]] = []
    for stream in result:
        if not isinstance(stream, dict):
            continue
        labels = stream.get("stream", {})
        values = stream.get("values", [])
        if not isinstance(labels, dict) or not isinstance(values, list):
            continue
        for value in values:
            if (
                isinstance(value, list)
                and len(value) >= 2
                and isinstance(value[0], str)
                and isinstance(value[1], str)
            ):
                found.append((labels, value[0], value[1]))
    return found


def _print_human(results: list[tuple[LokiQuery, list[tuple[dict[str, Any], str, str]]]]) -> None:
    print("loki smoke check")
    for query, entries in results:
        print(f"\n{query.name}: {len(entries)} matching entr{'y' if len(entries) == 1 else 'ies'}")
        for labels, timestamp, line in entries[:10]:
            service = labels.get("service_name", "<unknown>")
            level = (
                labels.get("level") or labels.get("detected_level") or labels.get("severity_text")
            )
            print(f"  {timestamp} service={service} level={level or '<unknown>'} {line}")
        if len(entries) > 10:
            print(f"  ... {len(entries) - 10} more")


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grafana-url", default=os.getenv("GRAFANA_URL", DEFAULT_GRAFANA_URL))
    parser.add_argument(
        "--datasource-uid", default=os.getenv("GRAFANA_LOKI_UID", DEFAULT_DATASOURCE_UID)
    )
    parser.add_argument("--token-env", default="GRAFANA_TOKEN")
    parser.add_argument("--since", type=_parse_duration, default=_parse_duration("10m"))
    parser.add_argument("--start", type=int, help="Unix seconds; overrides --since")
    parser.add_argument("--end", type=int)
    parser.add_argument("--service-regex", default=DEFAULT_SERVICE_REGEX)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    token = os.getenv(args.token_env)
    if not token:
        print(f"{args.token_env} is required for Grafana API access", file=sys.stderr)
        return 2

    end_value = args.end if args.end is not None else int(time.time())
    start_value = args.start if args.start is not None else end_value - args.since
    service_selector = f'{{service_name=~"{args.service_regex}"}}'
    agent_selector = (
        '{service_name=~"openclaw-livekit-nyla|openclaw-livekit-aoi|'
        'openclaw-livekit-yua|openclaw-livekit-party"}'
    )
    queries = [
        LokiQuery(
            "openclaw_and_voice_failures", f"{service_selector} |~ {json.dumps(FAILURE_REGEX)}"
        ),
        LokiQuery(
            "voice_otel_export_failures", f"{agent_selector} |~ {json.dumps(OTEL_FAILURE_REGEX)}"
        ),
    ]

    results: list[tuple[LokiQuery, list[tuple[dict[str, Any], str, str]]]] = []
    has_failure = False
    try:
        for query in queries:
            data = _query_range(
                grafana_url=args.grafana_url,
                datasource_uid=args.datasource_uid,
                token=token,
                query=query.query,
                start=start_value,
                end=end_value,
                limit=args.limit,
                timeout=args.timeout,
            )
            matches = _entries(data)
            results.append((query, matches))
            has_failure = has_failure or (query.fail_on_match and bool(matches))
    except Exception as err:
        print(f"Loki smoke check failed: {err}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not has_failure,
                    "grafana_url": args.grafana_url,
                    "datasource_uid": args.datasource_uid,
                    "start": start_value,
                    "end": end_value,
                    "queries": [
                        {
                            "name": query.name,
                            "query": query.query,
                            "matches": len(matches),
                            "entries": [
                                {"labels": labels, "timestamp": timestamp, "line": line}
                                for labels, timestamp, line in matches[: args.limit]
                            ],
                        }
                        for query, matches in results
                    ],
                },
                indent=2,
            )
        )
    else:
        _print_human(results)

    return 1 if has_failure else 0


if __name__ == "__main__":
    raise SystemExit(_main())
