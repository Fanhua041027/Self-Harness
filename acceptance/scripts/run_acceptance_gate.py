#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORMAT = "self_harness.acceptance_gate.v0"
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
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--split", action="append", help="Split to gate. Defaults to train and heldout.")
    parser.add_argument("--expected-repeats", type=int, default=DEFAULT_EXPECTED_REPEATS)
    args = parser.parse_args(argv)

    baseline_path = args.baseline_result.resolve()
    candidate_path = args.candidate_result.resolve()
    splits = tuple(args.split or DEFAULT_SPLITS)
    result = run_acceptance_gate(
        baseline_result=read_json(baseline_path),
        candidate_result=read_json(candidate_path),
        baseline_result_path=baseline_path,
        candidate_result_path=candidate_path,
        splits=splits,
        expected_repeats=args.expected_repeats,
    )
    write_json(args.output, result)
    print(f"{result['decision']}: {result['reason']}")
    return 0


def run_acceptance_gate(
    *,
    baseline_result: dict[str, Any],
    candidate_result: dict[str, Any],
    baseline_result_path: Path | None = None,
    candidate_result_path: Path | None = None,
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
        "splits": {item.split: item.to_dict() for item in comparisons},
    }


def compare_split(
    *,
    baseline_result: dict[str, Any],
    candidate_result: dict[str, Any],
    split: str,
    expected_repeats: int,
) -> SplitComparison:
    baseline_repeats = split_repeat_metrics(baseline_result, split=split, expected_repeats=expected_repeats)
    candidate_repeats = split_repeat_metrics(candidate_result, split=split, expected_repeats=expected_repeats)
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
    metrics = tuple(repeat_metric(item, split=split) for item in raw_repeats)
    repeat_ids = [item.repeat for item in metrics]
    if len(set(repeat_ids)) != len(repeat_ids):
        raise ValueError(f"split {split!r} contains duplicate repeat ids")
    return tuple(sorted(metrics, key=lambda item: item.repeat))


def repeat_metric(raw: Any, *, split: str) -> RepeatMetric:
    if not isinstance(raw, dict):
        raise ValueError(f"split {split!r} repeat entry must be an object")
    try:
        repeat = int(raw["repeat"])
        passed = int(raw["passed"])
        total = int(raw["total"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"split {split!r} repeat entry must include integer repeat/passed/total") from exc
    if total <= 0:
        raise ValueError(f"split {split!r} repeat {repeat} has non-positive total")
    if passed < 0 or passed > total:
        raise ValueError(f"split {split!r} repeat {repeat} has invalid passed/total values")
    return RepeatMetric(repeat=repeat, passed=passed, total=total, pass_rate=passed / total)


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
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
