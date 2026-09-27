#!/usr/bin/env python3
"""创建或验证从未参与 Self-Harness 选择的 Sealed21 任务清单。"""
from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any


FORMAT = "self_harness.sealed_split.v1"
MEDIA_EXTENSIONS = {
    ".avi",
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".tiff",
    ".wav",
    ".webm",
    ".webp",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def task_digest(task_dir: Path) -> str:
    """哈希相对路径、文件长度和内容，固定整个任务定义。"""
    digest = hashlib.sha256()
    for path in sorted((path for path in task_dir.rglob("*") if path.is_file()), key=lambda p: p.as_posix()):
        relative = path.relative_to(task_dir).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def clean_cases(config_path: Path) -> set[str]:
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    return {str(case["case_id"]) for case in raw.get("cases", [])}


def media_extensions(task_dir: Path) -> list[str]:
    return sorted(
        {
            path.suffix.lower()
            for path in task_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
        }
    )


def task_metadata(task_dir: Path) -> dict[str, Any]:
    raw = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    metadata = raw.get("metadata", {})
    return {
        "case_id": task_dir.name,
        "category": metadata.get("category"),
        "difficulty": metadata.get("difficulty"),
        "task_sha256": task_digest(task_dir),
    }


def seen_result_paths(runs_root: Path, case_ids: set[str], task_root: Path) -> dict[str, list[str]]:
    seen = {case_id: [] for case_id in case_ids}
    resolved_task_root = task_root.resolve()
    for path in runs_root.rglob("result.json"):
        try:
            path.resolve().relative_to(resolved_task_root)
            continue
        except ValueError:
            pass
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        stack = [payload]
        matched = set()
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                case_id = value.get("case_id")
                if case_id in seen:
                    matched.add(str(case_id))
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
        for case_id in matched:
            seen[case_id].append(str(path.resolve()))
    return {case_id: paths for case_id, paths in seen.items() if paths}


def build_manifest(*, task_root: Path, clean_config: Path, runs_root: Path) -> dict[str, Any]:
    used = clean_cases(clean_config)
    repo_root = runs_root.parent.resolve()
    selected = []
    excluded = []
    for task_dir in sorted((path for path in task_root.iterdir() if path.is_dir()), key=lambda path: path.name):
        if task_dir.name in used:
            continue
        media = media_extensions(task_dir)
        if media:
            excluded.append({"case_id": task_dir.name, "reason": "local_media_input", "extensions": media})
        else:
            selected.append(task_metadata(task_dir))
    if len(selected) != 21:
        raise ValueError(f"selection rule expected 21 text-only unused tasks, found {len(selected)}")
    seen = seen_result_paths(runs_root, {item["case_id"] for item in selected}, task_root)
    if seen:
        raise ValueError(f"sealed candidates already appear in evaluation results: {seen}")
    return {
        "format": FORMAT,
        "classification": "frozen-before-sealed-evaluation",
        "selection_rule": {
            "universe": "directories in terminal-bench-2-reliable-v2",
            "exclude_clean64": True,
            "exclude_local_media_extensions": sorted(MEDIA_EXTENSIONS),
            "outcome_blind": True,
        },
        "task_root": task_root.resolve().relative_to(repo_root).as_posix(),
        "clean_config": clean_config.resolve().relative_to(repo_root).as_posix(),
        "clean_case_ids_sha256": sha256_bytes(
            (json.dumps(sorted(used), separators=(",", ":")) + "\n").encode("utf-8")
        ),
        "prior_result_references": 0,
        "task_count": len(selected),
        "tasks": selected,
        "excluded_unused_tasks": excluded,
    }


def render_config(task_ids: list[str]) -> str:
    lines = [
        "[eval]",
        'name = "tb2-sealed21"',
        'runner = "harbor_cli"',
        'model = "${SELF_HARNESS_MODEL}"',
        "repeats = 2",
        'evals_project = "../.."',
        'harness_workspace = "../harness_workspace"',
        'task_root = "${SELF_HARNESS_TB2_ROOT}"',
        'agent_import_path = "self_harness_harbor.harbor_wrapper:FixedHarnessWrapper"',
        'environment = "docker"',
        'agent_kwargs = ["hard_timeout_sec=1800"]',
        "case_concurrency = 4",
        "timeout_s = 3000",
        "infrastructure_retries = 2",
        "",
    ]
    for case_id in task_ids:
        lines.extend(
            [
                "[[cases]]",
                f'case_id = "{case_id}"',
                'split = "sealed"',
                'stratum = "sealed21"',
                "",
            ]
        )
    return "\n".join(lines)


def write_frozen(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"refusing to overwrite frozen file with different content: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def verify(*, manifest_path: Path, config_path: Path) -> None:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("format") != FORMAT:
        raise ValueError(f"unexpected sealed manifest format: {manifest.get('format')!r}")
    expected_lock = sha256_bytes(manifest_bytes)
    actual_lock = manifest_path.with_suffix(manifest_path.suffix + ".sha256").read_text(encoding="ascii").strip()
    if actual_lock != expected_lock:
        raise ValueError("sealed manifest lock hash mismatch")
    repo_root = manifest_path.resolve().parents[2]
    task_root = repo_root / manifest["task_root"]
    clean_config = repo_root / manifest["clean_config"]
    task_ids = [str(item["case_id"]) for item in manifest["tasks"]]
    clean_ids = clean_cases(clean_config)
    clean_ids_hash = sha256_bytes(
        (json.dumps(sorted(clean_ids), separators=(",", ":")) + "\n").encode("utf-8")
    )
    if clean_ids_hash != manifest["clean_case_ids_sha256"]:
        raise ValueError("Clean64 task identity changed after sealed split freeze")
    if set(task_ids) & clean_ids:
        raise ValueError("sealed split overlaps Clean64")
    for item in manifest["tasks"]:
        actual = task_digest(task_root / item["case_id"])
        if actual != item["task_sha256"]:
            raise ValueError(f"sealed task hash mismatch: {item['case_id']}")
    config_raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    config_ids = [str(case["case_id"]) for case in config_raw.get("cases", [])]
    if config_ids != task_ids:
        raise ValueError("sealed eval config does not match frozen task order")
    expected_config = render_config(task_ids).encode("utf-8")
    if config_path.read_bytes() != expected_config:
        raise ValueError("sealed eval config bytes differ from the frozen canonical configuration")
    print(f"verified sealed split: {len(task_ids)} tasks, no Clean64 overlap, hashes intact")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-root", required=True, type=Path)
    parser.add_argument("--clean-config", required=True, type=Path)
    parser.add_argument("--runs-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config-output", required=True, type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    manifest_path = args.output.resolve()
    config_path = args.config_output.resolve()
    if args.verify:
        verify(manifest_path=manifest_path, config_path=config_path)
        return 0
    manifest = build_manifest(
        task_root=args.task_root.resolve(),
        clean_config=args.clean_config.resolve(),
        runs_root=args.runs_root.resolve(),
    )
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    config_bytes = render_config([item["case_id"] for item in manifest["tasks"]]).encode("utf-8")
    write_frozen(manifest_path, manifest_bytes)
    write_frozen(manifest_path.with_suffix(manifest_path.suffix + ".sha256"), (sha256_bytes(manifest_bytes) + "\n").encode("ascii"))
    write_frozen(config_path, config_bytes)
    verify(manifest_path=manifest_path, config_path=config_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
