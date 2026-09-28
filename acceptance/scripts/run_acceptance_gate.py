#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VALIDITY_DIR = ROOT / "eval" / "scripts"
if str(VALIDITY_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDITY_DIR))

from result_validity import (
    effective_invalid_reason,
    load_json_object,
    repeat_aggregate_consistency_error,
)

FORMAT = "self_harness.acceptance_gate.v1"
DEFAULT_SPLITS = ("train", "heldout")
DEFAULT_EXPECTED_REPEATS = 2


@dataclass(frozen=True)
class RepeatMetric:
    repeat: int
    passed: int
    total: int
    pass_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "repeat": self.repeat,
            "passed": self.passed,
            "total": self.total,
            "pass_rate": self.pass_rate,
        }


@dataclass(frozen=True)
class SplitComparison:
    split: str
    baseline_repeats: tuple[RepeatMetric, ...]
    candidate_repeats: tuple[RepeatMetric, ...]
    baseline_average_pass_rate: float
    candidate_average_pass_rate: float
    delta: float
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_average_pass_rate": self.baseline_average_pass_rate,
            "candidate_average_pass_rate": self.candidate_average_pass_rate,
            "delta": self.delta,
            "status": self.status,
            "baseline_repeats": [item.to_dict() for item in self.baseline_repeats],
            "candidate_repeats": [item.to_dict() for item in self.candidate_repeats],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the Self-Harness acceptance gate.")
    parser.add_argument("--baseline-result", required=True, type=Path)
    parser.add_argument("--candidate-result", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--verify-artifact",
        type=Path,
        help="Verify an existing acceptance artifact against the two source results without overwriting it.",
    )
    parser.add_argument("--split", action="append", help="Split to gate. Defaults to train and heldout.")
    parser.add_argument("--expected-repeats", type=int, default=DEFAULT_EXPECTED_REPEATS)
    args = parser.parse_args(argv)

    baseline_path = args.baseline_result.resolve()
    candidate_path = args.candidate_result.resolve()
    splits = tuple(args.split or DEFAULT_SPLITS)
    if args.verify_artifact is not None:
        if args.output is not None:
            parser.error("--output cannot be combined with --verify-artifact")
        artifact_path = args.verify_artifact.resolve()
        artifact_hash = sha256_file(artifact_path)
        artifact = load_json_object(artifact_path)
        verify_acceptance_artifact(
            artifact=artifact,
            baseline_result_path=baseline_path,
            candidate_result_path=candidate_path,
            artifact_path=artifact_path,
            artifact_sha256=artifact_hash,
            splits=splits,
            expected_repeats=args.expected_repeats,
        )
        print(f"verified acceptance artifact: {artifact_path}")
        return 0
    if args.output is None:
        parser.error("--output is required unless --verify-artifact is used")
    baseline_payload, baseline_hash = read_json_with_stable_hash(baseline_path)
    candidate_payload, candidate_hash = read_json_with_stable_hash(candidate_path)
    result = run_acceptance_gate(
        baseline_result=baseline_payload,
        candidate_result=candidate_payload,
        baseline_result_path=baseline_path,
        candidate_result_path=candidate_path,
        baseline_result_sha256=baseline_hash,
        candidate_result_sha256=candidate_hash,
        source_hashes_stable=True,
        splits=splits,
        expected_repeats=args.expected_repeats,
    )
    require_stable_hash(baseline_path, baseline_hash, "baseline result")
    require_stable_hash(candidate_path, candidate_hash, "candidate result")
    write_json(args.output, result)
    print(f"{result['decision']}: {result['reason']}")
    return 0


