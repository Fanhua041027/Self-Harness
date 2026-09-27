#!/usr/bin/env python3
"""计划或补跑评测结果中的基础设施无效单元。

默认仅输出计划，不修改结果、不调用模型。只有显式传入 ``--execute`` 才会归档
旧 checkpoint、清除待补跑的 JSON checkpoint，并以 ``--reuse-existing`` 运行评测器。
已有 Harbor job 目录始终保留，新的运行使用唯一的 resume job 名称。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from result_validity import effective_invalid_reason, invalid_reason_category, load_json_object


PLAN_FORMAT = "self_harness.invalid_rerun_plan.v1"
CELL_MANIFEST_FORMAT = "self_harness.rerun_cell_manifest.v3"
CANDIDATE_ENV = "SELF_HARNESS_CANDIDATE_WORKSPACE"


def read_json(path: Path) -> dict[str, Any]:
    return load_json_object(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json_with_stable_hash(path: Path) -> tuple[dict[str, Any], str]:
    before = sha256_file(path)
    payload = read_json(path)
    after = sha256_file(path)
    if before != after:
        raise ValueError(f"source result changed while rerun plan was being built: {path}")
    return payload, before


def case_checkpoint_path(repeat_dir: Path, case_id: str) -> Path:
    """Resolve the collision-resistant path, with legacy fallback for old runs."""
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:12]
    slug = "".join(char if char.isalnum() or char in "_.-" else "_" for char in case_id).strip("_")[:160] or "case"
    current = repeat_dir / "cases" / f"{slug}--{digest}" / "result.json"
    if current.is_file():
        return current
    return repeat_dir / "cases" / case_id / "result.json"


    for relative in (Path("repo_baseline.py"), Path("current") / "repo_baseline.py"):
        path = candidate_workspace / relative
        if path.is_file():
            return path.resolve()
    raise RuntimeError(
        f"candidate workspace has no repo_baseline.py or current/repo_baseline.py: {candidate_workspace}"
    )


def require_stable_source_hash(path: Path, expected_sha256: str, label: str = "source result") -> None:
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ValueError(f"{label} changed before rerun execution: {path}")


def find_invalids(result: dict[str, Any]) -> list[dict[str, Any]]:
    invalids = []
    for split, split_results in result.get("splits", {}).items():
        for split_run in split_results:
            repeat = int(split_run["repeat"])
            for case in split_run.get("case_results", []):
                invalid_reason = effective_invalid_reason(case, require_reward=True)
                if invalid_reason is None:
                    continue
                invalids.append(
                    {
                        "split": str(split),
                        "repeat": repeat,
                        "case_id": str(case["case_id"]),
                        "recorded_status": case.get("status"),
                        "infrastructure_error": invalid_reason,
                        "reason_category": invalid_reason_category(invalid_reason),
                        "failure_message": case.get("failure_message"),
                        "attempt_count": int(case.get("attempt_count", 1)),
                    }
                )
    return sorted(invalids, key=lambda item: (item["split"], item["repeat"], item["case_id"]))


def cell_key(item: dict[str, Any]) -> tuple[str, int, str]:
    return str(item["split"]), int(item["repeat"]), str(item["case_id"])


def load_cell_filter(
    path: Path, *, expected_side: str | None = None
) -> set[tuple[str, int, str]]:
    payload = read_json(path)
    if payload.get("format") != CELL_MANIFEST_FORMAT:
        raise ValueError(f"unexpected rerun cell manifest format: {payload.get('format')!r}")
    if payload.get("source_hashes_stable") is not True:
        raise ValueError("rerun cell manifest lacks stable source-hash provenance")
    manifest_side = payload.get("side")
    if manifest_side not in {"baseline", "candidate"}:
        raise ValueError(f"rerun cell manifest has invalid side: {manifest_side!r}")
    if expected_side is not None and manifest_side != expected_side:
        raise ValueError(
            f"rerun cell manifest side mismatch: expected {expected_side!r}, "
            f"got {manifest_side!r}"
        )
    for role in ("baseline", "candidate"):
        source_path = Path(str(payload.get(f"{role}_result", ""))).expanduser().resolve()
        expected_sha256 = payload.get(f"{role}_result_sha256")
        if not source_path.is_file() or not isinstance(expected_sha256, str):
            raise ValueError(f"stale cell manifest: missing {role} source result identity")
        if sha256_file(source_path) != expected_sha256:
            raise ValueError(f"stale cell manifest: {role} source result hash mismatch")
    cells = payload.get("cells")
    if not isinstance(cells, list) or not cells:
        raise ValueError("rerun cell manifest must contain a non-empty cells list")
    keys = {cell_key(item) for item in cells if isinstance(item, dict)}
    if len(keys) != len(cells):
        raise ValueError("rerun cell manifest contains duplicate or invalid cells")
    return keys


def load_rerun_limits(config_path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
        import tomli as tomllib  # type: ignore[no-redef]
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    eval_config = raw.get("eval", {})
    timeout_s = float(eval_config.get("timeout_s", 0))
    retries = int(eval_config.get("infrastructure_retries", 0))
    concurrency = int(eval_config.get("case_concurrency", 1))
    return {
        "timeout_s": timeout_s,
        "infrastructure_retries": retries,
        "case_concurrency": concurrency,
    }


def build_plan(
    *,
    output_dir: Path,
    config_path: Path,
    candidate_workspace: Path | None,
    selected_cells: set[tuple[str, int, str]] | None = None,
) -> dict[str, Any]:
    result_path = output_dir / "result.json"
    if not result_path.is_file():
        raise FileNotFoundError(f"aggregate result not found: {result_path}")
    result, source_hash = read_json_with_stable_hash(result_path)
    all_invalids = find_invalids(result)
    invalid_by_key = {cell_key(item): item for item in all_invalids}
    if selected_cells is not None:
        unavailable = sorted(selected_cells - set(invalid_by_key))
        if unavailable:
            raise ValueError(
                "selected rerun cells are not currently invalid; rebuild the phased plan: "
                f"{unavailable}"
            )
        invalids = [invalid_by_key[key] for key in sorted(selected_cells)]
    else:
        invalids = all_invalids
    require_stable_source_hash(result_path, source_hash)
    limits = load_rerun_limits(config_path)
    max_attempts = len(invalids) * (limits["infrastructure_retries"] + 1)
    concurrency = max(1, limits["case_concurrency"])
    worst_case_hours = max_attempts * limits["timeout_s"] / concurrency / 3600
    return {
        "format": PLAN_FORMAT,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(output_dir),
        "source_result": str(result_path),
        "source_result_sha256": source_hash,
        "source_result_stable": True,
        "model": result.get("model"),
        "config": str(config_path),
        "candidate_workspace": str(candidate_workspace) if candidate_workspace else None,
        "selection": "cell_manifest" if selected_cells is not None else "all_effective_invalid",
        "selection_side": (
            "candidate" if candidate_workspace is not None else "baseline"
        )
        if selected_cells is not None
        else None,
        "global_invalid_count_at_plan_time": len(all_invalids),
        "invalid_count": len(invalids),
        "reason_counts": {
            category: sum(item["reason_category"] == category for item in invalids)
            for category in sorted({item["reason_category"] for item in invalids})
        },
        "affected_repeats": sorted(
            {f"{item['split']}/repeat-{item['repeat']:02d}" for item in invalids}
        ),
        "max_model_attempts": max_attempts,
        "worst_case_wall_hours_at_configured_concurrency": round(worst_case_hours, 2),
        "limits": limits,
        "cases": invalids,
    }


def print_plan(plan: dict[str, Any]) -> None:
    print(
        f"model={plan['model']} invalid={plan['invalid_count']} "
        f"max_attempts={plan['max_model_attempts']} "
        f"worst_case_hours={plan['worst_case_wall_hours_at_configured_concurrency']}"
    )
    for item in plan["cases"]:
        print(
            f"  {item['split']} repeat-{item['repeat']:02d} {item['case_id']} "
            f"attempts={item['attempt_count']} error={item['infrastructure_error'] or '-'}"
        )


def validate_execution_environment(plan: dict[str, Any], candidate_workspace: Path | None) -> None:
    configured_model = os.environ.get("SELF_HARNESS_MODEL")
    if not configured_model:
        raise RuntimeError("SELF_HARNESS_MODEL must be set for --execute")
    if configured_model != plan["model"]:
        raise RuntimeError(
            f"model mismatch: result uses {plan['model']!r}, environment uses {configured_model!r}"
        )
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY must be set for --execute")
    if candidate_workspace is None:
        return
    candidate_harness_path(candidate_workspace)


def create_archive_dir(output_dir: Path) -> Path:
    """Create a collision-free archive directory for one execution."""
    base = output_dir / "rerun_history" / time.strftime("%Y%m%d-%H%M%S")
    archive_dir = base
    suffix = 1
    while True:
        try:
            archive_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            archive_dir = base.with_name(f"{base.name}-{suffix:02d}")
            suffix += 1
            continue
        return archive_dir


def archive_and_clear_checkpoints(
    *, output_dir: Path, plan: dict[str, Any], archive_dir: Path
) -> None:
    """只归档并清除 JSON checkpoint；Harbor 原始目录保持不变。"""
    paths = {output_dir / "result.json"}
    for item in plan["cases"]:
        repeat_dir = output_dir / "splits" / item["split"] / f"repeat-{item['repeat']:02d}"
        paths.add(repeat_dir / "result.json")
        paths.add(case_checkpoint_path(repeat_dir, item["case_id"]))
    for path in sorted(paths, key=str):
        if not path.is_file():
            continue
        relative = path.relative_to(output_dir)
        destination = archive_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        path.unlink()


def count_invalids(result_path: Path) -> int:
    return len(find_invalids(read_json(result_path)))


def count_invalids_from_result(result: dict[str, Any]) -> int:
    return len(find_invalids(result))


def count_selected_invalids(
    result_path: Path, selected_cells: set[tuple[str, int, str]]
) -> int:
    return sum(
        cell_key(item) in selected_cells
        for item in find_invalids(read_json(result_path))
    )


def count_selected_invalids_from_result(
    result: dict[str, Any], selected_cells: set[tuple[str, int, str]]
) -> int:
    return sum(cell_key(item) in selected_cells for item in find_invalids(result))


def evaluate_canary_outcomes(
    result: dict[str, Any], selected_cells: set[tuple[str, int, str]]
) -> dict[str, Any]:
    """按严格 verifier 有效性判定 Phase 0，不把行为 pass/fail 当作健康信号。"""
    case_by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
    for split, repeats in result.get("splits", {}).items():
        if not isinstance(repeats, list):
            continue
        for repeat_record in repeats:
            if not isinstance(repeat_record, dict):
                continue
            try:
                repeat = int(repeat_record.get("repeat"))
            except (TypeError, ValueError):
                continue
            for case in repeat_record.get("case_results", []):
                if not isinstance(case, dict):
                    continue
                case_id = case.get("case_id")
                if isinstance(case_id, str):
                    case_by_key[(str(split), repeat, case_id)] = case
    numeric = 0
    invalid = 0
    missing = 0
    behavioral_pass = 0
    behavioral_fail = 0
    details: list[dict[str, Any]] = []
    for key in sorted(selected_cells):
        case = case_by_key.get(key)
        if case is None:
            missing += 1
            details.append({"split": key[0], "repeat": key[1], "case_id": key[2], "status": "missing"})
            continue
        reason = effective_invalid_reason(case, require_reward=True)
        if reason is None:
            numeric += 1
            if case.get("passed") is True:
                behavioral_pass += 1
            elif case.get("passed") is False:
                behavioral_fail += 1
            details.append({"split": key[0], "repeat": key[1], "case_id": key[2], "status": "numeric_verifier_outcome"})
        else:
            invalid += 1
            details.append({"split": key[0], "repeat": key[1], "case_id": key[2], "status": "infrastructure_invalid", "reason": reason})
    return {
        "selected_cell_count": len(selected_cells),
        "numeric_verifier_outcome_count": numeric,
        "infrastructure_invalid_count": invalid,
        "missing_cell_count": missing,
        "behavioral_pass_count": behavioral_pass,
        "behavioral_fail_count": behavioral_fail,
        "canary_ready": bool(selected_cells) and numeric == len(selected_cells),
        "details": details,
    }


def build_execution_provenance(
    *,
    plan: dict[str, Any],
    result_path: Path,
    evaluator_returncode: int,
    selected_cells: set[tuple[str, int, str]] | None,
    selection_phase: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """绑定 rerun 执行前后的 aggregate result 身份，并确认执行后读取稳定。"""
    result, post_hash = read_json_with_stable_hash(result_path)
    global_remaining = count_invalids_from_result(result)
    selected_remaining = (
        count_selected_invalids_from_result(result, selected_cells)
        if selected_cells is not None
        else global_remaining
    )
    provenance = {
        "status": "completed" if evaluator_returncode == 0 else "evaluator_failed",
        "evaluator_returncode": evaluator_returncode,
        "selection_phase": selection_phase,
        "pre_source_result_sha256": plan.get("source_result_sha256"),
        "post_source_result_sha256": post_hash,
        "post_source_result_stable": True,
        "selected_invalid_after": selected_remaining,
        "global_invalid_after": global_remaining,
    }
    return provenance, result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--candidate-workspace", type=Path)
    parser.add_argument(
        "--cells-file",
        type=Path,
        help="只补跑 manifest 中精确列出的当前 invalid cells；用于分阶段基础设施恢复",
    )
    parser.add_argument("--plan-out", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="归档旧 checkpoint 并执行付费补跑；省略时仅生成计划",
    )
    args = parser.parse_args(argv)

    output_dir = args.output_dir.expanduser().resolve()
    config_path = args.config.expanduser().resolve()
    candidate_workspace = (
        args.candidate_workspace.expanduser().resolve() if args.candidate_workspace else None
    )
    selected_cells = (
        load_cell_filter(
            args.cells_file.expanduser().resolve(),
            expected_side="candidate" if candidate_workspace is not None else "baseline",
        )
        if args.cells_file
        else None
    )
    selection_phase = None
    if args.cells_file:
        manifest_payload = read_json(args.cells_file.expanduser().resolve())
        selection_phase = manifest_payload.get("phase")
        if selection_phase not in {
            "phase_0_infrastructure_canary",
            "phase_1_one_sided_bridge",
            "phase_2_both_sides_missing",
        }:
            raise ValueError(f"rerun cell manifest has invalid phase: {selection_phase!r}")
    plan = build_plan(
        output_dir=output_dir,
        config_path=config_path,
        candidate_workspace=candidate_workspace,
        selected_cells=selected_cells,
    )
    if selected_cells is not None:
        plan["selection_side"] = (
            "candidate" if candidate_workspace is not None else "baseline"
        )
        plan["selection_phase"] = selection_phase
    plan_out = (
        args.plan_out.expanduser().resolve()
        if args.plan_out
        else output_dir / "invalid-rerun-plan.json"
    )
    plan_out.parent.mkdir(parents=True, exist_ok=True)
    plan_out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_plan(plan)
    print(f"plan: {plan_out}")

    if not plan["invalid_count"]:
        print("no invalid cases; nothing to do")
        return 0
    if not args.execute:
        print("dry-run only; pass --execute to archive checkpoints and start model calls")
        return 0

    if plan.get("source_result_stable") is not True:
        raise ValueError("rerun plan lacks stable source-result provenance")
    require_stable_source_hash(output_dir / "result.json", plan["source_result_sha256"])
    validate_execution_environment(plan, candidate_workspace)
    archive_dir = create_archive_dir(output_dir)
    archive_and_clear_checkpoints(
        output_dir=output_dir,
        plan=plan,
        archive_dir=archive_dir,
    )
    shutil.copy2(plan_out, archive_dir / "invalid-rerun-plan.json")
    print(f"archived JSON checkpoints: {archive_dir}")

    runner = Path(__file__).resolve().parent / "run_harbor_eval.py"
    command = [
        args.python,
        str(runner),
        "--config",
        str(config_path),
        "--output-dir",
        str(output_dir),
        "--reuse-existing",
    ]
    env = os.environ.copy()
    if candidate_workspace is not None:
        env[CANDIDATE_ENV] = str(candidate_workspace)
    else:
        env.pop(CANDIDATE_ENV, None)
    print("running:", subprocess.list2cmdline(command))
    completed = subprocess.run(command, env=env, check=False)
    result_path = output_dir / "result.json"
    if completed.returncode != 0:
        plan["execution_provenance"] = {
            "status": "evaluator_failed",
            "evaluator_returncode": completed.returncode,
            "selection_phase": selection_phase,
            "pre_source_result_sha256": plan.get("source_result_sha256"),
            "post_source_result_sha256": None,
            "post_source_result_stable": False,
        }
        plan_out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.copy2(plan_out, archive_dir / "invalid-rerun-plan.json")
        print(f"rerun failed with exit code {completed.returncode}")
        return completed.returncode

    execution_provenance, result = build_execution_provenance(
        plan=plan,
        result_path=result_path,
        evaluator_returncode=completed.returncode,
        selected_cells=selected_cells,
        selection_phase=selection_phase,
    )
    plan["execution_provenance"] = execution_provenance
    global_remaining = execution_provenance["global_invalid_after"]
    selected_remaining = execution_provenance["selected_invalid_after"]
    print(
        f"after rerun: total={result.get('total')} passed={result.get('passed')} "
        f"selected_invalid={selected_remaining} global_invalid={global_remaining}"
    )
    if selection_phase == "phase_0_infrastructure_canary":
        canary_outcome = evaluate_canary_outcomes(result, selected_cells or set())
        plan["canary_outcome"] = canary_outcome
        print(
            "canary_ready: "
            f"{canary_outcome['canary_ready']} "
            f"numeric_verifier_outcomes={canary_outcome['numeric_verifier_outcome_count']}/"
            f"{canary_outcome['selected_cell_count']}"
        )
    plan_out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(plan_out, archive_dir / "invalid-rerun-plan.json")
    if selected_remaining:
        print("selected phase remains incomplete")
        return 2
    if global_remaining:
        print("selected phase complete; strict acceptance remains blocked by later phases")
    else:
        print("strict acceptance input is complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
