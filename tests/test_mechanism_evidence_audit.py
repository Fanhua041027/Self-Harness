from __future__ import annotations

import json
from pathlib import Path

import pytest

from paper import audit_mechanism_evidence as audit


def write_result(path: Path, *, candidate: bool) -> None:
    cases = [
        {
            "case_id": "target",
            "repeat": 1,
            "status": "passed" if candidate else "failed",
            "passed": candidate,
            "reward": 1.0 if candidate else 0.0,
        },
        {
            "case_id": "outage",
            "repeat": 1,
            "status": "failed",
            "passed": False,
            "reward": None,
        },
    ]
    path.write_text(
        json.dumps({"splits": {"train": [{"repeat": 1, "case_results": cases}]}}),
        encoding="utf-8",
    )


def test_build_audit_confirms_only_valid_direct_target(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    write_result(baseline, candidate=False)
    write_result(candidate, candidate=True)
    proposal = {
        "proposal_id": "candidate",
        "mechanism": "fixture",
        "metadata": {
            "selected_cluster_id": "cause / root / mechanism",
            "exact_hook": "build_instruction",
            "expected_affected_cases": ["target#repeat-01"],
        },
    }

    result = audit.build_audit(
        proposal=proposal,
        baseline_path=baseline,
        candidate_path=candidate,
    )

    assert result["direct_target_confirmed"] is True
    assert result["transition_counts"]["fail-to-pass"] == 1
    assert result["transition_counts"]["invalid-involved"] == 1


def test_invalid_direct_target_is_not_confirmed(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    write_result(baseline, candidate=False)
    write_result(candidate, candidate=True)
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    payload["splits"]["train"][0]["case_results"][0]["reward"] = None
    baseline.write_text(json.dumps(payload), encoding="utf-8")

    result = audit.build_audit(
        proposal={
            "proposal_id": "candidate",
            "metadata": {"expected_affected_cases": ["target#repeat-01"]},
        },
        baseline_path=baseline,
        candidate_path=candidate,
    )

    assert result["direct_target_confirmed"] is False


def test_load_json_accepts_bom_and_rejects_nested_duplicate_key(tmp_path: Path) -> None:
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf{\"ok\": true}")
    assert audit.load_json(bom)["ok"] is True

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"outer": {"value": 1, "value": 2}}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        audit.load_json(duplicate)