def run_acceptance_gate(
    *,
    baseline_result: dict[str, Any],
    candidate_result: dict[str, Any],
    baseline_result_path: Path | None = None,
    candidate_result_path: Path | None = None,
    baseline_result_sha256: str | None = None,
    candidate_result_sha256: str | None = None,
    source_hashes_stable: bool = False,
    splits: tuple[str, ...] = DEFAULT_SPLITS,
    expected_repeats: int = DEFAULT_EXPECTED_REPEATS,
) -> dict[str, Any]:
    if expected_repeats < 1:
        raise ValueError("expected_repeats must be at least 1")
    comparisons = [
        compare_split(
            baseline_result=baseline_result,
            candidate_result=candidate_result,
            split=split,
            expected_repeats=expected_repeats,
        )
        for split in splits
    ]
    dropped = [item.split for item in comparisons if item.status == "dropped"]
    improved = [item.split for item in comparisons if item.status == "improved"]
    accepted = not dropped and bool(improved)
    reason = build_reason(accepted=accepted, improved=improved, dropped=dropped)
    return {
        "format": FORMAT,
        "accepted": accepted,
        "decision": "accepted" if accepted else "rejected",
        "reason": reason,
        "rule": {
            "splits": list(splits),
            "expected_repeats": expected_repeats,
            "average_metric": "pass_rate",
            "accept_if": "no split drops and at least one split improves",
        },
        "baseline_result": str(baseline_result_path) if baseline_result_path is not None else None,
        "candidate_result": str(candidate_result_path) if candidate_result_path is not None else None,
        "baseline_result_sha256": baseline_result_sha256
        if baseline_result_sha256 is not None
        else sha256_file(baseline_result_path)
        if baseline_result_path is not None
        else None,
        "candidate_result_sha256": candidate_result_sha256
        if candidate_result_sha256 is not None
        else sha256_file(candidate_result_path)
        if candidate_result_path is not None
        else None,
        "source_hashes_stable": bool(source_hashes_stable),
        "splits": {item.split: item.to_dict() for item in comparisons},
    }


def verify_acceptance_artifact(
    *,
    artifact: dict[str, Any],
    baseline_result_path: Path,
    candidate_result_path: Path,
    artifact_path: Path | None = None,
    artifact_sha256: str | None = None,
    splits: tuple[str, ...] = DEFAULT_SPLITS,
    expected_repeats: int = DEFAULT_EXPECTED_REPEATS,
) -> dict[str, Any]:
    """验证既有 acceptance 摘要与当前源结果的结构、绑定和重算结果一致。"""
    if artifact.get("format") != FORMAT:
        raise ValueError("strict acceptance artifact has unexpected format")
    accepted = artifact.get("accepted")
    decision = artifact.get("decision")
    if not isinstance(accepted, bool) or decision not in {"accepted", "rejected"}:
        raise ValueError("strict acceptance artifact has malformed decision fields")
    if accepted != (decision == "accepted"):
        raise ValueError("strict acceptance artifact accepted/decision mismatch")
    if artifact.get("source_hashes_stable") is not True:
        raise ValueError("strict acceptance artifact source hashes are not stable")
    rule = artifact.get("rule")
    if not isinstance(rule, dict):
        raise ValueError("strict acceptance artifact is missing rule")
    if rule.get("splits") != list(splits) or rule.get("expected_repeats") != expected_repeats:
        raise ValueError("strict acceptance artifact rule does not match Clean64 design")
    if rule.get("average_metric") != "pass_rate" or rule.get("accept_if") != "no split drops and at least one split improves":
        raise ValueError("strict acceptance artifact rule semantics are invalid")
    split_map = artifact.get("splits")
    if not isinstance(split_map, dict) or set(split_map) != set(splits):
        raise ValueError("strict acceptance artifact must contain train and heldout comparisons")
    for split, comparison in split_map.items():
        if not isinstance(comparison, dict) or comparison.get("status") not in {"improved", "unchanged", "dropped"}:
            raise ValueError(f"strict acceptance artifact has invalid {split} comparison")
        for role in ("baseline_repeats", "candidate_repeats"):
            repeats = comparison.get(role)
            if not isinstance(repeats, list) or len(repeats) != expected_repeats:
                raise ValueError(f"strict acceptance artifact {split} comparison must contain two {role}")
            if [item.get("repeat") for item in repeats if isinstance(item, dict)] != list(range(1, expected_repeats + 1)):
                raise ValueError(f"strict acceptance artifact {split} {role} repeat sequence is invalid")
        delta = comparison.get("delta")
        if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(float(delta)):
            raise ValueError(f"strict acceptance artifact {split} delta is invalid")

    expected_paths = {
        "baseline_result": baseline_result_path.resolve(),
        "candidate_result": candidate_result_path.resolve(),
    }
    for field, expected_path in expected_paths.items():
        actual_raw = artifact.get(field)
        try:
            actual_path = Path(str(actual_raw)).resolve()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"strict acceptance artifact has malformed {field} path") from exc
        if actual_path != expected_path:
            raise ValueError(f"strict acceptance artifact {field} path does not match analysis input")
        hash_field = f"{field}_sha256"
        if artifact.get(hash_field) != sha256_file(expected_path):
            raise ValueError(f"strict acceptance artifact {hash_field} does not match analysis input")

    baseline_payload, baseline_hash = read_json_with_stable_hash(expected_paths["baseline_result"])
    candidate_payload, candidate_hash = read_json_with_stable_hash(expected_paths["candidate_result"])
    recomputed = run_acceptance_gate(
        baseline_result=baseline_payload,
        candidate_result=candidate_payload,
        baseline_result_path=expected_paths["baseline_result"],
        candidate_result_path=expected_paths["candidate_result"],
        baseline_result_sha256=baseline_hash,
        candidate_result_sha256=candidate_hash,
        source_hashes_stable=True,
        splits=splits,
        expected_repeats=expected_repeats,
    )
    require_stable_hash(expected_paths["baseline_result"], baseline_hash, "baseline result")
    require_stable_hash(expected_paths["candidate_result"], candidate_hash, "candidate result")
    if artifact_path is not None:
        artifact_path = artifact_path.resolve()
        expected_artifact_hash = artifact_sha256 or sha256_file(artifact_path)
        require_stable_hash(artifact_path, expected_artifact_hash, "acceptance artifact")
    for field in ("accepted", "decision", "reason", "rule", "splits"):
        if artifact.get(field) != recomputed.get(field):
            raise ValueError(f"strict acceptance artifact {field} does not match recomputed gate")
    return artifact


