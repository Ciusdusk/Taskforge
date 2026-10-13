"""Validate the documented scenario runner against real processes and persistence."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.integration
def test_reproducible_scenarios_and_durable_business_idempotency(tmp_path):
    project = Path(__file__).resolve().parents[1]
    output = tmp_path / "receipt.json"
    result = subprocess.run(
        [sys.executable, str(project / "examples/verify_scenarios.py"), "--output", str(output)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(project / "examples")},
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    receipt = json.loads(output.read_text())
    assert receipt["crash_without_idempotency"]["total_value"] == 14
    assert receipt["crash_with_idempotency"]["total_value"] == 7
    for name in ("crash_without_idempotency", "crash_with_idempotency"):
        crash = receipt[name]
        assert crash["crash_exit_code"] == -9
        assert [attempt["status"] for attempt in crash["recovered"]["attempts"]] == [
            "INTERRUPTED",
            "SUCCESS",
        ]
        assert crash["calls"][0]["job_id"] == crash["calls"][1]["job_id"]
    assert receipt["cleanup"] == {
        "temporary_directory_removed": True,
        "all_children_reaped": True,
    }
