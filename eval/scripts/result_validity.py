"""统一判定历史与当前评测单元是否具有可用的 verifier 结果。"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def effective_invalid_reason(
    case: dict[str, Any], *, require_reward: bool = False
) -> str | None:
    """返回基础设施无效原因；``None`` 表示可作为行为通过/失败使用。

    早期 Clean64 运行会把 API、余额、超时或 wrapper 异常写成
    ``status=failed``。这些单元没有 verifier reward，不能作为模型行为失败。
    新运行器已使用 ``status=invalid``，这里同时兼容历史产物。
    """
    infrastructure_error = case.get("infrastructure_error")
    if infrastructure_error:
        return str(infrastructure_error)

    trace_metadata = case.get("trace_metadata")
    trace_failure = trace_metadata.get("failure") if isinstance(trace_metadata, dict) else None
    if trace_failure:
        return f"runtime failure: {trace_failure}"

    status = str(case.get("status") or "")
    if status == "invalid":
        return str(case.get("failure_message") or "explicit invalid status")

    passed_value = case.get("passed")
    if not isinstance(passed_value, bool):
        return f"non-boolean passed: {passed_value!r}"
    passed = passed_value
    if require_reward and "reward" not in case:
        return "missing verifier reward"
    if "reward" in case:
        reward = case.get("reward")
        if reward is None:
            return "missing verifier reward"
        if isinstance(reward, bool) or not isinstance(reward, (int, float)):
            return f"non-numeric verifier reward: {reward!r}"
        try:
            finite_reward = math.isfinite(reward)
        except (OverflowError, TypeError):
            finite_reward = False
        if not finite_reward:
            return f"non-finite verifier reward: {reward!r}"
        reward_passed = reward > 0
        if reward_passed != passed:
            return f"inconsistent passed={passed} and reward={reward!r}"

    if passed and status not in {"", "passed"}:
        return f"inconsistent passed=True and status={status!r}"
    if not passed and status == "passed":
        return "inconsistent passed=False and status='passed'"
    return None


def is_effectively_invalid(case: dict[str, Any], *, require_reward: bool = False) -> bool:
    return effective_invalid_reason(case, require_reward=require_reward) is not None


def reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def load_json_object(path: Path) -> dict[str, Any]:
    """读取结果/验收 JSON，并统一处理 BOM、重复键和根对象类型。"""
    payload = json.loads(
        path.read_text(encoding="utf-8-sig"),
        object_pairs_hook=reject_duplicate_json_keys,
    )
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def repeat_aggregate_consistency_error(repeat: dict[str, Any]) -> str | None:
    """返回 repeat 汇总与逐 case 结果不一致的原因。"""
    cases = repeat.get("case_results")
    if not isinstance(cases, list):
        return "missing case_results"
    passed = repeat.get("passed")
    total = repeat.get("total")
    if isinstance(passed, bool) or not isinstance(passed, int):
        return f"aggregate passed is not an integer: {passed!r}"
    if isinstance(total, bool) or not isinstance(total, int):
        return f"aggregate total is not an integer: {total!r}"
    if total != len(cases):
        return f"aggregate total={total} does not match case count={len(cases)}"
    passed_values = [case.get("passed") for case in cases if isinstance(case, dict)]
    if len(passed_values) != len(cases) or any(not isinstance(value, bool) for value in passed_values):
        return "case passed values are not all JSON booleans"
    calculated = sum(passed_values)
    if passed != calculated:
        return f"aggregate passed={passed} does not match case sum={calculated}"
    return None


def invalid_reason_category(reason: str) -> str:
    normalized = reason.lower()
    if "timeout" in normalized or "timed out" in normalized:
        return "timeout"
    if any(
        marker in normalized
        for marker in (
            "api",
            "connection error",
            "arrearage",
            "balance",
            "rate limit",
            "authentication",
            "getaddrinfo",
        )
    ):
        return "provider"
    if "no verifier reward" in normalized or "missing verifier reward" in normalized:
        return "missing_reward"
    if "did not produce a trial" in normalized:
        return "missing_trial"
    if "failed to fetch" in normalized or "unable to locate package" in normalized:
        return "dependency"
    if "docker" in normalized:
        return "docker"
    if (
        "inconsistent" in normalized
        or "non-numeric" in normalized
        or "non-boolean" in normalized
        or "non-finite" in normalized
    ):
        return "inconsistent_result"
    return "other"


def effective_invalid_cases(
    result: dict[str, Any], *, require_reward: bool = False
) -> list[dict[str, Any]]:
    invalid = []
    for split, repeats in result.get("splits", {}).items():
        for repeat in repeats:
            for case in repeat.get("case_results", []):
                reason = effective_invalid_reason(case, require_reward=require_reward)
                if reason is not None:
                    invalid.append(
                        {
                            "split": str(split),
                            "repeat": int(case.get("repeat", repeat.get("repeat", 0))),
                            "case_id": str(case.get("case_id") or "unknown"),
                            "reason": reason,
                            "reason_category": invalid_reason_category(reason),
                        }
                    )
    return invalid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--count", action="store_true")
    parser.add_argument(
        "--require-reward",
        action="store_true",
        help="strict mode: classify a case without a reward field as invalid",
    )
    parser.add_argument(
        "--emit-object",
        action="store_true",
        help="emit the validated JSON object after duplicate-key/BOM checks",
    )
    args = parser.parse_args(argv)
    payload = load_json_object(args.result)
    if args.emit_object:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    invalid = effective_invalid_cases(payload, require_reward=args.require_reward)
    if args.count:
        print(len(invalid))
    else:
        print(json.dumps(invalid, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