def compare_split(
    *,
    baseline_result: dict[str, Any],
    candidate_result: dict[str, Any],
    split: str,
    expected_repeats: int,
) -> SplitComparison:
    baseline_repeats = split_repeat_metrics(baseline_result, split=split, expected_repeats=expected_repeats)
    candidate_repeats = split_repeat_metrics(candidate_result, split=split, expected_repeats=expected_repeats)
    assert_same_case_sets(
        split=split,
        baseline_result=baseline_result,
        candidate_result=candidate_result,
        expected_repeats=expected_repeats,
    )
    assert_same_denominators(split=split, baseline_repeats=baseline_repeats, candidate_repeats=candidate_repeats)
    baseline_average = average(item.pass_rate for item in baseline_repeats)
    candidate_average = average(item.pass_rate for item in candidate_repeats)
    delta = candidate_average - baseline_average
    if delta > 0:
        status = "improved"
    elif delta < 0:
        status = "dropped"
    else:
        status = "unchanged"
    return SplitComparison(
        split=split,
        baseline_repeats=baseline_repeats,
        candidate_repeats=candidate_repeats,
        baseline_average_pass_rate=baseline_average,
        candidate_average_pass_rate=candidate_average,
        delta=delta,
        status=status,
    )


def split_repeat_metrics(payload: dict[str, Any], *, split: str, expected_repeats: int) -> tuple[RepeatMetric, ...]:
    split_map = payload.get("splits")
    if not isinstance(split_map, dict):
        raise ValueError("eval result must contain a 'splits' object")
    raw_repeats = split_map.get(split)
    if not isinstance(raw_repeats, list):
        raise ValueError(f"eval result is missing split {split!r}")
    if len(raw_repeats) != expected_repeats:
        raise ValueError(f"split {split!r} must contain exactly {expected_repeats} repeats")
    metrics = tuple(
        repeat_metric(item, split=split, expected_repeats=expected_repeats)
        for item in raw_repeats
    )
    repeat_ids = [item.repeat for item in metrics]
    expected_ids = list(range(1, expected_repeats + 1))
    if sorted(repeat_ids) != expected_ids:
        raise ValueError(
            f"split {split!r} must contain repeat ids {expected_ids}, got {sorted(repeat_ids)}"
        )
    return tuple(sorted(metrics, key=lambda item: item.repeat))


