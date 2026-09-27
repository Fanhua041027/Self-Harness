#!/usr/bin/env python3
"""结果盲审计 Clean64、Sealed21 与能力边界排除任务的类别和难度覆盖。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper.analyze_experiments import load_json


def import_toml_reader() -> Any:
    try:
        import tomllib

        return tomllib
    except ModuleNotFoundError:
        import tomli

        return tomli


def clean_splits(config_path: Path) -> dict[str, set[str]]:
    raw = import_toml_reader().loads(config_path.read_text(encoding="utf-8"))
    grouped: dict[str, set[str]] = {}
    for case in raw.get("cases", []):
        grouped.setdefault(str(case["split"]), set()).add(str(case["case_id"]))
    return grouped


def task_metadata(task_dir: Path) -> dict[str, str]:
    raw = import_toml_reader().loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    metadata = raw.get("metadata", {})
    return {
        "category": str(metadata.get("category") or "unknown"),
        "difficulty": str(metadata.get("difficulty") or "unknown"),
    }


def source_file_record(role: str, path: Path) -> dict[str, str]:
    resolved = path.resolve()
    return {
        "role": role,
        "path": str(resolved),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
    }


def proportions(counts: Counter[str]) -> dict[str, float]:
    total = sum(counts.values())
    return {label: count / total for label, count in counts.items()} if total else {}


def distribution_distance(
    reference: Counter[str], comparison: Counter[str]
) -> dict[str, float]:
    p = proportions(reference)
    q = proportions(comparison)
    # 固定求和顺序，避免 Python hash 随进程变化导致浮点末位和 JSON 字节漂移。
    labels = sorted(set(p) | set(q))
    total_variation = 0.5 * sum(abs(p.get(label, 0.0) - q.get(label, 0.0)) for label in labels)
    js = 0.0
    for label in labels:
        left = p.get(label, 0.0)
        right = q.get(label, 0.0)
        midpoint = (left + right) / 2
        if left:
            js += 0.5 * left * math.log2(left / midpoint)
        if right:
            js += 0.5 * right * math.log2(right / midpoint)
    return {"total_variation": total_variation, "jensen_shannon_bits": js}


def build_audit(
    *,
    clean: dict[str, set[str]],
    sealed: set[str],
    excluded: set[str],
    universe: set[str],
    metadata: dict[str, dict[str, str]],
    source_files: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    clean_all = set().union(*clean.values()) if clean else set()
    groups = {
        "clean_train": set(clean.get("train", set())),
        "clean_heldout": set(clean.get("heldout", set())),
        "clean64": clean_all,
        "sealed21": set(sealed),
        "excluded_media": set(excluded),
    }
    names = list(groups)
    for left_index, left in enumerate(names):
        for right in names[left_index + 1 :]:
            if left == "clean64" and right in {"clean_train", "clean_heldout"}:
                continue
            if right == "clean64" and left in {"clean_train", "clean_heldout"}:
                continue
            overlap = groups[left] & groups[right]
            if overlap:
                raise ValueError(f"split overlap between {left} and {right}: {sorted(overlap)}")
    accounted = clean_all | sealed | excluded
    if accounted != universe:
        raise ValueError(
            f"task accounting mismatch: missing={sorted(universe - accounted)}, "
            f"extra={sorted(accounted - universe)}"
        )
    missing_metadata = accounted - set(metadata)
    if missing_metadata:
        raise ValueError(f"missing task metadata: {sorted(missing_metadata)}")

    counts: dict[str, dict[str, dict[str, int]]] = {}
    for group, case_ids in groups.items():
        counts[group] = {}
        for dimension in ("category", "difficulty"):
            counts[group][dimension] = dict(
                sorted(Counter(metadata[case_id][dimension] for case_id in case_ids).items())
            )
    clean_categories = set(counts["clean64"]["category"])
    sealed_categories = set(counts["sealed21"]["category"])
    audit = {
        "format": "self_harness.split_coverage_audit.v1",
        "outcome_blind": True,
        "task_accounting": {
            "universe": len(universe),
            **{group: len(case_ids) for group, case_ids in groups.items()},
            "all_accounted_for": accounted == universe,
            "clean_sealed_overlap": len(clean_all & sealed),
        },
        "counts": counts,
        "clean64_vs_sealed21": {
            "category": distribution_distance(
                Counter(counts["clean64"]["category"]),
                Counter(counts["sealed21"]["category"]),
            ),
            "difficulty": distribution_distance(
                Counter(counts["clean64"]["difficulty"]),
                Counter(counts["sealed21"]["difficulty"]),
            ),
            "shared_categories": sorted(clean_categories & sealed_categories),
            "categories_missing_from_sealed": sorted(clean_categories - sealed_categories),
            "categories_novel_in_sealed": sorted(sealed_categories - clean_categories),
        },
    }
    if source_files is not None:
        audit["source_files"] = source_files
    return audit


def count_rows(audit: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for group, dimensions in audit["counts"].items():
        group_total = audit["task_accounting"][group]
        for dimension, labels in dimensions.items():
            for label, count in labels.items():
                rows.append(
                    {
                        "group": group,
                        "dimension": dimension,
                        "label": label,
                        "count": count,
                        "proportion": count / group_total if group_total else 0.0,
                    }
                )
    return rows


def pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def render_markdown(audit: dict[str, Any]) -> str:
    accounting = audit["task_accounting"]
    comparison = audit["clean64_vs_sealed21"]
    lines = [
        "# Clean64–Sealed21 划分覆盖审计",
        "",
        "本审计只读取冻结任务元数据，不读取模型输出、分数、轨迹或 verifier 结果。",
        "",
        f"任务记账：全集 {accounting['universe']} = Clean64 {accounting['clean64']} + Sealed21 {accounting['sealed21']} + 媒体能力边界排除 {accounting['excluded_media']}；Clean64/Sealed21 重叠为 {accounting['clean_sealed_overlap']}。",
        "",
        "## 分布距离",
        "",
        "| 维度 | Total variation | Jensen–Shannon divergence (bits) |",
        "|---|---:|---:|",
    ]
    for dimension in ("category", "difficulty"):
        metrics = comparison[dimension]
        lines.append(
            f"| {dimension} | {metrics['total_variation']:.3f} "
            f"| {metrics['jensen_shannon_bits']:.3f} |"
        )
    lines.extend(
        [
            "",
            f"- 共同类别：{', '.join(comparison['shared_categories']) or '无'}。",
            f"- Clean64 有但 Sealed21 缺失：{', '.join(comparison['categories_missing_from_sealed']) or '无'}。",
            f"- Sealed21 新增类别：{', '.join(comparison['categories_novel_in_sealed']) or '无'}。",
            "",
            "## 类别构成",
            "",
            "| Category | Clean64 | Sealed21 |",
            "|---|---:|---:|",
        ]
    )
    clean_counts = audit["counts"]["clean64"]["category"]
    sealed_counts = audit["counts"]["sealed21"]["category"]
    labels = sorted(set(clean_counts) | set(sealed_counts))
    for label in labels:
        clean_count = clean_counts.get(label, 0)
        sealed_count = sealed_counts.get(label, 0)
        lines.append(
            f"| {label} | {clean_count} ({pct(clean_count / accounting['clean64'])}) "
            f"| {sealed_count} ({pct(sealed_count / accounting['sealed21'])}) |"
        )
    lines.extend(
        [
            "",
            "## 难度构成",
            "",
            "| Difficulty | Clean64 | Sealed21 |",
            "|---|---:|---:|",
        ]
    )
    clean_diff = audit["counts"]["clean64"]["difficulty"]
    sealed_diff = audit["counts"]["sealed21"]["difficulty"]
    for label in sorted(set(clean_diff) | set(sealed_diff)):
        clean_count = clean_diff.get(label, 0)
        sealed_count = sealed_diff.get(label, 0)
        lines.append(
            f"| {label} | {clean_count} ({pct(clean_count / accounting['clean64'])}) "
            f"| {sealed_count} ({pct(sealed_count / accounting['sealed21'])}) |"
        )
    lines.extend(
        [
            "",
            "解释边界：Sealed21 是结果盲、互斥的本地测试集，但不是从目标任务总体随机抽样，也未按 Clean64 类别比例分层。分布距离用于描述覆盖差异，不用于对结果加权、事后选择任务或修改预注册主终点。",
            "",
        ]
    )
    return "\n".join(lines)


def write_svg(path: Path, audit: dict[str, Any]) -> None:
    clean = audit["counts"]["clean64"]["category"]
    sealed = audit["counts"]["sealed21"]["category"]
    labels = sorted(set(clean) | set(sealed), key=lambda label: (-clean.get(label, 0), label))
    clean_total = audit["task_accounting"]["clean64"]
    sealed_total = audit["task_accounting"]["sealed21"]
    width = 980
    top = 82
    row_height = 42
    height = top + len(labels) * row_height + 58
    left = 250
    chart_width = 650
    max_share = max(
        [clean.get(label, 0) / clean_total for label in labels]
        + [sealed.get(label, 0) / sealed_total for label in labels]
    )
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:Arial,sans-serif;fill:#172033}.title{font-size:22px;font-weight:700}.label{font-size:13px}.value{font-size:11px;fill:#475569}.note{font-size:12px;fill:#64748b}</style>',
        '<text x="490" y="32" text-anchor="middle" class="title">Outcome-blind task-category coverage</text>',
        '<rect x="330" y="50" width="14" height="10" fill="#64748b"/><text x="350" y="60" class="label">Clean64</text>',
        '<rect x="440" y="50" width="14" height="10" fill="#0f9d76"/><text x="460" y="60" class="label">Sealed21</text>',
    ]
    for index, label in enumerate(labels):
        y = top + index * row_height
        clean_share = clean.get(label, 0) / clean_total
        sealed_share = sealed.get(label, 0) / sealed_total
        clean_width = clean_share / max_share * chart_width
        sealed_width = sealed_share / max_share * chart_width
        parts.append(
            f'<text x="{left - 12}" y="{y + 15}" text-anchor="end" class="label">{html.escape(label)}</text>'
        )
        parts.append(
            f'<rect x="{left}" y="{y}" width="{clean_width:.1f}" height="13" rx="2" fill="#64748b"/>'
        )
        parts.append(
            f'<rect x="{left}" y="{y + 17}" width="{sealed_width:.1f}" height="13" rx="2" fill="#0f9d76"/>'
        )
        parts.append(
            f'<text x="{left + clean_width + 6:.1f}" y="{y + 11}" class="value">{100 * clean_share:.1f}%</text>'
        )
        parts.append(
            f'<text x="{left + sealed_width + 6:.1f}" y="{y + 28}" class="value">{100 * sealed_share:.1f}%</text>'
        )
    parts.append(
        f'<text x="490" y="{height - 18}" text-anchor="middle" class="note">Metadata coverage only; no model outcomes were read.</text>'
    )
    parts.append("</svg>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean-config", type=Path, default=ROOT / "eval/configs/harbor_local_clean64.toml")
    parser.add_argument("--sealed-manifest", type=Path, default=ROOT / "configs/splits/sealed21.json")
    parser.add_argument("--task-root", type=Path, default=ROOT / "runs/terminal-bench-2-reliable-v2")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "paper/generated/split-coverage")
    args = parser.parse_args(argv)
    manifest = load_json(args.sealed_manifest.resolve())
    clean = clean_splits(args.clean_config.resolve())
    sealed = {str(item["case_id"]) for item in manifest["tasks"]}
    excluded = {str(item["case_id"]) for item in manifest["excluded_unused_tasks"]}
    task_root = args.task_root.resolve()
    universe = {
        path.name for path in task_root.iterdir() if path.is_dir() and (path / "task.toml").is_file()
    }
    metadata = {case_id: task_metadata(task_root / case_id) for case_id in universe}
    source_files = [
        source_file_record("clean_config", args.clean_config),
        source_file_record("sealed_manifest", args.sealed_manifest),
        *[
            source_file_record("task_metadata", task_root / case_id / "task.toml")
            for case_id in sorted(universe)
        ],
    ]
    audit = build_audit(
        clean=clean,
        sealed=sealed,
        excluded=excluded,
        universe=universe,
        metadata=metadata,
        source_files=source_files,
    )
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "split_coverage.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    rows = count_rows(audit)
    with (output_dir / "split_coverage_counts.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (output_dir / "SPLIT_COVERAGE_AUDIT.md").write_text(
        render_markdown(audit), encoding="utf-8"
    )
    write_svg(output_dir / "split_coverage.svg", audit)
    print(output_dir / "SPLIT_COVERAGE_AUDIT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
