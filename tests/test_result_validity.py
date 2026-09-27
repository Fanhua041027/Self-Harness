from __future__ import annotations

import sys
import json
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "eval" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from result_validity import (
    effective_invalid_cases,
    effective_invalid_reason,
    invalid_reason_category,
    load_json_object,
    main,
)


def test_verifier_pass_and_behavioral_failure_are_valid() -> None:
    assert effective_invalid_reason(
        {"status": "passed", "passed": True, "reward": 1.0}
    ) is None
    assert effective_invalid_reason(
        {"status": "failed", "passed": False, "reward": 0.0}
    ) is None


def test_legacy_failed_without_reward_is_infrastructure_invalid() -> None:
    reason = effective_invalid_reason(
        {
            "status": "failed",
            "passed": False,
            "reward": None,
            "trace_metadata": {"failure": "APIConnectionError: Connection error."},
        }
    )

    assert reason == "runtime failure: APIConnectionError: Connection error."


def test_inconsistent_positive_reward_and_failed_status_is_invalid() -> None:
    reason = effective_invalid_reason(
        {"status": "failed", "passed": False, "reward": 1.0}
    )

    assert reason == "inconsistent passed=False and reward=1.0"


def test_non_boolean_passed_is_invalid_instead_of_truthiness_coercion() -> None:
    assert effective_invalid_reason(
        {"status": "", "passed": "false", "reward": 0.0}
    ) == "non-boolean passed: 'false'"


def test_non_finite_reward_is_invalid() -> None:
    assert effective_invalid_reason(
        {"status": "failed", "passed": False, "reward": float("nan")}
    ) == "non-finite verifier reward: nan"


def test_huge_integer_reward_is_invalid_without_overflow(tmp_path: Path) -> None:
    assert effective_invalid_reason(
        {"status": "failed", "passed": False, "reward": 10**1000}
    ) == f"non-finite verifier reward: {10**1000!r}"


def test_shared_result_loader_accepts_bom_and_rejects_duplicate_keys(tmp_path: Path) -> None:
    valid = tmp_path / "valid.json"
    valid.write_text('{"status": "failed"}', encoding="utf-8-sig")
    assert load_json_object(valid) == {"status": "failed"}

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"outer": {"status": "failed", "status": "invalid"}}', encoding="utf-8")
    try:
        load_json_object(duplicate)
    except ValueError as exc:
        assert "duplicate JSON key: status" in str(exc)
    else:
        raise AssertionError("duplicate JSON key was accepted")


def test_result_validity_cli_emits_validated_object(tmp_path: Path, capsys) -> None:
    valid = tmp_path / "valid.json"
    valid.write_text('\ufeff{"queue": {"candidate_id": "demo"}}', encoding="utf-8")

    assert main(["--result", str(valid), "--emit-object"]) == 0
    assert json.loads(capsys.readouterr().out) == {"queue": {"candidate_id": "demo"}}


def test_old_non_harbor_fixture_without_reward_field_remains_compatible() -> None:
    assert effective_invalid_reason(
        {"status": "failed", "passed": False}
    ) is None


def test_strict_mode_requires_reward_field_without_breaking_legacy_mode() -> None:
    case = {"status": "failed", "passed": False}
    assert effective_invalid_reason(case) is None
    assert effective_invalid_reason(case, require_reward=True) == "missing verifier reward"


def test_effective_invalid_cases_preserves_cell_identity() -> None:
    result = {
        "splits": {
            "train": [
                {
                    "repeat": 2,
                    "case_results": [
                        {
                            "case_id": "outage",
                            "repeat": 2,
                            "status": "failed",
                            "passed": False,
                            "reward": None,
                        }
                    ],
                }
            ]
        }
    }

    assert effective_invalid_cases(result) == [
        {
            "split": "train",
            "repeat": 2,
            "case_id": "outage",
            "reason": "missing verifier reward",
            "reason_category": "missing_reward",
        }
    ]


def test_invalid_reason_categories_are_stable() -> None:
    assert invalid_reason_category("runtime failure: APIConnectionError") == "provider"
    assert invalid_reason_category("agent timeout after 900 seconds") == "timeout"
    assert invalid_reason_category("harbor trial has no verifier reward") == "missing_reward"
    assert invalid_reason_category("inconsistent passed=False and reward=1.0") == "inconsistent_result"
    assert invalid_reason_category("non-boolean passed: 'false'") == "inconsistent_result"
    assert invalid_reason_category("non-finite verifier reward: nan") == "inconsistent_result"
