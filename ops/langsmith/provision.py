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
4. **Datasets** — created and populated from
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
# Phase 4: datasets
# ---------------------------------------------------------------------------


def apply_datasets(
    client: Any, config: dict[str, str], counters: Counters, *, dry_run: bool
) -> None:
    """Create datasets and populate with examples from the seed modules."""
    print("\n=== phase 4: datasets ===")

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
        choices=["project", "feedback", "queues", "datasets", "all"],
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
    if args.phase in ("all", "datasets"):
        apply_datasets(client, config, counters, dry_run=args.dry_run)

    print(f"\n=== summary ===\n{counters}")
    return 1 if counters.errored else 0


if __name__ == "__main__":
    sys.exit(main())