def repeat_metric(raw: Any, *, split: str, expected_repeats: int = DEFAULT_EXPECTED_REPEATS) -> RepeatMetric:
    if not isinstance(raw, dict):
        raise ValueError(f"split {split!r} repeat entry must be an object")
    case_results = raw.get("case_results")
    if not isinstance(case_results, list):
        raise ValueError(f"split {split!r} repeat entry must contain case_results")
    raw_repeat = raw.get("repeat")
    if type(raw_repeat) is not int:
        raise ValueError(
            f"split {split!r} repeat entry must include an integer repeat; repeat ids must be JSON integers"
        )
    repeat = raw_repeat
    if repeat not in range(1, expected_repeats + 1):
        raise ValueError(
            f"split {split!r} must contain repeat ids {list(range(1, expected_repeats + 1))}; "
            f"repeat id {repeat} is outside the expected range"
        )
    if raw.get("split", split) != split:
        raise ValueError(f"split {split!r} repeat {repeat} has mismatched split metadata")
    case_ids = []
    for item in case_results:
        if not isinstance(item, dict) or not isinstance(item.get("case_id"), str) or not item["case_id"]:
            raise ValueError(f"split {split!r} repeat {repeat} has an invalid case_id")
        if item.get("split", split) != split:
            raise ValueError(f"split {split!r} repeat {repeat} has mismatched split metadata")
        item_repeat = item.get("repeat", repeat)
        if type(item_repeat) is not int:
            raise ValueError(f"split {split!r} repeat {repeat} has invalid case repeat metadata")
        if item_repeat != repeat:
            raise ValueError(f"split {split!r} repeat {repeat} has mismatched case metadata")
        case_ids.append(item["case_id"])
    if len(case_ids) != len(set(case_ids)):
        raise ValueError(f"split {split!r} repeat {repeat} contains duplicate case ids")
    invalid_cases = [
        f"{item.get('case_id') or 'unknown'} ({effective_invalid_reason(item, require_reward=True)})"
        for item in case_results
        if effective_invalid_reason(item, require_reward=True) is not None
    ]
    if invalid_cases:
        raise ValueError(
            f"split {split!r} contains invalid infrastructure trials: {', '.join(invalid_cases)}"
        )
    aggregate_error = repeat_aggregate_consistency_error(raw)
    if aggregate_error is not None:
        raise ValueError(f"split {split!r} repeat aggregate mismatch: {aggregate_error}")
    passed_raw, total_raw = raw.get("passed"), raw.get("total")
    if type(passed_raw) is not int or type(total_raw) is not int:
        raise ValueError(
            f"split {split!r} repeat entry must include integer passed/total"
        )
    passed = passed_raw
    total = total_raw
    if total <= 0:
        raise ValueError(f"split {split!r} repeat {repeat} has non-positive total")
    if passed < 0 or passed > total:
        raise ValueError(f"split {split!r} repeat {repeat} has invalid passed/total values")
    return RepeatMetric(repeat=repeat, passed=passed, total=total, pass_rate=passed / total)


def assert_same_case_sets(
    *,
    split: str,
    baseline_result: dict[str, Any],
    candidate_result: dict[str, Any],
    expected_repeats: int,
) -> None:
    def collect(payload: dict[str, Any]) -> dict[int, set[str]]:
        repeats = payload["splits"][split]
        return {
            int(repeat["repeat"]): {str(item["case_id"]) for item in repeat["case_results"]}
            for repeat in repeats
        }

    baseline = collect(baseline_result)
    candidate = collect(candidate_result)
    if set(baseline) != set(range(1, expected_repeats + 1)) or baseline != candidate:
        raise ValueError(f"split {split!r} baseline and candidate case sets are not identical")


def assert_same_denominators(
    *,
    split: str,
    baseline_repeats: tuple[RepeatMetric, ...],
    candidate_repeats: tuple[RepeatMetric, ...],
) -> None:
    baseline = [(item.repeat, item.total) for item in baseline_repeats]
    candidate = [(item.repeat, item.total) for item in candidate_repeats]
    if baseline != candidate:
        raise ValueError(f"split {split!r} baseline and candidate repeats are not comparable")


def average(values: Any) -> float:
    materialized = list(values)
    if not materialized:
        raise ValueError("cannot average an empty sequence")
    return sum(materialized) / len(materialized)


def build_reason(*, accepted: bool, improved: list[str], dropped: list[str]) -> str:
    if accepted:
        return f"accepted: improved {', '.join(improved)} with no split drops"
    if dropped:
        return f"rejected: dropped {', '.join(dropped)}"
    return "rejected: no split improved"


def read_json(path: Path) -> dict[str, Any]:
    return load_json_object(path)


def read_json_with_stable_hash(path: Path) -> tuple[dict[str, Any], str]:
    before = sha256_file(path)
    payload = load_json_object(path)
    after = sha256_file(path)
    if before != after:
        raise ValueError(f"{path} changed while acceptance input was being read")
    return payload, before


def require_stable_hash(path: Path, expected_sha256: str, label: str) -> None:
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ValueError(f"{label} changed during acceptance evaluation: {path}")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    # 先写同目录临时文件并原子替换，避免 gate 中断留下半个 strict artifact。
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
