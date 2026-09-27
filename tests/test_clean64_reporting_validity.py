from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def load_script(name: str):
    path = Path(__file__).resolve().parents[1] / "eval" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"self_harness_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def complete_payload(case: dict) -> dict:
    return {
        "splits": {
            split: [
                {
                    "repeat": repeat,
                    "passed": total * int(bool(case.get("passed"))),
                    "total": total,
                    "case_results": [
                        {**dict(case), "case_id": str(case.get("case_id", "case")).format(index=index)}
                        for index in range(total)
                    ],
                }
                for repeat in (1, 2)
            ]
            for split, total in (("train", 43), ("heldout", 21))
        }
    }


def test_final_report_rejects_duplicate_case_identity() -> None:
    report = load_script("build_clean64_final_report")
    payload = complete_payload({"case_id": "case-{index}", "status": "passed", "passed": True, "reward": 1.0})
    payload["splits"]["train"][0]["case_results"][1]["case_id"] = "case-0"
    with pytest.raises(RuntimeError, match="重复 case identity"):
        report.validate_complete(payload, label="fixture")


def test_final_report_rejects_baseline_final_case_set_mismatch() -> None:
    report = load_script("build_clean64_final_report")
    baseline = complete_payload({"case_id": "case-{index}", "status": "passed", "passed": True, "reward": 1.0})
    final = complete_payload({"case_id": "case-{index}", "status": "passed", "passed": True, "reward": 1.0})
    final["splits"]["heldout"][0]["case_results"][0]["case_id"] = "replacement"
    with pytest.raises(RuntimeError, match="case manifest"):
        report.validate_same_case_manifest(baseline, final, label="fixture")
def test_final_report_rejects_legacy_missing_reward() -> None:
    report = load_script("build_clean64_final_report")
    payload = complete_payload(
        {"case_id": "legacy-{index}", "status": "failed", "passed": False, "reward": None}
    )

    with pytest.raises(RuntimeError, match="effective-invalid trial"):
        report.validate_complete(payload, label="fixture")


def test_live_report_rejects_unexpected_case_identity() -> None:
    report = load_script("build_clean64_live_report")
    expected = {"train": ["known"], "heldout": []}
    records = [{"model": "model-not-configured", "split": "train", "repeat": 1, "case_id": "unknown"}]
    report.RUNS = {"model-not-configured": Path("unused")}
    with pytest.raises(RuntimeError, match="unexpected case identity"):
        report.validate_records(expected=expected, records=records)


    report = load_script("build_clean64_live_report")
    records = [
        {
            "model": "model",
            "split": "train",
            "status": "failed",
            "passed": False,
            "reward": None,
            "trace_metadata": None,
            "infrastructure_error": None,
        },
        {
            "model": "model",
            "split": "train",
            "status": "passed",
            "passed": True,
            "reward": 1.0,
            "trace_metadata": None,
            "infrastructure_error": None,
        },
    ]

    assert report.observed_rate(records, "model", "train") == (1.0, 1)
