"""LangSmith provisioning — idempotent apply of declarative config.

Usage:

    python -m ops.langsmith.provision --dry-run    # preview, no writes
    python -m ops.langsmith.provision              # apply

Or via Makefile:

    make langsmith-plan       # dry-run
    make langsmith-provision  # apply

What gets applied (in order):

1. **Project settings** — description, metadata. Project must already
   exist (LangSmith creates it on first ingest); this updates metadata.
2. **Feedback configs** — one per ``FEEDBACK_CONFIGS`` entry. Skips
   if already present with matching shape.
3. **Annotation queues** — one per ``ANNOTATION_QUEUES`` entry. Skips
   if name already exists.
4. **Workspace secrets** — provider API keys (OpenAI, Gemini, xAI,
   OpenRouter) sourced from ``~/.openclaw/.env`` and POSTed to
   ``/api/v1/workspaces/current/secrets``. These power online
   LLM-as-judge evaluators. Already-present keys are not re-uploaded
   (avoids silent value rotation). Override source via
   ``OPENCLAW_ENV_PATH`` env var.
5. **Datasets** — created and populated from
   ``ops/langsmith/datasets/<name>.py::EXAMPLES``.

Each phase prints a per-resource status: ``[+] created``, ``[~] updated``,
``[=] unchanged``, ``[!] error``. Final summary at the end.

Idempotency contract: re-running with no config changes prints all
``[=] unchanged``. Editing a config file and re-running applies the diff.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Any

# Imports work whether you run `python -m ops.langsmith.provision` or
# `python ops/langsmith/provision.py` from the repo root.
_pkg_root = __package__ or "ops.langsmith"
if not __package__:
    # Direct script invocation — make sibling imports work.
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent))
    from ops.langsmith import datasets as _datasets_mod
    from ops.langsmith import projects as _projects_mod
    from ops.langsmith.client import make_client
else:
    from . import datasets as _datasets_mod
    from . import projects as _projects_mod
    from .client import make_client


# ---------------------------------------------------------------------------
# Print helpers — every line tagged with status so the summary at the
# end can grep its own output for counts.
# ---------------------------------------------------------------------------

CREATED = "[+]"
UPDATED = "[~]"
UNCHANGED = "[=]"
SKIPPED = "[-]"
ERROR = "[!]"
DRY = "[?]"


class Counters:
    def __init__(self) -> None:
        self.created = 0
        self.updated = 0
        self.unchanged = 0
        self.skipped = 0
        self.errored = 0
        self.would_change = 0  # for dry-run

    def __str__(self) -> str:
        return (
            f"created={self.created}  updated={self.updated}  "
            f"unchanged={self.unchanged}  skipped={self.skipped}  "
            f"errored={self.errored}  would_change={self.would_change}"
        )


# ---------------------------------------------------------------------------
# Phase 1: project settings
# ---------------------------------------------------------------------------


def apply_project_settings(
    client: Any, config: dict[str, str], counters: Counters, *, dry_run: bool
) -> None:
    """Update the project's description + metadata. Project itself
    is created on first OTel ingest, not by this script."""
    print(f"\n=== phase 1: project settings — {config['project']!r} ===")
    target = _projects_mod.PROJECT_SETTINGS

    try:
        project = client.read_project(project_name=config["project"])
    except Exception as exc:
        print(f"{ERROR} could not read project {config['project']!r}: {exc}")
        print("    (project must exist before provisioning — send one trace first)")
        counters.errored += 1
        return

    current_desc = project.description or ""
    desc_changed = current_desc != target["description"]
    # Metadata diff — current may be None or dict.
    current_meta = getattr(project, "metadata", None) or {}
    meta_changed = any(current_meta.get(k) != v for k, v in target["metadata"].items())

    if not (desc_changed or meta_changed):
        print(f"{UNCHANGED} project description + metadata already match config")
        counters.unchanged += 1
        return

    if dry_run:
        print(f"{DRY} would update description / metadata on project {project.id}")
        counters.would_change += 1
        return

    try:
        client.update_project(
            project_id=project.id,
            description=target["description"],
            metadata=target["metadata"],
        )
        print(f"{UPDATED} project description + metadata updated")
        counters.updated += 1
    except Exception as exc:
        print(f"{ERROR} update_project failed: {exc}")
        counters.errored += 1


# ---------------------------------------------------------------------------
# Phase 2: feedback configs
# ---------------------------------------------------------------------------


def apply_feedback_configs(
    client: Any, config: dict[str, str], counters: Counters, *, dry_run: bool
) -> None:
    """Create feedback configs (rating dimensions) for the project."""
    print("\n=== phase 2: feedback configs ===")

    try:
        existing = {fc.feedback_key: fc for fc in client.list_feedback_configs()}
    except Exception as exc:
        print(f"{ERROR} list_feedback_configs failed: {exc}")
        counters.errored += 1
        return

    for fc in _projects_mod.FEEDBACK_CONFIGS:
        key = fc["feedback_key"]
        if key in existing:
            print(f"{UNCHANGED} feedback config {key!r} already exists")
            counters.unchanged += 1
            continue

        if dry_run:
            print(f"{DRY} would create feedback config {key!r}")
            counters.would_change += 1
            continue

        try:
            # SDK signature: create_feedback_config(feedback_key, feedback_config, ...)
            # feedback_config IS the spec dict; no separate description kwarg.
            # The description in our config is documentation-only — surfaced
            # in projects.py for the operator, not on the LangSmith API.
            client.create_feedback_config(
                feedback_key=key,
                feedback_config=fc["feedback_score_spec"],
            )
            print(f"{CREATED} feedback config {key!r}")
            counters.created += 1
        except Exception as exc:
            print(f"{ERROR} create_feedback_config({key!r}) failed: {exc}")
            counters.errored += 1


# ---------------------------------------------------------------------------
# Phase 3: annotation queues
# ---------------------------------------------------------------------------


def apply_annotation_queues(
    client: Any, config: dict[str, str], counters: Counters, *, dry_run: bool
) -> None:
    """Create annotation queues for human review workflows."""
    print("\n=== phase 3: annotation queues ===")

    try:
        existing_names = {q.name for q in client.list_annotation_queues()}
    except Exception as exc:
        print(f"{ERROR} list_annotation_queues failed: {exc}")
        counters.errored += 1
        return

    for q in _projects_mod.ANNOTATION_QUEUES:
        name = q["name"]
        if name in existing_names:
            print(f"{UNCHANGED} annotation queue {name!r} already exists")
            counters.unchanged += 1
            continue

        if dry_run:
            print(f"{DRY} would create annotation queue {name!r}")
            counters.would_change += 1
            continue

        try:
            client.create_annotation_queue(name=name, description=q["description"])
            print(f"{CREATED} annotation queue {name!r}")
            counters.created += 1
        except Exception as exc:
            print(f"{ERROR} create_annotation_queue({name!r}) failed: {exc}")
            counters.errored += 1


# ---------------------------------------------------------------------------
# Phase 4: workspace secrets — provider API keys for online evaluators
# ---------------------------------------------------------------------------


def _load_openclaw_secrets() -> dict[str, str]:
    """Source the operator's ~/.openclaw/.env (or ``OPENCLAW_ENV_PATH`` override)
    via bash so quoted values + multiline continuations behave like normal
    shell sourcing. Same trick as client.py uses for our own provisioning env.

    Only ``LANGSMITH_*``-irrelevant keys are returned — the caller picks
    which ones to forward to the LangSmith workspace per ``WORKSPACE_SECRETS``.
    """
    import os
    import shlex
    import subprocess
    from pathlib import Path

    src = os.environ.get("OPENCLAW_ENV_PATH") or str(Path.home() / ".openclaw" / ".env")
    src_path = Path(src)
    if not src_path.exists():
        raise FileNotFoundError(
            f"openclaw env file not found: {src} — set OPENCLAW_ENV_PATH "
            "to override, or skip the secrets phase: `make langsmith-provision "
            "--phase=feedback` etc."
        )

    cmd = f"set -a; source {shlex.quote(str(src_path))}; set +a; env"
    proc = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, check=True)
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


def apply_workspace_secrets(
    client: Any, config: dict[str, str], counters: Counters, *, dry_run: bool
) -> None:
    """Forward provider API keys from the operator's ~/.openclaw/.env into
    the LangSmith workspace via POST /api/v1/workspaces/current/secrets.

    Idempotency: GET current secrets first (LangSmith returns keys without
    values for security). For each configured key, if it's already present,
    mark unchanged. Otherwise POST upsert. Source values that are missing
    in the env file are skipped with a warning — operator may have rotated
    a key out without updating the IaC list.
    """
    print("\n=== phase 4: workspace secrets (provider API keys) ===")

    # Source values from ~/.openclaw/.env
    try:
        source_env = _load_openclaw_secrets()
    except FileNotFoundError as exc:
        print(f"{ERROR} {exc}")
        counters.errored += 1
        return

    # Current state in LangSmith — endpoint returns key list, no values.
    endpoint = config["endpoint"]
    api_key = config["api_key"]
    workspace_id = config["workspace_id"]

    import requests

    headers = {
        "x-api-key": api_key,
        "X-Tenant-Id": workspace_id,
        "Content-Type": "application/json",
    }
    secrets_url = f"{endpoint}/api/v1/workspaces/current/secrets"

    try:
        resp = requests.get(secrets_url, headers=headers, timeout=10)
        resp.raise_for_status()
        existing_keys = {entry["key"] for entry in resp.json()}
    except Exception as exc:
        print(f"{ERROR} GET workspace secrets failed: {exc}")
        counters.errored += 1
        return

    # Build payload — skip keys missing from source env
    upserts: list[dict] = []
    for spec in _projects_mod.WORKSPACE_SECRETS:
        key = spec["key"]
        value = source_env.get(key, "")
        if not value:
            print(f"{SKIPPED} {key!r} not present in source env — skipped")
            counters.skipped += 1
            continue

        if key in existing_keys:
            # Already loaded — re-upserting would rotate the value silently.
            # Treat as unchanged unless operator forces a re-apply by
            # deleting the key first (UI or via direct DELETE).
            print(f"{UNCHANGED} {key!r} already loaded in workspace")
            counters.unchanged += 1
            continue

        if dry_run:
            print(f"{DRY} would load {key!r} from source env into workspace")
            counters.would_change += 1
            continue

        upserts.append({"key": key, "value": value})

    # Single batched POST per LangSmith's upsert API.
    if not upserts:
        return
    try:
        resp = requests.post(secrets_url, headers=headers, json=upserts, timeout=15)
        resp.raise_for_status()
        for u in upserts:
            print(f"{CREATED} {u['key']!r} loaded into workspace")
            counters.created += 1
    except Exception as exc:
        print(f"{ERROR} POST workspace secrets failed: {exc}")
        counters.errored += 1


# ---------------------------------------------------------------------------
# Phase 5: workspace prompts — agent system prompts as versioned artifacts
# ---------------------------------------------------------------------------


def apply_workspace_prompts(
    client: Any, config: dict[str, str], counters: Counters, *, dry_run: bool
) -> None:
    """Push agent system prompts to LangSmith's prompt library so they
    can be played with in the playground and referenced by online
    evaluators. Idempotent via content-hash compare: only commits a new
    revision when the local source file's content differs from the
    current LangSmith commit.

    LangSmith's prompt model: each prompt has a stable identifier
    (``<workspace>/<name>``) and a chain of immutable commits. Pushing
    "the same content twice" creates duplicate commits — not what we
    want. So we read the current latest commit, compare to local
    content, only push when they diverge.
    """
    from pathlib import Path

    print("\n=== phase 5: workspace prompts (agent personas) ===")

    repo_root = Path(__file__).resolve().parent.parent.parent

    for spec in _projects_mod.WORKSPACE_PROMPTS:
        name = spec["name"]
        src = repo_root / spec["source_path"]

        if not src.exists():
            print(f"{ERROR} prompt {name!r} source file missing: {src}")
            counters.errored += 1
            continue

        local_content = src.read_text(encoding="utf-8")

        # Try to pull current LangSmith content. If the prompt doesn't
        # exist yet, ``pull_prompt`` raises — we treat as "needs create".
        try:
            current = client.pull_prompt(name)
            # ``pull_prompt`` returns a langchain ChatPromptTemplate or
            # similar. Extract the system message text. Convention:
            # we push as a single ``ChatPromptTemplate`` with one
            # ``SystemMessagePromptTemplate``.
            current_text = _extract_system_text(current)
        except Exception:
            current_text = None  # not yet created

        if current_text == local_content:
            print(f"{UNCHANGED} prompt {name!r} already at current content")
            counters.unchanged += 1
            continue

        if dry_run:
            verb = "create" if current_text is None else "commit revision to"
            print(f"{DRY} would {verb} prompt {name!r} ({len(local_content)} chars)")
            counters.would_change += 1
            continue

        try:
            # Build a langchain ChatPromptTemplate so the LangSmith UI
            # renders it as a chat-shape prompt (system message). This
            # is the shape the playground + evaluators expect.
            from langchain_core.prompts import (
                ChatPromptTemplate,
                SystemMessagePromptTemplate,
            )

            template = ChatPromptTemplate.from_messages(
                [SystemMessagePromptTemplate.from_template(local_content)]
            )

            client.push_prompt(
                name,
                object=template,
                description=spec["description"],
                tags=spec["tags"],
            )

            verb = "created" if current_text is None else "committed revision"
            print(f"{CREATED if current_text is None else UPDATED} prompt {name!r} {verb}")
            if current_text is None:
                counters.created += 1
            else:
                counters.updated += 1
        except Exception as exc:
            print(f"{ERROR} push_prompt({name!r}) failed: {exc}")
            counters.errored += 1


def _extract_system_text(prompt_obj: Any) -> str | None:
    """Pull the system message text out of a pulled LangSmith prompt.

    LangSmith's ``pull_prompt`` returns a langchain object — usually a
    ``ChatPromptTemplate`` whose first message is the system prompt.
    We avoid hard-typing this since langchain version skew can change
    the exact shape; duck-type via attribute walk instead.
    """
    if prompt_obj is None:
        return None

    # ChatPromptTemplate path: .messages -> list of message templates,
    # each of which has a .prompt.template (string) for the formatted
    # text. SystemMessagePromptTemplate is the standard shape.
    messages = getattr(prompt_obj, "messages", None)
    if messages:
        for m in messages:
            inner = getattr(m, "prompt", None)
            template = getattr(inner, "template", None) if inner else None
            if template is not None:
                return str(template)

    # PromptTemplate fallback (single-string prompts):
    template = getattr(prompt_obj, "template", None)
    if template is not None:
        return str(template)

    return None


# ---------------------------------------------------------------------------
# Phase 6: online evaluators — POST /v1/platform/evaluators
# ---------------------------------------------------------------------------


def apply_evaluators(
    client: Any, config: dict[str, str], counters: Counters, *, dry_run: bool
) -> None:
    """Create LLM-as-judge evaluators referencing prompt-library prompts.

    LangSmith's evaluator API:
      - GET /v1/platform/evaluators           list current
      - POST /v1/platform/evaluators          create new
      - PATCH /v1/platform/evaluators/{id}    update existing (variable map etc.)

    Idempotency: GET current, match on ``name``. If the evaluator exists
    AND its prompt_repo_handle / variable_mapping matches our config,
    skip. Otherwise create-or-update.

    NOTE: This phase creates the evaluator DEFINITION. It does NOT
    attach the evaluator to a project (that's the run-rules layer,
    handled separately). After this phase, the operator goes to the
    LangSmith UI -> Evaluators -> Online -> "attach to project" to
    wire up which spans the evaluator fires on. When LangSmith's
    run-rules API stabilises around declarative config, add a phase 7.
    """
    print("\n=== phase 6: online evaluators ===")

    endpoint = config["endpoint"]
    api_key = config["api_key"]
    workspace_id = config["workspace_id"]

    import requests

    headers = {
        "x-api-key": api_key,
        "X-Tenant-Id": workspace_id,
        "Content-Type": "application/json",
    }
    list_url = f"{endpoint}/v1/platform/evaluators"

    try:
        resp = requests.get(list_url, headers=headers, timeout=10)
        resp.raise_for_status()
        existing = {e["name"]: e for e in resp.json().get("evaluators", [])}
    except Exception as exc:
        print(f"{ERROR} GET evaluators failed: {exc}")
        counters.errored += 1
        return

    for ev in _projects_mod.EVALUATORS:
        name = ev["name"]
        if name in existing:
            current = existing[name]
            current_llm = current.get("llm_evaluator") or {}
            mapping_match = current_llm.get("variable_mapping") == ev["variable_mapping"]
            handle_match = current_llm.get("prompt_repo_handle") == ev["prompt_repo_handle"]
            if mapping_match and handle_match:
                print(f"{UNCHANGED} evaluator {name!r} already at current config")
                counters.unchanged += 1
                continue

            if dry_run:
                print(f"{DRY} would PATCH evaluator {name!r} (handle/mapping diff)")
                counters.would_change += 1
                continue

            patch_url = f"{endpoint}/v1/platform/evaluators/{current['id']}"
            patch_body = {
                "llm_evaluator": {
                    "prompt_repo_handle": ev["prompt_repo_handle"],
                    "commit_hash_or_tag": ev["commit_hash_or_tag"],
                    "variable_mapping": ev["variable_mapping"],
                },
            }
            try:
                resp = requests.patch(patch_url, headers=headers, json=patch_body, timeout=15)
                resp.raise_for_status()
                print(f"{UPDATED} evaluator {name!r} updated")
                counters.updated += 1
            except Exception as exc:
                print(f"{ERROR} PATCH evaluator {name!r} failed: {exc}")
                counters.errored += 1
            continue

        # Not present — CREATE
        if dry_run:
            print(f"{DRY} would CREATE evaluator {name!r}")
            counters.would_change += 1
            continue

        create_body = {
            "name": name,
            "type": ev["type"],
            "llm_evaluator": {
                "prompt_repo_handle": ev["prompt_repo_handle"],
                "commit_hash_or_tag": ev["commit_hash_or_tag"],
                "variable_mapping": ev["variable_mapping"],
            },
        }
        try:
            resp = requests.post(list_url, headers=headers, json=create_body, timeout=15)
            resp.raise_for_status()
            print(f"{CREATED} evaluator {name!r}")
            counters.created += 1
        except Exception as exc:
            print(f"{ERROR} POST evaluator {name!r} failed: {exc}")
            if hasattr(exc, "response") and exc.response is not None:
                print(f"    body: {exc.response.text[:300]}")
            counters.errored += 1


# ---------------------------------------------------------------------------
# Phase 7: datasets — golden-recall, etc.
# ---------------------------------------------------------------------------


def apply_datasets(
    client: Any, config: dict[str, str], counters: Counters, *, dry_run: bool
) -> None:
    """Create datasets and populate with examples from the seed modules."""
    print("\n=== phase 7: datasets ===")

    for ds in _datasets_mod.DATASETS:
        name = ds["name"]

        # Lazy-import the seed module
        try:
            seed_mod = importlib.import_module(f"ops.langsmith.datasets.{ds['examples_module']}")
            examples = seed_mod.EXAMPLES
        except (ImportError, AttributeError) as exc:
            print(f"{ERROR} dataset {name!r} seed import failed: {exc}")
            counters.errored += 1
            continue

        # Does it exist?
        already_exists = client.has_dataset(dataset_name=name)

        if not already_exists:
            if dry_run:
                print(f"{DRY} would create dataset {name!r} with {len(examples)} examples")
                counters.would_change += 1
                continue
            try:
                dataset = client.create_dataset(dataset_name=name, description=ds["description"])
                client.create_examples(
                    inputs=[ex["inputs"] for ex in examples],
                    outputs=[ex["outputs"] for ex in examples],
                    dataset_id=dataset.id,
                )
                print(f"{CREATED} dataset {name!r} with {len(examples)} examples")
                counters.created += 1
            except Exception as exc:
                print(f"{ERROR} create dataset {name!r} failed: {exc}")
                counters.errored += 1
            continue

        # Exists — count examples + diff
        try:
            existing_examples = list(client.list_examples(dataset_name=name))
        except Exception as exc:
            print(f"{ERROR} list_examples({name!r}) failed: {exc}")
            counters.errored += 1
            continue

        if len(existing_examples) == len(examples):
            # Heuristic: same count == same content for the v1 pass.
            # Future: hash inputs and diff for content drift.
            print(f"{UNCHANGED} dataset {name!r} ({len(examples)} examples)")
            counters.unchanged += 1
            continue

        if dry_run:
            print(
                f"{DRY} would update dataset {name!r}: "
                f"{len(existing_examples)} existing → {len(examples)} configured"
            )
            counters.would_change += 1
            continue

        # Add missing examples — LangSmith doesn't dedup by content so we
        # only append if count differs. Future: support full upsert/delete.
        try:
            dataset = client.read_dataset(dataset_name=name)
            client.create_examples(
                inputs=[ex["inputs"] for ex in examples[len(existing_examples) :]],
                outputs=[ex["outputs"] for ex in examples[len(existing_examples) :]],
                dataset_id=dataset.id,
            )
            print(
                f"{UPDATED} dataset {name!r}: appended "
                f"{len(examples) - len(existing_examples)} examples"
            )
            counters.updated += 1
        except Exception as exc:
            print(f"{ERROR} update dataset {name!r} failed: {exc}")
            counters.errored += 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes; make no API writes.",
    )
    ap.add_argument(
        "--phase",
        choices=[
            "project",
            "feedback",
            "queues",
            "secrets",
            "prompts",
            "evaluators",
            "datasets",
            "all",
        ],
        default="all",
        help="Run a subset of phases.",
    )
    args = ap.parse_args(argv)

    print(f"langsmith provision  mode={'DRY-RUN' if args.dry_run else 'APPLY'}")
    print(f"phases: {args.phase}")

    client, config = make_client()
    print(f"endpoint:    {config['endpoint']}")
    print(f"project:     {config['project']!r}")
    print(f"workspace:   {config['workspace_id']}")

    counters = Counters()

    if args.phase in ("all", "project"):
        apply_project_settings(client, config, counters, dry_run=args.dry_run)
    if args.phase in ("all", "feedback"):
        apply_feedback_configs(client, config, counters, dry_run=args.dry_run)
    if args.phase in ("all", "queues"):
        apply_annotation_queues(client, config, counters, dry_run=args.dry_run)
    if args.phase in ("all", "secrets"):
        apply_workspace_secrets(client, config, counters, dry_run=args.dry_run)
    if args.phase in ("all", "prompts"):
        apply_workspace_prompts(client, config, counters, dry_run=args.dry_run)
    if args.phase in ("all", "evaluators"):
        apply_evaluators(client, config, counters, dry_run=args.dry_run)
    if args.phase in ("all", "datasets"):
        apply_datasets(client, config, counters, dry_run=args.dry_run)

    print(f"\n=== summary ===\n{counters}")
    return 1 if counters.errored else 0


if __name__ == "__main__":
    sys.exit(main())
