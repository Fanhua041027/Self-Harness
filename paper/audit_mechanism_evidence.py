#!/usr/bin/env python3
"""将诊断、候选修改和配对结果连接为可审计的机制证据链。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
VALIDITY_DIR = ROOT / "eval" / "scripts"
if str(VALIDITY_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDITY_DIR))

from result_validity import effective_invalid_reason

from paper.analyze_experiments import flatten_result, load_json as load_strict_json


def load_json(path: Path) -> dict[str, Any]:
    return load_strict_json(path)


def source_file_record(role: str, path: Path) -> dict[str, str]:
    resolved = path.resolve()
    return {
        "role": role,
        "path": str(resolved),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
    }


def parse_expected(value: str) -> tuple[str, int]:
    case_id, separator, repeat_text = value.rpartition("#repeat-")
    if not separator or not case_id:
        raise ValueError(f"invalid expected case reference: {value!r}")
    return case_id, int(repeat_text)


def transition_row(
    key: tuple[str, int, str],
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    baseline_invalid = effective_invalid_reason(baseline, require_reward=True)
    candidate_invalid = effective_invalid_reason(candidate, require_reward=True)
    if baseline_invalid or candidate_invalid:
        transition = "invalid-involved"
    elif not baseline["passed"] and candidate["passed"]:
        transition = "fail-to-pass"
    elif baseline["passed"] and not candidate["passed"]:
        transition = "pass-to-fail"
    elif baseline["passed"]:
        transition = "pass-to-pass"
    else:
        transition = "fail-to-fail"
    return {
        "split": key[0],
        "repeat": key[1],
        "case_id": key[2],
        "baseline_passed": bool(baseline["passed"]),
        "candidate_passed": bool(candidate["passed"]),
        "baseline_invalid_reason": baseline_invalid,
        "candidate_invalid_reason": candidate_invalid,
        "transition": transition,
    }


def build_audit(
    *,
    proposal: dict[str, Any],
    baseline_path: Path,
    candidate_path: Path,
    source_files: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    baseline = flatten_result(baseline_path)
    candidate = flatten_result(candidate_path)
    if baseline.keys() != candidate.keys():
        raise ValueError("baseline and candidate keys do not match")
    transitions = [transition_row(key, baseline[key], candidate[key]) for key in sorted(baseline)]
    expected_refs = [str(value) for value in proposal.get("metadata", {}).get("expected_affected_cases", [])]
    targets = []
    for reference in expected_refs:
        case_id, repeat = parse_expected(reference)
        matches = [row for row in transitions if row["case_id"] == case_id and row["repeat"] == repeat]
        if len(matches) != 1:
            raise ValueError(f"expected exactly one paired result for {reference}, found {len(matches)}")
        targets.append({"reference": reference, **matches[0]})
    direct_confirmed = bool(targets) and any(row["transition"] == "fail-to-pass" for row in targets)
    counts = {
        name: sum(row["transition"] == name for row in transitions)
        for name in ("fail-to-pass", "pass-to-fail", "pass-to-pass", "fail-to-fail", "invalid-involved")
    }
    audit = {
        "format": "self_harness.mechanism_evidence_audit.v1",
        "candidate_id": proposal.get("proposal_id"),
        "selected_cluster_id": proposal.get("metadata", {}).get("selected_cluster_id"),
        "mechanism": proposal.get("mechanism"),
        "exact_hook": proposal.get("metadata", {}).get("exact_hook"),
        "expected_affected_cases": expected_refs,
        "direct_target_confirmed": direct_confirmed,
        "interpretation": (
            "direct targeted fail-to-pass evidence observed"
            if direct_confirmed
            else "no valid fail-to-pass transition on the predeclared direct target"
        ),
        "transition_counts": counts,
        "direct_targets": targets,
        "transitions": transitions,
    }
    if source_files is not None:
        audit["source_files"] = source_files
    return audit


def render_markdown(audit: dict[str, Any]) -> str:
    target_lines = []
    for row in audit["direct_targets"]:
        reasons = "; ".join(
            reason
            for reason in (row["baseline_invalid_reason"], row["candidate_invalid_reason"])
            if reason
        )
        target_lines.append(
            f"| `{row['reference']}` | {row['split']} | {row['transition']} | {reasons or '-'} |"
        )
    counts = audit["transition_counts"]
    return "\n".join(
        [
            "# Qwen 机制证据链审计",
            "",
            f"- 诊断簇：`{audit['selected_cluster_id']}`",
            f"- 修改 hook：`{audit['exact_hook']}`",
            f"- 机制：{audit['mechanism']}",
            f"- 直接目标是否确认：**{'是' if audit['direct_target_confirmed'] else '否'}**",
            "",
            "## 预声明直接目标",
            "",
            "| 目标 | Split | 配对转移 | 无效原因 |",
            "|---|---|---|---|",
            *target_lines,
            "",
            "直接机制确认要求预声明目标出现 baseline 有效失败、candidate 有效通过。当前目标未满足该条件，不能用总体固定分母差值替代直接机制证据。",
            "",
            "## 全部配对单元审计",
            "",
            f"- 有效 fail→pass：{counts['fail-to-pass']}",
            f"- 有效 pass→fail：{counts['pass-to-fail']}",
            f"- 有效 pass→pass：{counts['pass-to-pass']}",
            f"- 有效 fail→fail：{counts['fail-to-fail']}",
            f"- 任一侧 invalid：{counts['invalid-involved']}",
            "",
            "这些总体转移仅用于审计；只有 Clean64 补跑至零 invalid 后，才能重新判断机制与总体效应。",
            "",
        ]
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_svg(path: Path, audit: dict[str, Any]) -> None:
    cluster_parts = [html.escape(part.strip()) for part in str(audit["selected_cluster_id"]).split("/")]
    cluster_lines = "".join(
        f'<text x="65" y="{135 + index * 23}" class="body">{part}</text>'
        for index, part in enumerate(cluster_parts)
    )
    hook = html.escape(str(audit["exact_hook"]))
    target = html.escape(", ".join(audit["expected_affected_cases"]))
    outcome = "Confirmed" if audit["direct_target_confirmed"] else "Not confirmed"
    color = "#16845b" if audit["direct_target_confirmed"] else "#c2413b"
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="300" viewBox="0 0 1100 300">
<rect width="1100" height="300" fill="white"/>
<style>text{{font-family:Arial,sans-serif;fill:#172033}}.title{{font-size:22px;font-weight:700}}.head{{font-size:17px;font-weight:700}}.body{{font-size:13px}}</style>
<text x="550" y="34" text-anchor="middle" class="title">Mechanism evidence chain (historical Qwen candidate)</text>
<rect x="45" y="75" width="285" height="145" rx="12" fill="#eef4ff" stroke="#6682b5"/>
<text x="65" y="105" class="head">1. Diagnosed mechanism</text>{cluster_lines}
<rect x="405" y="75" width="285" height="145" rx="12" fill="#f3f0ff" stroke="#8068b5"/>
<text x="425" y="105" class="head">2. Candidate edit</text><text x="425" y="135" class="body">hook: {hook}</text>
<rect x="765" y="75" width="290" height="145" rx="12" fill="#fff3f1" stroke="{color}" stroke-width="2"/>
<text x="785" y="105" class="head">3. Direct target outcome</text><text x="785" y="135" class="body">{target}</text><text x="785" y="175" class="head" fill="{color}">{outcome}</text>
<path d="M330 147 H405" stroke="#64748b" stroke-width="3" marker-end="url(#a)"/><path d="M690 147 H765" stroke="#64748b" stroke-width="3" marker-end="url(#a)"/>
<defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#64748b"/></marker></defs>
<text x="550" y="266" text-anchor="middle" class="body">Direct confirmation requires a valid fail-to-pass transition on the predeclared target.</text>
</svg>'''
    path.write_text(svg + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", required=True, type=Path)
    parser.add_argument("--baseline-result", required=True, type=Path)
    parser.add_argument("--candidate-result", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    audit = build_audit(
        proposal=load_json(args.proposal),
        baseline_path=args.baseline_result,
        candidate_path=args.candidate_result,
        source_files=[
            source_file_record("proposal", args.proposal),
            source_file_record("baseline_result", args.baseline_result),
            source_file_record("candidate_result", args.candidate_result),
        ],
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "mechanism_evidence.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "MECHANISM_EVIDENCE.md").write_text(render_markdown(audit), encoding="utf-8")
    write_csv(args.output_dir / "mechanism_transitions.csv", audit["transitions"])
    write_svg(args.output_dir / "mechanism_evidence.svg", audit)
    print(args.output_dir / "MECHANISM_EVIDENCE.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
