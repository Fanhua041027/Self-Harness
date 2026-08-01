from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def load_acceptance_module():
    path = Path(__file__).resolve().parents[1] / "acceptance" / "scripts" / "run_acceptance_gate.py"
    spec = importlib.util.spec_from_file_location("self_harness_acceptance_gate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_invalid_infrastructure_trial_cannot_enter_acceptance() -> None:
    gate = load_acceptance_module()
    repeat = {
        "repeat": 1,
        "passed": 0,
        "total": 1,
        "case_results": [{"case_id": "sample", "status": "invalid", "passed": False}],
    }

    with pytest.raises(ValueError, match="invalid infrastructure trials: sample"):
        gate.repeat_metric(repeat, split="heldout")
