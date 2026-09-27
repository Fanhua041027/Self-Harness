#!/usr/bin/env python3
"""在不读取任何 Sealed21 结果的前提下审计预注册统计设计的分辨率。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper.analyze_experiments import (
    BOOTSTRAP_PERCENTILE_METHOD,
    clustered_bootstrap_ci,
    task_level_permutation_p,
)

PREREGISTRATION_PATH = ROOT / "configs" / "experiments" / "ei_confirmation_v1.json"


def source_file_record(role: str, path: Path) -> dict[str, str]:
    resolved = path.resolve()
    return {
        "role": role,
        "path": str(resolved),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
    }


def canonical_pattern(tasks: int, favorable_tasks: int) -> list[tuple[str, bool, bool]]:
    """每个有利任务仅一个 repeat 从 fail→pass，另一 repeat 不变。"""
    if not 0 <= favorable_tasks <= tasks:
        raise ValueError("favorable_tasks must be between zero and tasks")
    pairs: list[tuple[str, bool, bool]] = []
    for index in range(tasks):
        case_id = f"task-{index + 1:02d}"
        pairs.append((case_id, False, index < favorable_tasks))
        pairs.append((case_id, False, False))
    return pairs


def build_audit(
    *,
    tasks: int = 21,
    bootstrap_samples: int = 20_000,
    source_files: list[dict[str, str]] | None = None,
    preregistration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prereg = preregistration or json.loads(PREREGISTRATION_PATH.read_text(encoding="utf-8"))
    endpoint = prereg.get("primary_endpoint", {})
    bootstrap = prereg.get("statistical_design", {}).get("bootstrap", {})
    alpha = endpoint.get("alpha")
    if not isinstance(alpha, (int, float)) or not isinstance(endpoint.get("success_rule"), str):
        raise ValueError("frozen preregistration primary endpoint is incomplete")
    if not isinstance(bootstrap.get("seed"), int):
        raise ValueError("frozen preregistration bootstrap seed is incomplete")
    rows = []
    for favorable_tasks in range(tasks + 1):
        pairs = canonical_pattern(tasks, favorable_tasks)
        delta = favorable_tasks / (2 * tasks)
        ci_low, ci_high = clustered_bootstrap_ci(
            pairs, seed=bootstrap["seed"], samples=bootstrap_samples
        )
        p_value = task_level_permutation_p(pairs)
        meets_success_rule = delta > 0 and ci_low > 0 and p_value < alpha
        rows.append(
            {
                "favorable_task_clusters": favorable_tasks,
                "attempt_level_delta": delta,
                "cluster_bootstrap_ci_low": ci_low,
                "cluster_bootstrap_ci_high": ci_high,
                "exact_task_permutation_p": p_value,
                "meets_success_rule": meets_success_rule,
            }
        )
    successful = [row for row in rows if row["meets_success_rule"]]
    first = successful[0] if successful else None
    audit = {
        "format": "self_harness.confirmation_design_audit.v1",
        "outcome_blind": True,
        "tasks": tasks,
        "repeats_per_task": 2,
        "bootstrap_samples": bootstrap_samples,
        "bootstrap_seed": bootstrap["seed"],
        "bootstrap_percentile_method": BOOTSTRAP_PERCENTILE_METHOD,
        "primary_endpoint_alpha": alpha,
        "primary_success_rule": endpoint["success_rule"],
        "canonical_pattern": (
            "k task clusters each contain one fail-to-pass repeat and one unchanged failed "
            "repeat; all other task clusters are unchanged failures"
        ),
        "smallest_exact_two_sided_p": 2 / (2**tasks),
        "minimum_canonical_success": first,
        "rows": rows,
    }
    if source_files is not None:
        audit["source_files"] = source_files
    return audit


def pct(value: float) -> str:
    return f"{100 * value:.2f}%"


def render_markdown(audit: dict[str, Any]) -> str:
    first = audit["minimum_canonical_success"]
    lines = [
        "# Sealed21 确认性统计设计审计",
        "",
        "本审计不读取任何 Sealed21 运行结果。它只评估预注册规则在一个透明的稀疏有利模式下的统计分辨率：每个有利任务仅有一次 repeat 从 fail→pass，另一次 repeat 不变，其余任务均不变。该模式不是功效假设，也不是预期结果。",
        "",
        f"- 任务数：{audit['tasks']}；每任务 repeats：{audit['repeats_per_task']}。",
        f"- 全部任务同方向时，双侧精确置换检验可达到的最小 p：{audit['smallest_exact_two_sided_p']:.8f}。",
    ]
    if first:
        lines.append(
            "- 在该规范模式下，首次同时满足 `delta > 0`、CI 下界大于 0、"
            f"`p < {audit['primary_endpoint_alpha']:g}` 需要 {first['favorable_task_clusters']} 个有利任务簇，"
            f"对应 attempt-level Δ={pct(first['attempt_level_delta'])}。"
        )
    lines.extend(
        [
            "",
            "| 有利任务簇 k | Attempt Δ | 95% cluster bootstrap CI | 精确置换 p | 满足规则 |",
            "|---:|---:|---:|---:|---|",
        ]
    )
    for row in audit["rows"]:
        lines.append(
            f"| {row['favorable_task_clusters']} | {pct(row['attempt_level_delta'])} "
            f"| [{pct(row['cluster_bootstrap_ci_low'])}, {pct(row['cluster_bootstrap_ci_high'])}] "
            f"| {row['exact_task_permutation_p']:.8f} "
            f"| {'是' if row['meets_success_rule'] else '否'} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("paper/generated/design-audit"))
    args = parser.parse_args(argv)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_files = [
        source_file_record("design_audit_script", Path(__file__)),
        source_file_record("statistics_script", ROOT / "paper" / "analyze_experiments.py"),
        source_file_record("validity_script", ROOT / "eval" / "scripts" / "result_validity.py"),
        source_file_record("preregistration", PREREGISTRATION_PATH),
    ]
    audit = build_audit(source_files=source_files)
    (output_dir / "confirmation_design_audit.json").write_bytes(
        (json.dumps(audit, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    (output_dir / "CONFIRMATION_DESIGN_AUDIT.md").write_bytes(
        render_markdown(audit).encode("utf-8")
    )
    print(output_dir / "CONFIRMATION_DESIGN_AUDIT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
