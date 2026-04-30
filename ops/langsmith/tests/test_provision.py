"""Smoke tests for the LangSmith provisioning script.

These tests don't hit the real LangSmith API — they exercise the
declarative-config import paths, idempotency math, and dry-run
counter accumulation. Real-API smoke is the manual ``make
langsmith-plan`` you run before the apply.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest

from ops.langsmith import datasets as datasets_mod
from ops.langsmith import projects as projects_mod
from ops.langsmith import provision


def test_project_settings_is_well_formed() -> None:
    """The PROJECT_SETTINGS constant must have description + metadata
    keys so apply_project_settings doesn't KeyError. Catches accidental
    deletions during edits."""
    assert "description" in projects_mod.PROJECT_SETTINGS
    assert "metadata" in projects_mod.PROJECT_SETTINGS
    assert isinstance(projects_mod.PROJECT_SETTINGS["metadata"], dict)


def test_feedback_configs_have_unique_keys() -> None:
    """Two configs sharing a feedback_key would collide on apply.
    Catch it at test time, not at the moment we hit a 4xx from the API."""
    keys = [fc["feedback_key"] for fc in projects_mod.FEEDBACK_CONFIGS]
    assert len(keys) == len(set(keys)), f"duplicate feedback_keys: {keys}"


def test_feedback_config_score_specs_are_valid_shape() -> None:
    """Each feedback_score_spec must be either continuous (with min/max)
    or categorical (with categories list). Wrong shape = silent ignore
    on the API side, no signal that the config is dead."""
    for fc in projects_mod.FEEDBACK_CONFIGS:
        spec = fc["feedback_score_spec"]
        assert spec["type"] in ("continuous", "categorical"), spec
        if spec["type"] == "continuous":
            assert "min" in spec and "max" in spec, spec
        else:
            assert "categories" in spec, spec
            for cat in spec["categories"]:
                assert "value" in cat and "label" in cat, cat


def test_annotation_queues_have_unique_names() -> None:
    """Same uniqueness contract as feedback configs — name collisions
    would cause silent overwrites on apply."""
    names = [q["name"] for q in projects_mod.ANNOTATION_QUEUES]
    assert len(names) == len(set(names)), f"duplicate queue names: {names}"


def test_dataset_examples_modules_are_importable() -> None:
    """Every dataset references a module under ops/langsmith/datasets/.
    Missing modules surface as ImportError at provision time — better
    to fail the test suite first."""
    for ds in datasets_mod.DATASETS:
        mod_name = ds["examples_module"]
        mod = importlib.import_module(f"ops.langsmith.datasets.{mod_name}")
        assert hasattr(mod, "EXAMPLES"), f"{mod_name} must export module-level EXAMPLES"
        assert isinstance(mod.EXAMPLES, list), (
            f"{mod_name}.EXAMPLES must be a list, got {type(mod.EXAMPLES)}"
        )


def test_golden_recall_examples_have_expected_tool_field() -> None:
    """The golden-recall dataset's contract: every example has an
    expected_tool. If we drift away from that shape, downstream evals
    that compare actual vs expected break silently."""
    from ops.langsmith.datasets import golden_recall

    for i, ex in enumerate(golden_recall.EXAMPLES):
        assert "inputs" in ex and "outputs" in ex, f"example {i} missing keys"
        assert "user_question" in ex["inputs"], f"example {i} missing user_question"
        assert "expected_tool" in ex["outputs"], f"example {i} missing expected_tool"


# ---------------------------------------------------------------------------
# Counter / dry-run smoke
# ---------------------------------------------------------------------------


def test_counters_initial_state_is_zero() -> None:
    c = provision.Counters()
    assert c.created == 0
    assert c.updated == 0
    assert c.unchanged == 0
    assert c.skipped == 0
    assert c.errored == 0
    assert c.would_change == 0


def test_dry_run_apply_project_settings_increments_would_change(monkeypatch) -> None:
    """When the project's current state differs and we're in dry-run mode,
    apply_project_settings must NOT call update_project and must
    increment would_change. Catches a regression where dry-run accidentally
    becomes apply-mode."""
    fake_client = MagicMock()
    fake_project = MagicMock()
    fake_project.id = "proj-id"
    fake_project.description = "old description"
    fake_project.metadata = {}
    fake_client.read_project.return_value = fake_project

    counters = provision.Counters()
    provision.apply_project_settings(fake_client, {"project": "test"}, counters, dry_run=True)

    assert counters.would_change == 1
    fake_client.update_project.assert_not_called()


def test_apply_when_project_already_matches_increments_unchanged() -> None:
    """No-op path — when the project description + metadata already
    match the configured target, we must increment unchanged (NOT
    update_project). The whole IaC value is "running again is safe"."""
    target = projects_mod.PROJECT_SETTINGS

    fake_client = MagicMock()
    fake_project = MagicMock()
    fake_project.id = "proj-id"
    fake_project.description = target["description"]
    fake_project.metadata = dict(target["metadata"])
    fake_client.read_project.return_value = fake_project

    counters = provision.Counters()
    provision.apply_project_settings(fake_client, {"project": "test"}, counters, dry_run=False)

    assert counters.unchanged == 1
    fake_client.update_project.assert_not_called()


def test_apply_feedback_configs_skips_existing(monkeypatch) -> None:
    """Idempotency check: if every configured feedback config already
    exists on the LangSmith side, nothing should be created."""
    fake_existing = [
        MagicMock(feedback_key=fc["feedback_key"]) for fc in projects_mod.FEEDBACK_CONFIGS
    ]

    fake_client = MagicMock()
    fake_client.list_feedback_configs.return_value = fake_existing

    counters = provision.Counters()
    provision.apply_feedback_configs(fake_client, {}, counters, dry_run=False)

    assert counters.unchanged == len(projects_mod.FEEDBACK_CONFIGS)
    assert counters.created == 0
    fake_client.create_feedback_config.assert_not_called()


@pytest.mark.parametrize("phase", ["project", "feedback", "queues", "secrets", "datasets", "all"])
def test_provision_main_accepts_phase_arg(phase, monkeypatch) -> None:
    """Argparse contract — every phase value documented in --help must
    be accepted without error."""
    monkeypatch.setattr(
        provision,
        "make_client",
        lambda: (
            MagicMock(),
            {"endpoint": "x", "project": "y", "workspace_id": "z", "api_key": "k"},
        ),
    )
    # Stub out every apply_* so we don't hit the network
    for fn in (
        "apply_project_settings",
        "apply_feedback_configs",
        "apply_annotation_queues",
        "apply_workspace_secrets",
        "apply_datasets",
    ):
        monkeypatch.setattr(provision, fn, lambda *a, **kw: None)

    rc = provision.main(["--dry-run", "--phase", phase])
    assert rc == 0


# ---------------------------------------------------------------------------
# Phase 4: workspace secrets — IaC contract
# ---------------------------------------------------------------------------


def test_workspace_secrets_have_unique_keys() -> None:
    """Two entries with the same key would race on apply (one wins, no
    deterministic outcome). Catch at test time."""
    keys = [s["key"] for s in projects_mod.WORKSPACE_SECRETS]
    assert len(keys) == len(set(keys)), f"duplicate WORKSPACE_SECRETS keys: {keys}"


def test_workspace_secrets_use_canonical_env_var_names() -> None:
    """LangSmith's online evaluators reach for env-style names
    (OPENAI_API_KEY etc.). Configs that use freeform names work but
    require manual binding in every evaluator config — defeats the
    point of workspace-level secrets. Lock to the standard convention."""
    canonical_suffixes = ("_API_KEY", "_TOKEN", "_SECRET")
    for s in projects_mod.WORKSPACE_SECRETS:
        key = s["key"]
        assert key == key.upper(), f"{key!r} must be uppercase"
        assert any(key.endswith(suf) for suf in canonical_suffixes), (
            f"{key!r} should end with {canonical_suffixes}"
        )


def test_workspace_secrets_skips_missing_source_env(monkeypatch) -> None:
    """When a configured key isn't present in the source env file,
    we skip with a warning rather than POST an empty value (LangSmith
    would either 422 or store an empty cred — both bad)."""
    # Source env missing every WORKSPACE_SECRETS entry
    monkeypatch.setattr(provision, "_load_openclaw_secrets", lambda: {})

    # Mock requests.get to return empty current state
    fake_resp = MagicMock()
    fake_resp.json.return_value = []
    fake_resp.raise_for_status.return_value = None
    fake_post = MagicMock()
    fake_post.raise_for_status.return_value = None

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **kw: fake_resp)
    monkeypatch.setattr(requests, "post", lambda *a, **kw: fake_post)

    counters = provision.Counters()
    provision.apply_workspace_secrets(
        client=MagicMock(),
        config={"endpoint": "x", "api_key": "k", "workspace_id": "w"},
        counters=counters,
        dry_run=False,
    )

    # Every configured secret is missing → all skipped, none created
    assert counters.skipped == len(projects_mod.WORKSPACE_SECRETS)
    assert counters.created == 0


def test_workspace_secrets_skips_already_loaded(monkeypatch) -> None:
    """If LangSmith already has the configured keys, we DON'T re-POST.
    Re-POSTing would silently rotate the stored value to whatever the
    source env currently has — invisible churn that could break a
    correctly-configured workspace."""
    monkeypatch.setattr(
        provision,
        "_load_openclaw_secrets",
        lambda: {s["key"]: f"value-of-{s['key']}" for s in projects_mod.WORKSPACE_SECRETS},
    )

    # All configured keys already loaded
    fake_resp = MagicMock()
    fake_resp.json.return_value = [{"key": s["key"]} for s in projects_mod.WORKSPACE_SECRETS]
    fake_resp.raise_for_status.return_value = None
    post_calls = []
    fake_post = MagicMock(side_effect=lambda *a, **kw: post_calls.append((a, kw)))

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **kw: fake_resp)
    monkeypatch.setattr(requests, "post", fake_post)

    counters = provision.Counters()
    provision.apply_workspace_secrets(
        client=MagicMock(),
        config={"endpoint": "x", "api_key": "k", "workspace_id": "w"},
        counters=counters,
        dry_run=False,
    )

    assert counters.unchanged == len(projects_mod.WORKSPACE_SECRETS)
    assert counters.created == 0
    assert post_calls == []  # no POST should fire


def test_workspace_secrets_dry_run_does_not_write(monkeypatch) -> None:
    """In dry-run mode, even when keys would be created, no POST fires
    and would_change increments. This is the safety contract for
    `make langsmith-plan`."""
    monkeypatch.setattr(
        provision,
        "_load_openclaw_secrets",
        lambda: {s["key"]: "v" for s in projects_mod.WORKSPACE_SECRETS},
    )

    fake_resp = MagicMock()
    fake_resp.json.return_value = []
    fake_resp.raise_for_status.return_value = None
    post_calls = []

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **kw: fake_resp)
    monkeypatch.setattr(requests, "post", lambda *a, **kw: post_calls.append((a, kw)))

    counters = provision.Counters()
    provision.apply_workspace_secrets(
        client=MagicMock(),
        config={"endpoint": "x", "api_key": "k", "workspace_id": "w"},
        counters=counters,
        dry_run=True,
    )

    assert counters.would_change == len(projects_mod.WORKSPACE_SECRETS)
    assert counters.created == 0
    assert post_calls == []  # dry-run must not write
