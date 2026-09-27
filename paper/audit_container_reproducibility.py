#!/usr/bin/env python3
"""结果盲审计任务 Dockerfile 的基础镜像是否使用不可变 digest。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.IGNORECASE)


def source_file_record(role: str, path: Path) -> dict[str, str]:
    resolved = path.resolve()
    return {
        "role": role,
        "path": str(resolved),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
    }


def classify_reference(reference: str) -> str:
    if "${" in reference or "$" in reference:
        return "variable"
    if "@sha256:" in reference.lower():
        return "digest_pinned"
    tail = reference.rsplit("/", 1)[-1]
    if ":" not in tail or tail.lower().endswith(":latest"):
        return "floating_tag"
    return "explicit_tag"


def dockerfile_references(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = FROM_RE.match(line)
        if not match:
            continue
        reference = match.group(1)
        rows.append(
            {
                "line": line_number,
                "reference": reference,
                "pinning": classify_reference(reference),
            }
        )
    return rows


def build_audit(
    task_root: Path, *, source_files: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    task_dirs = sorted(
        (path for path in task_root.iterdir() if path.is_dir() and (path / "task.toml").is_file()),
        key=lambda path: path.name,
    )
    tasks = []
    all_references = []
    for task_dir in task_dirs:
        dockerfiles = sorted(task_dir.rglob("Dockerfile*"), key=lambda path: path.as_posix())
        references = []
        for dockerfile in dockerfiles:
            for row in dockerfile_references(dockerfile):
                item = {
                    "dockerfile": dockerfile.relative_to(task_dir).as_posix(),
                    **row,
                }
                references.append(item)
                all_references.append({"case_id": task_dir.name, **item})
        tasks.append(
            {
                "case_id": task_dir.name,
                "dockerfile_count": len(dockerfiles),
                "base_reference_count": len(references),
                "all_bases_digest_pinned": bool(references)
                and all(item["pinning"] == "digest_pinned" for item in references),
                "references": references,
            }
        )
    pinning_counts = Counter(item["pinning"] for item in all_references)
    reference_counts = Counter(item["reference"] for item in all_references)
    missing = [item["case_id"] for item in tasks if item["dockerfile_count"] == 0]
    no_from = [item["case_id"] for item in tasks if item["base_reference_count"] == 0]
    audit = {
        "format": "self_harness.container_reproducibility_audit.v1",
        "outcome_blind": True,
        "task_count": len(tasks),
        "tasks_with_no_dockerfile": missing,
        "tasks_with_no_from_reference": no_from,
        "base_reference_count": len(all_references),
        "pinning_counts": dict(sorted(pinning_counts.items())),
        "tasks_all_bases_digest_pinned": sum(item["all_bases_digest_pinned"] for item in tasks),
        "unique_base_references": [
            {"reference": reference, "uses": count, "pinning": classify_reference(reference)}
            for reference, count in sorted(reference_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "tasks": tasks,
    }
    if source_files is not None:
        audit["source_files"] = source_files
    return audit


def render_markdown(audit: dict[str, Any]) -> str:
    counts = audit["pinning_counts"]
    lines = [
        "# 任务容器基础镜像可复现性审计",
        "",
        "本审计只读取任务 Dockerfile，不读取模型输出或评测结果，也不拉取镜像。",
        "",
        f"- 任务数：{audit['task_count']}。",
        f"- `FROM` 引用数：{audit['base_reference_count']}。",
        f"- 所有基础镜像均使用 digest 的任务：{audit['tasks_all_bases_digest_pinned']} / {audit['task_count']}。",
        f"- Digest-pinned 引用：{counts.get('digest_pinned', 0)}；显式 tag：{counts.get('explicit_tag', 0)}；浮动/latest：{counts.get('floating_tag', 0)}；变量：{counts.get('variable', 0)}。",
        "",
        "## 基础镜像引用",
        "",
        "| Reference | Uses | Pinning |",
        "|---|---:|---|",
    ]
    for item in audit["unique_base_references"]:
        lines.append(f"| `{item['reference']}` | {item['uses']} | {item['pinning']} |")
    lines.extend(
        [
            "",
            "解释边界：显式版本 tag 比 `latest` 更稳定，但 tag 仍可被 registry 重新指向，不能提供内容不可变性。只有 `@sha256:` digest 能静态证明基础镜像内容身份。任务目录 SHA256 可以证明 Dockerfile 未变，不能证明其 tag 在不同时间解析到相同镜像。正式运行应记录实际解析的 image ID/RepoDigest；由于当前 Sealed21 尚未执行，本报告不事后改写冻结任务 Dockerfile。",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task-root", type=Path, default=ROOT / "runs/terminal-bench-2-reliable-v2"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "paper/generated/container-audit"
    )
    args = parser.parse_args(argv)
    task_root = args.task_root.resolve()
    task_dirs = sorted(
        (path for path in task_root.iterdir() if path.is_dir() and (path / "task.toml").is_file()),
        key=lambda path: path.name,
    )
    source_files = []
    for task_dir in task_dirs:
        source_files.append(source_file_record("task_metadata", task_dir / "task.toml"))
        source_files.extend(
            source_file_record("dockerfile", dockerfile)
            for dockerfile in sorted(task_dir.rglob("Dockerfile*"), key=lambda path: path.as_posix())
        )
    audit = build_audit(task_root, source_files=source_files)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "container_reproducibility.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    rows = [
        {"case_id": item["case_id"], **reference}
        for item in audit["tasks"]
        for reference in item["references"]
    ]
    with (output_dir / "container_base_images.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (output_dir / "CONTAINER_REPRODUCIBILITY_AUDIT.md").write_text(
        render_markdown(audit), encoding="utf-8"
    )
    print(output_dir / "CONTAINER_REPRODUCIBILITY_AUDIT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
