"""Declarative dataset registry — one entry per LangSmith dataset.

Each entry's ``examples_module`` names a module in this package
exporting a top-level ``EXAMPLES: list[dict]``. Provisioning is
idempotent:

- Dataset doesn't exist → create + populate
- Dataset exists, count differs → upsert
- Dataset exists, no diff → skip

Adding a dataset:
  1. Create ``ops/langsmith/datasets/<name>.py`` with EXAMPLES
  2. Register here in DATASETS
  3. Re-run ``make langsmith-provision``
"""

from __future__ import annotations

from typing import TypedDict


class DatasetConfig(TypedDict):
    name: str
    description: str
    examples_module: str
    """Module name in this package — without the .py suffix."""


DATASETS: list[DatasetConfig] = [
    {
        "name": "golden-recall",
        "description": (
            "Canonical recall question/expected pairs. Pins the right tool "
            "selection and minimum content-overlap signal for each example. "
            "Used as ground-truth baseline for online recall_accuracy evals."
        ),
        "examples_module": "golden_recall",
    },
]
