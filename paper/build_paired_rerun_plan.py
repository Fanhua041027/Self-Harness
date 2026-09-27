#!/usr/bin/env python3
"""生成 baseline/candidate 的配对完成度与分阶段补跑预算。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
VALIDITY_DIR = ROOT / "eval" / "scripts"
if str(VALIDITY_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDITY_DIR))

from result_validity import effective_invalid_reason, invalid_reason_category

from paper.analyze_experiments import (
    flatten_result,
    require_stable_analysis_inputs,
    stable_hash_snapshot,
)


CELL_MANIFEST_FORMAT = "self_harness.rerun_cell_manifest.v3"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cell(key: tuple[str, int, str], side: str, reason: str) -> dict[str, Any]:
    return {
        "side": side,
        "split": key[0],
        "repeat": key[1],
        "case_id": key[2],
        "reason": reason,
        "reason_category": invalid_reason_category(reason),
    }


def budget(cells: list[dict[str, Any]], *, timeout_s: float, retries: int, concurrency: int) -> dict[str, Any]:
    attempts = len(cells) * (retries + 1)
    return {
        "cells": len(cells),
        "max_model_attempts": attempts,
        "configured_wall_hours": round(attempts * timeout_s / max(1, concurrency) / 3600, 2),
    }


def build(
    *, baseline_path: Path, candidate_path: Path, timeout_s: float, retries: int, concurrency: int
) -> dict[str, Any]:
    baseline_path = baseline_path.resolve()
    candidate_path = candidate_path.resolve()
    source_paths = (baseline_path, candidate_path)
    source_hashes_before = stable_hash_snapshot(source_paths)
    baseline = flatten_result(baseline_path)
    candidate = flatten_result(candidate_path)
    if baseline.keys() != candidate.keys():
        raise ValueError("baseline and candidate result keys do not match")
    pair_counts: Counter[str] = Counter()
    bridge: list[dict[str, Any]] = []
    both: list[dict[str, Any]] = []
    for key in sorted(baseline):
        before_reason = effective_invalid_reason(baseline[key], require_reward=True)
        after_reason = effective_invalid_reason(candidate[key], require_reward=True)
        if before_reason is None and after_reason is None:
            pair_counts["both_valid"] += 1
        elif before_reason is not None and after_reason is None:
            pair_counts["baseline_invalid_only"] += 1
            bridge.append(cell(key, "baseline", before_reason))
        elif before_reason is None and after_reason is not None:
            pair_counts["candidate_invalid_only"] += 1
            bridge.append(cell(key, "candidate", after_reason))
        else:
            pair_counts["both_invalid"] += 1
            both.append(cell(key, "baseline", before_reason or "unknown"))
            both.append(cell(key, "candidate", after_reason or "unknown"))
    all_cells = bridge + both
    canaries = []
    for side in ("baseline", "candidate"):
        choices = sorted(
            (item for item in all_cells if item["side"] == side),
            key=lambda item: (item["reason_category"] != "provider", item["split"], item["repeat"], item["case_id"]),
        )
        if choices:
            canaries.append(choices[0])
    phases = {
        "phase_0_infrastructure_canary": {
            "purpose": "verify provider, wrapper, Harbor, and verifier health; do not inspect behavioral outcomes",
            "overlaps_later_phases": True,
            "budget": budget(canaries, timeout_s=timeout_s, retries=retries, concurrency=concurrency),
            "cells": canaries,
        },
        "phase_1_one_sided_bridge": {
            "purpose": "complete pairs for which the opposite harness already has a valid result",
            "budget": budget(bridge, timeout_s=timeout_s, retries=retries, concurrency=concurrency),
            "cells": bridge,
        },
        "phase_2_both_sides_missing": {
            "purpose": "complete both harness sides for pairs with no valid result",
            "budget": budget(both, timeout_s=timeout_s, retries=retries, concurrency=concurrency),
            "cells": both,
        },
    }
    source_hashes_after = stable_hash_snapshot(source_paths)
    require_stable_analysis_inputs(source_hashes_before, source_hashes_after)
    return {
        "format": "self_harness.paired_rerun_plan.v1",
        "baseline_result": str(baseline_path),
        "baseline_result_sha256": source_hashes_before[str(baseline_path)],
        "candidate_result": str(candidate_path),
        "candidate_result_sha256": source_hashes_before[str(candidate_path)],
        "source_hashes_stable": True,
        "pair_counts": dict(pair_counts),
        "incomplete_pairs": len(baseline) - pair_counts["both_valid"],
        "rerun_cells": len(all_cells),
        "reason_counts": dict(sorted(Counter(item["reason_category"] for item in all_cells).items())),
        "limits": {"timeout_s": timeout_s, "infrastructure_retries": retries, "case_concurrency": concurrency},
        "total_budget": budget(all_cells, timeout_s=timeout_s, retries=retries, concurrency=concurrency),
        "phases": phases,
    }


def render_markdown(plan: dict[str, Any]) -> str:
    pairs = plan["pair_counts"]
    phase0 = plan["phases"]["phase_0_infrastructure_canary"]["budget"]
    phase1 = plan["phases"]["phase_1_one_sided_bridge"]["budget"]
    phase2 = plan["phases"]["phase_2_both_sides_missing"]["budget"]
    reasons = "、".join(f"{key}={value}" for key, value in plan["reason_counts"].items())
    return "\n".join(
        [
            "# Qwen 严格 promotion 配对补跑计划",
            "",
            f"- baseline result SHA256：`{plan['baseline_result_sha256']}`",
            f"- candidate result SHA256：`{plan['candidate_result_sha256']}`",
            "",
            "## 当前配对完成度",
            "",
            f"- 双方有效：{pairs.get('both_valid', 0)} / 128",
            f"- 仅 baseline 无效：{pairs.get('baseline_invalid_only', 0)}",
            f"- 仅 candidate 无效：{pairs.get('candidate_invalid_only', 0)}",
            f"- 双方均无效：{pairs.get('both_invalid', 0)}",
            f"- 待补跑 cell：{plan['rerun_cells']}",
            f"- 原因分布：{reasons}",
            "",
            "## 分阶段执行",
            "",
            "| 阶段 | 目的 | Cells | 最多模型尝试 | 配置上界 |",
            "|---|---|---:|---:|---:|",
            f"| Phase 0 | 每侧一个固定基础设施 canary；只检查是否得到数值 verifier 结果 | {phase0['cells']} | {phase0['max_model_attempts']} | {phase0['configured_wall_hours']:.2f} h |",
            f"| Phase 1 | 补齐单侧缺失配对 | {phase1['cells']} | {phase1['max_model_attempts']} | {phase1['configured_wall_hours']:.2f} h |",
            f"| Phase 2 | 补齐双方均缺失配对 | {phase2['cells']} | {phase2['max_model_attempts']} | {phase2['configured_wall_hours']:.2f} h |",
            "",
            "Phase 0 与后续阶段重叠，成功后必须重新生成计划以自动排除已完成 cell。任何阶段均不得依据 pass/fail 决定是否继续；只能依据是否恢复稳定的基础设施结果决定暂停或继续。确认性分析仍要求全部 183 个待补跑 cell 完成且最终为零 invalid。",
            "",
        ]
    )


def write_csv(path: Path, plan: dict[str, Any]) -> None:
    rows = []
    for phase_name, phase in plan["phases"].items():
        for item in phase["cells"]:
            rows.append({"phase": phase_name, **item})
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_cell_manifests(output_dir: Path, plan: dict[str, Any]) -> None:
    for phase_name, phase in plan["phases"].items():
        for side in ("baseline", "candidate"):
            cells = [
                {key: item[key] for key in ("split", "repeat", "case_id")}
                for item in phase["cells"]
                if item["side"] == side
            ]
            if not cells:
                continue
            payload = {
                "format": CELL_MANIFEST_FORMAT,
                "phase": phase_name,
                "side": side,
                "baseline_result": plan["baseline_result"],
                "baseline_result_sha256": plan["baseline_result_sha256"],
                "candidate_result": plan["candidate_result"],
                "candidate_result_sha256": plan["candidate_result_sha256"],
                "source_hashes_stable": plan["source_hashes_stable"],
                "cells": cells,
            }
            (output_dir / f"{phase_name}.{side}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-result", required=True, type=Path)
    parser.add_argument("--candidate-result", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout-s", type=float, default=3900)
    parser.add_argument("--infrastructure-retries", type=int, default=2)
    parser.add_argument("--case-concurrency", type=int, default=4)
    args = parser.parse_args(argv)
    plan = build(
        baseline_path=args.baseline_result,
        candidate_path=args.candidate_result,
        timeout_s=args.timeout_s,
        retries=args.infrastructure_retries,
        concurrency=args.case_concurrency,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "paired_rerun_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "PAIRED_RERUN_PLAN.md").write_text(render_markdown(plan), encoding="utf-8")
    write_csv(args.output_dir / "paired_rerun_cells.csv", plan)
    write_cell_manifests(args.output_dir, plan)
    print(args.output_dir / "PAIRED_RERUN_PLAN.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
