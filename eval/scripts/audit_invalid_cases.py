#!/usr/bin/env python3
"""Create a read-only, hash-bound audit of effective-invalid result cells."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from result_validity import effective_invalid_reason, invalid_reason_category, load_json_object

FORMAT = "self_harness.invalid_audit.v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
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


def audit_result(path: Path, side: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    before = sha256_file(path)
    payload = load_json_object(path)
    cells: list[dict[str, Any]] = []
    for split, repeats in payload.get("splits", {}).items():
        if not isinstance(repeats, list):
            raise ValueError(f"{path}: split {split!r} is not a list")
        for repeat in repeats:
            if not isinstance(repeat, dict):
                raise ValueError(f"{path}: split {split!r} contains a non-object repeat")
            repeat_id = int(repeat["repeat"])
            for case in repeat.get("case_results", []):
                if not isinstance(case, dict):
                    raise ValueError(f"{path}: split {split!r} contains a non-object case")
                reason = effective_invalid_reason(case, require_reward=True)
                if reason is None:
                    continue
                cells.append(
                    {
                        "side": side,
                        "split": str(split),
                        "repeat": repeat_id,
                        "case_id": str(case["case_id"]),
                        "category": invalid_reason_category(reason),
                        "reason": reason,
                        "attempt_count": int(case.get("attempt_count", 1)),
                    }
                )
    after = sha256_file(path)
    if before != after:
        raise ValueError(f"result changed while being audited: {path}")
    source = {
        "path": str(path),
        "sha256": before,
        "source_stable": True,
        "invalid_count": len(cells),
    }
    return source, cells


def build_audit(inputs: list[tuple[str, Path]]) -> dict[str, Any]:
    sources: dict[str, Any] = {}
    cells: list[dict[str, Any]] = []
    source_paths: set[Path] = set()
    for side, path in inputs:
        path = path.resolve()
        if side in sources:
            raise ValueError(f"duplicate side: {side}")
        if path in source_paths:
            raise ValueError(f"duplicate source result: {path}")
        source_paths.add(path)
        source, side_cells = audit_result(path, side)
        sources[side] = source
        cells.extend(side_cells)
    for side, source in sources.items():
        path = Path(source["path"])
        if sha256_file(path) != source["sha256"]:
            raise ValueError(f"{side} result changed before audit completion: {path}")
    cells.sort(key=lambda item: (item["side"], item["split"], item["repeat"], item["case_id"]))
    categories: dict[str, int] = {}
    by_side: dict[str, int] = {}
    by_side_category: dict[str, int] = {}
    by_split_repeat: dict[str, int] = {}
    for cell in cells:
        category = cell["category"]
        categories[category] = categories.get(category, 0) + 1
        side = cell["side"]
        by_side[side] = by_side.get(side, 0) + 1
        side_category = f"{side}:{category}"
        by_side_category[side_category] = by_side_category.get(side_category, 0) + 1
        key = f"{side}:{cell['split']}/repeat-{cell['repeat']:02d}"
        by_split_repeat[key] = by_split_repeat.get(key, 0) + 1
    return {
        "format": FORMAT,
        "sources": sources,
        "total_invalid": len(cells),
        "invalid_by_side": dict(sorted(by_side.items())),
        "invalid_by_side_category": dict(sorted(by_side_category.items())),
        "invalid_by_split_repeat": dict(sorted(by_split_repeat.items())),
        "reason_categories": dict(sorted(categories.items())),
        "cells": cells,
    }


def verify_audit_artifact(artifact_path: Path, rebuilt: dict[str, Any]) -> None:
    before = sha256_file(artifact_path)
    artifact = load_json_object(artifact_path)
    after = sha256_file(artifact_path)
    if before != after:
        raise ValueError(f"audit artifact changed while being verified: {artifact_path}")
    if artifact.get("format") != FORMAT:
        raise ValueError("invalid audit artifact has unexpected format")
    if artifact != rebuilt:
        raise ValueError("invalid audit artifact does not match current source results")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-result", required=True, type=Path)
    parser.add_argument("--candidate-result", required=True, type=Path)
    output_group = parser.add_mutually_exclusive_group(required=True)
    output_group.add_argument("--output", type=Path)
    output_group.add_argument("--verify-artifact", type=Path)
    args = parser.parse_args(argv)
    audit = build_audit(
        [
            ("baseline", args.baseline_result.expanduser().resolve()),
            ("candidate", args.candidate_result.expanduser().resolve()),
        ]
    )
    if args.verify_artifact is not None:
        verify_audit_artifact(args.verify_artifact.expanduser().resolve(), audit)
        print(json.dumps({"verified": str(args.verify_artifact.resolve()), "total_invalid": audit["total_invalid"]}, ensure_ascii=False))
        return 0
    output_path = args.output.expanduser().resolve()
    write_json_atomic(output_path, audit)
    print(json.dumps({"output": str(args.output.resolve()), "total_invalid": audit["total_invalid"], "reason_categories": audit["reason_categories"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
