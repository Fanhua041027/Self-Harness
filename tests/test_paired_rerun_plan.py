from __future__ import annotations

import json
from pathlib import Path

import pytest

from paper import build_paired_rerun_plan


def write_result(path: Path, rows: list[dict]) -> None:
    path.write_text(
        json.dumps({"splits": {"train": [{"repeat": 1, "case_results": rows}]}}),
        encoding="utf-8",
    )


def row(case_id: str, *, valid: bool) -> dict:
    return {
        "case_id": case_id,
        "repeat": 1,
        "passed": False,
        "status": "failed",
        "reward": 0.0 if valid else None,
    }


def test_build_separates_bridge_and_both_invalid_cells(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    write_result(
        baseline,
        [row("both-valid", valid=True), row("base-only", valid=False), row("cand-only", valid=True), row("both-bad", valid=False)],
    )
    write_result(
        candidate,
        [row("both-valid", valid=True), row("base-only", valid=True), row("cand-only", valid=False), row("both-bad", valid=False)],
    )

    plan = build_paired_rerun_plan.build(
        baseline_path=baseline,
        candidate_path=candidate,
        timeout_s=100,
        retries=2,
        concurrency=1,
    )

    assert plan["pair_counts"] == {
        "both_valid": 1,
        "baseline_invalid_only": 1,
        "candidate_invalid_only": 1,
        "both_invalid": 1,
    }
    assert plan["rerun_cells"] == 4
    assert plan["baseline_result_sha256"] == build_paired_rerun_plan.sha256_file(baseline)
    assert plan["candidate_result_sha256"] == build_paired_rerun_plan.sha256_file(candidate)
    assert plan["source_hashes_stable"] is True
    assert plan["phases"]["phase_1_one_sided_bridge"]["budget"]["cells"] == 2
    assert plan["phases"]["phase_2_both_sides_missing"]["budget"]["cells"] == 2

    output = tmp_path / "manifests"
    output.mkdir()
    build_paired_rerun_plan.write_cell_manifests(output, plan)
    baseline_phase1 = json.loads(
        (output / "phase_1_one_sided_bridge.baseline.json").read_text(encoding="utf-8")
    )
    assert baseline_phase1["format"] == build_paired_rerun_plan.CELL_MANIFEST_FORMAT
    assert baseline_phase1["baseline_result"] == str(baseline.resolve())
    assert baseline_phase1["candidate_result"] == str(candidate.resolve())
    assert baseline_phase1["baseline_result_sha256"] == plan["baseline_result_sha256"]
    assert baseline_phase1["candidate_result_sha256"] == plan["candidate_result_sha256"]
    assert baseline_phase1["source_hashes_stable"] is True
    assert baseline_phase1["cells"] == [
        {"split": "train", "repeat": 1, "case_id": "base-only"}
    ]


def test_build_rejects_result_change_during_plan_generation(monkeypatch, tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    write_result(baseline, [row("case", valid=False)])
    write_result(candidate, [row("case", valid=False)])
    snapshots = iter(
        [
            {str(baseline.resolve()): "before", str(candidate.resolve()): "before"},
            {str(baseline.resolve()): "after", str(candidate.resolve()): "before"},
        ]
    )
    monkeypatch.setattr(
        build_paired_rerun_plan,
        "stable_hash_snapshot",
        lambda _paths: next(snapshots),
    )

    with pytest.raises(ValueError, match="changed during analysis"):
        build_paired_rerun_plan.build(
            baseline_path=baseline,
            candidate_path=candidate,
            timeout_s=100,
            retries=2,
            concurrency=1,
        )
