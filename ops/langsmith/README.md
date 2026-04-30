# LangSmith — infrastructure as code

This directory holds the **declarative configuration** for our LangSmith
project — datasets, feedback configs, project settings, annotation
queues. Edit the Python files; re-run `make langsmith-provision` to
apply. The code is the source of truth, not the LangSmith web UI.

## Why this exists

The LangSmith dashboard is fine for one-off setup, but six weeks from
now you won't remember which evaluator does what or why. Code-as-config
fixes that:

- **Discoverable.** Every evaluator, dataset, and rule is in this repo.
- **Reviewable.** Changes go through PRs alongside the agent code.
- **Reproducible.** Wipe the LangSmith project and re-provision in
  one command.
- **Reversible.** Rollbacks are `git revert` + `make langsmith-provision`.

## Layout

```
ops/langsmith/
├── README.md                  ← you are here
├── provision.py               ← entry point — runs every apply phase
├── client.py                  ← authenticated Client factory
├── projects.py                ← project settings (retention, tags, description)
├── datasets.py                ← dataset definitions (one dict per dataset)
├── datasets/                  ← seed example payloads, one .py per dataset
│   └── golden_recall.py       ← canonical recall question/expected pairs
└── tests/
    └── test_provision.py      ← idempotency + dry-run tests
```

## Running

**Pre-flight:** make sure `secrets/langsmith-provisioning.env` is filled
in with the service key, endpoint, project name, and workspace ID.

```bash
# Preview what would change (no API writes)
make langsmith-plan

# Apply — idempotent, safe to re-run
make langsmith-provision
```

Output is a per-resource summary: `created N`, `updated M`, `unchanged K`.

## Adding a new dataset

1. Create `ops/langsmith/datasets/<name>.py` with a module-level
   `EXAMPLES = [...]` list of `{inputs, outputs}` dicts.
2. Register in `ops/langsmith/datasets.py` `DATASETS` list:
   ```python
   {"name": "<name>", "description": "...", "examples_module": "<name>"}
   ```
3. `make langsmith-plan` then `make langsmith-provision`.

## Adding a feedback config

Edit `ops/langsmith/projects.py` `FEEDBACK_CONFIGS` list. Each entry
defines a feedback dimension (e.g., "recall accuracy 0-1", "naturalness
1-5"). These appear in the LangSmith UI as click-to-rate buttons on
every trace.

## What lives in the UI, not in code

Some LangSmith surfaces aren't fully API-exposed (yet). Configure these
in the dashboard and document the choice in `projects.py` as comments:

- **Online evaluators** (auto-run on every new trace) — REST API but
  schema not stable. Track in `projects.py::ONLINE_EVAL_NOTES` so the
  *intent* is in code even if the apply isn't.
- **Custom dashboards** — UI-only.
- **Slack/PagerDuty integrations for alerts** — UI-only.

## Re-applying after credential rotation

Rotate the service key in the LangSmith dashboard, paste the new value
into `secrets/langsmith-provisioning.env`, re-run `make langsmith-provision`.
The script is idempotent — it'll find existing resources by name and
update in place.
