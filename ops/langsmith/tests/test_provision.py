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


@pytest.mark.parametrize("phase", ["project", "feedback", "queues", "datasets", "all"])
def test_provision_main_accepts_phase_arg(phase, monkeypatch) -> None:
    """Argparse contract — every phase value documented in --help must
    be accepted without error."""
    monkeypatch.setattr(
        provision,
        "make_client",
        lambda: (MagicMock(), {"endpoint": "x", "project": "y", "workspace_id": "z"}),
    )
    # Stub out every apply_* so we don't hit the network
    for fn in (
        "apply_project_settings",
        "apply_feedback_configs",
        "apply_annotation_queues",
        "apply_datasets",
    ):
        monkeypatch.setattr(provision, fn, lambda *a, **kw: None)

    rc = provision.main(["--dry-run", "--phase", phase])
    assert rc == 0
