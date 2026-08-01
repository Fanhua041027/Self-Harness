from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "eval/plugins/mechanism_smoke.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("mechanism_smoke", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_evaluator_contract_hides_regression_details():
    plugin = load_plugin()
    result = plugin.evaluate("baseline", {"execution": "recover artifact retry adapt verify rollback"})
    assert set(result) == {"model_view", "private_metrics"}
    assert set(result["model_view"]) == {"failures", "clusters"}
    assert "regression-preserve-verification" not in repr(result["model_view"])
    assert result["private_metrics"]["splits"]["regression"] == {"passed": 1, "total": 1}


def test_smoke_evaluator_materializes_and_executes_candidates():
    plugin = load_plugin()
    result = plugin.evaluate("candidates", {"proposals": [{
        "proposal_id": "p1", "surface": "textual_harness",
        "harness_text": "recover artifact retry adapt verify rollback",
    }]})
    candidate = result["candidates"][0]
    assert candidate["materialized"] is True and candidate["executed"] is True
    assert len(candidate["candidate_hash"]) == 64
    assert set(candidate["metrics"]["splits"]) == {"evolution", "regression"}


def test_smoke_evaluator_rejects_unknown_stage():
    plugin = load_plugin()
    with pytest.raises(ValueError, match="unsupported"):
        plugin.evaluate("sealed", {})
