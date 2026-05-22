"""Tests for the OpenClaw Gateway hooks client."""

from __future__ import annotations

import pytest
from aiohttp import web
from sdk.openclaw_hooks import (
    OpenClawHookConfigError,
    OpenClawHookError,
    get_openclaw_hook_config,
    post_agent_hook,
)


async def _serve(handler):
    """Start an aiohttp test server on an OS-assigned port and return (runner, port)."""
    app = web.Application()
    app.router.add_post("/hooks/agent", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    addresses = runner.addresses
    assert addresses
    port = addresses[0][1]
    return runner, port


def test_hook_config_requires_dedicated_token(monkeypatch):
    monkeypatch.delenv("OPENCLAW_HOOK_TOKEN", raising=False)
    monkeypatch.setenv("GATEWAY_PORT", "18789")

    with pytest.raises(OpenClawHookConfigError, match="OPENCLAW_HOOK_TOKEN"):
        get_openclaw_hook_config()


def test_hook_config_defaults_to_gateway_port(monkeypatch):
    monkeypatch.setenv("OPENCLAW_HOOK_TOKEN", "secret")
    monkeypatch.setenv("GATEWAY_PORT", "18789")
    monkeypatch.delenv("OPENCLAW_GATEWAY_HTTP_URL", raising=False)

    cfg = get_openclaw_hook_config()

    assert cfg.agent_url == "http://127.0.0.1:18789/hooks/agent"


@pytest.mark.asyncio
async def test_post_agent_hook_sends_expected_payload(monkeypatch):
    seen: dict[str, object] = {}

    async def handler(request: web.Request) -> web.Response:
        seen["authorization"] = request.headers.get("Authorization")
        seen["idempotency"] = request.headers.get("Idempotency-Key")
        seen["body"] = await request.json()
        return web.json_response({"ok": True, "runId": "run-123"})

    runner, port = await _serve(handler)
    monkeypatch.setenv("OPENCLAW_HOOK_TOKEN", "secret")
    monkeypatch.setenv("OPENCLAW_GATEWAY_HTTP_URL", f"http://127.0.0.1:{port}")

    try:
        accepted = await post_agent_hook(
            agent_id="yumi",
            message="Do the research",
            name="Voice delegation",
            channel="discord",
            to="channel:123",
        )
    finally:
        await runner.cleanup()

    assert accepted.run_id == "run-123"
    assert seen["authorization"] == "Bearer secret"
    assert isinstance(seen["idempotency"], str)
    assert seen["body"] == {
        "agentId": "yumi",
        "message": "Do the research",
        "name": "Voice delegation",
        "wakeMode": "now",
        "deliver": True,
        "channel": "discord",
        "to": "channel:123",
    }


@pytest.mark.asyncio
async def test_post_agent_hook_surfaces_rejection(monkeypatch):
    async def handler(_request: web.Request) -> web.Response:
        return web.json_response({"ok": False, "error": "denied"}, status=400)

    runner, port = await _serve(handler)
    monkeypatch.setenv("OPENCLAW_HOOK_TOKEN", "secret")
    monkeypatch.setenv("OPENCLAW_GATEWAY_HTTP_URL", f"http://127.0.0.1:{port}")

    try:
        with pytest.raises(OpenClawHookError, match="denied"):
            await post_agent_hook(agent_id="yumi", message="Do it", name="Voice")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_post_agent_hook_rejects_empty_success_body(monkeypatch):
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=200, body=b"")

    runner, port = await _serve(handler)
    monkeypatch.setenv("OPENCLAW_HOOK_TOKEN", "secret")
    monkeypatch.setenv("OPENCLAW_GATEWAY_HTTP_URL", f"http://127.0.0.1:{port}")

    try:
        with pytest.raises(OpenClawHookError, match="empty response body"):
            await post_agent_hook(agent_id="yumi", message="Do it", name="Voice")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_post_agent_hook_rejects_invalid_json_success_body(monkeypatch):
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=200, body=b"not json", content_type="application/json")

    runner, port = await _serve(handler)
    monkeypatch.setenv("OPENCLAW_HOOK_TOKEN", "secret")
    monkeypatch.setenv("OPENCLAW_GATEWAY_HTTP_URL", f"http://127.0.0.1:{port}")

    try:
        with pytest.raises(OpenClawHookError, match="invalid JSON"):
            await post_agent_hook(agent_id="yumi", message="Do it", name="Voice")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_post_agent_hook_surfaces_5xx_with_empty_body(monkeypatch):
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=503, body=b"")

    runner, port = await _serve(handler)
    monkeypatch.setenv("OPENCLAW_HOOK_TOKEN", "secret")
    monkeypatch.setenv("OPENCLAW_GATEWAY_HTTP_URL", f"http://127.0.0.1:{port}")

    try:
        with pytest.raises(OpenClawHookError, match="status 503"):
            await post_agent_hook(agent_id="yumi", message="Do it", name="Voice")
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_post_agent_hook_handles_undecodable_body(monkeypatch):
    """A 5xx with bytes that aren't valid UTF-8 must surface as OpenClawHookError, not UnicodeDecodeError."""

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=500, body=b"\xff\xfe\xfd garbage \x80\x81")

    runner, port = await _serve(handler)
    monkeypatch.setenv("OPENCLAW_HOOK_TOKEN", "secret")
    monkeypatch.setenv("OPENCLAW_GATEWAY_HTTP_URL", f"http://127.0.0.1:{port}")

    try:
        with pytest.raises(OpenClawHookError, match="status 500"):
            await post_agent_hook(agent_id="yumi", message="Do it", name="Voice")
    finally:
        await runner.cleanup()
