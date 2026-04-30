"""Authenticated LangSmith client factory.

Loads credentials from ``secrets/langsmith-provisioning.env`` (gitignored)
and returns a ``langsmith.Client`` configured with the workspace context
header service keys require for write endpoints.

Service keys (``lsv2_sk_*``) need an ``X-Tenant-Id`` header on most
write paths — without it most ``/api/v1/sessions``-shaped endpoints
return 403. The SDK doesn't auto-set this; we inject it via the
``custom_headers`` parameter the Client accepts.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from langsmith import Client

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SECRETS_PATH = REPO_ROOT / "secrets" / "langsmith-provisioning.env"


def _load_secrets() -> dict[str, str]:
    """Load the gitignored secrets file via bash so quoted values
    (LANGSMITH_PROJECT="Harem World") survive intact.

    python-dotenv doesn't handle the inline-quote-around-spaces case
    cleanly, but bash's standard ``source`` does. Spawn a subshell,
    source, dump env, parse it back. Slight overhead at startup;
    correctness over micro-optimisation.
    """
    if not SECRETS_PATH.exists():
        raise FileNotFoundError(
            f"missing {SECRETS_PATH} — copy from "
            f"secrets/langsmith-provisioning.env.example and fill in"
        )

    cmd = f"set -a; source {shlex.quote(str(SECRETS_PATH))}; set +a; env | grep ^LANGSMITH_"
    proc = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, check=True)
    out: dict[str, str] = {}
    for line in proc.stdout.strip().splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


def make_client() -> tuple[Client, dict[str, str]]:
    """Return ``(client, config)`` where config has the resolved values
    every provisioning step needs (api_key, project, workspace_id).

    The Client is pre-authenticated with the workspace tenant header so
    write endpoints work out of the box. Apply scripts call this once
    at module top.
    """
    secrets = _load_secrets()
    api_key = secrets.get("LANGSMITH_API_KEY", "")
    endpoint = secrets.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com")
    project = secrets.get("LANGSMITH_PROJECT", "")
    workspace_id = secrets.get("LANGSMITH_WORKSPACE_ID", "")

    if not api_key.startswith("lsv2_sk_"):
        raise ValueError(
            "LANGSMITH_API_KEY must be a service key (lsv2_sk_*) for provisioning. "
            "Personal tokens (lsv2_pt_*) lack workspace-write perms."
        )
    if not workspace_id:
        raise ValueError(
            "LANGSMITH_WORKSPACE_ID is required — service keys need a workspace "
            "tenant header on write endpoints. Find it via:\n"
            "  curl -H 'x-api-key: $KEY' "
            "https://api.smith.langchain.com/api/v1/workspaces"
        )

    # Inject the workspace tenant header on every request. Without this,
    # /api/v1/sessions and other write endpoints 403.
    client = Client(
        api_key=api_key,
        api_url=endpoint,
        web_url=endpoint.replace("api.smith", "smith"),
        # Some SDK versions accept this as kwarg; fall back to manual
        # header injection on the underlying session below.
    )
    # Force the header onto the session object the SDK uses for HTTP.
    # The Client exposes ``session`` (a requests.Session) on most versions.
    if hasattr(client, "session") and client.session is not None:
        client.session.headers["X-Tenant-Id"] = workspace_id

    return client, {
        "api_key": api_key,
        "endpoint": endpoint,
        "project": project,
        "workspace_id": workspace_id,
    }
