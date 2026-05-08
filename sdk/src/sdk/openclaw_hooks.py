"""OpenClaw Gateway hooks client for voice-agent handoffs.

The phone agents should treat OpenClaw as the owner of agent work and
delivery. This module submits narrow `/hooks/agent` requests and returns
once the Gateway accepts the job.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import aiohttp

logger = logging.getLogger("openclaw-livekit.agent")


class OpenClawHookConfigError(RuntimeError):
    """Raised when hook submission is not configured."""


class OpenClawHookError(RuntimeError):
    """Raised when the Gateway rejects or fails a hook request."""


@dataclass(frozen=True)
class OpenClawHookConfig:
    """Runtime settings for Gateway hooks."""

    base_url: str
    token: str
    path: str = "/hooks"

    @property
    def agent_url(self) -> str:
        base = self.base_url.rstrip("/")
        path = "/" + self.path.strip("/")
        return f"{base}{path}/agent"


@dataclass(frozen=True)
class OpenClawHookAccepted:
    """Accepted hook response."""

    run_id: str


def get_openclaw_hook_config() -> OpenClawHookConfig:
    """Read Gateway hook config from env.

    `OPENCLAW_HOOK_TOKEN` is intentionally distinct from
    `GATEWAY_AUTH_TOKEN`; hooks should use their narrow dedicated token.
    """
    token = os.environ.get("OPENCLAW_HOOK_TOKEN", "").strip()
    if not token:
        raise OpenClawHookConfigError("OPENCLAW_HOOK_TOKEN is not configured")

    base_url = os.environ.get("OPENCLAW_GATEWAY_HTTP_URL", "").strip()
    if not base_url:
        port = os.environ.get("GATEWAY_PORT", "").strip()
        if not port:
            raise OpenClawHookConfigError(
                "OPENCLAW_GATEWAY_HTTP_URL or GATEWAY_PORT is not configured"
            )
        base_url = f"http://127.0.0.1:{port}"

    path = os.environ.get("OPENCLAW_HOOKS_PATH", "/hooks").strip() or "/hooks"
    return OpenClawHookConfig(base_url=base_url, token=token, path=path)


async def post_agent_hook(
    *,
    agent_id: str,
    message: str,
    name: str,
    deliver: bool = True,
    channel: str | None = None,
    to: str | None = None,
    timeout_seconds: int | None = None,
    request_timeout_seconds: float = 5.0,
) -> OpenClawHookAccepted:
    """Submit an isolated OpenClaw agent turn and return after acceptance."""
    cfg = get_openclaw_hook_config()
    payload: dict[str, Any] = {
        "agentId": agent_id,
        "message": message,
        "name": name,
        "wakeMode": "now",
        "deliver": deliver,
    }
    if channel:
        payload["channel"] = channel
    if to:
        payload["to"] = to
    if timeout_seconds is not None:
        payload["timeoutSeconds"] = timeout_seconds

    idem_key = f"openclaw-livekit-{uuid4()}"
    timeout = aiohttp.ClientTimeout(total=request_timeout_seconds)
    headers = {
        "Authorization": f"Bearer {cfg.token}",
        "Content-Type": "application/json",
        "Idempotency-Key": idem_key,
        "User-Agent": "openclaw-livekit/voice-agent",
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(cfg.agent_url, json=payload, headers=headers) as response:
                status = response.status
                reason = response.reason
                text = await response.text(errors="replace")
    except TimeoutError as err:
        raise OpenClawHookError("OpenClaw hook request timed out") from err
    except aiohttp.ClientError as err:
        raise OpenClawHookError(f"OpenClaw hook request failed: {err}") from err

    if not 200 <= status < 300:
        snippet = text.strip()[:200]
        detail = snippet or reason or "no body"
        raise OpenClawHookError(f"OpenClaw hook returned status {status}: {detail}")
    if not text.strip():
        raise OpenClawHookError("OpenClaw hook returned empty response body")
    try:
        body = json.loads(text)
    except ValueError as err:
        raise OpenClawHookError(f"OpenClaw hook returned invalid JSON: {err}") from err
    if not isinstance(body, dict):
        raise OpenClawHookError("OpenClaw hook returned a non-object response")
    if body.get("ok") is not True:
        error = body.get("error") if isinstance(body.get("error"), str) else reason
        raise OpenClawHookError(f"OpenClaw hook rejected the request: {error}")

    run_id = body.get("runId")
    if not isinstance(run_id, str) or not run_id.strip():
        raise OpenClawHookError("OpenClaw hook accepted without a runId")

    logger.info("[voice-tools] OpenClaw hook accepted agent=%s run_id=%s", agent_id, run_id)
    return OpenClawHookAccepted(run_id=run_id)
