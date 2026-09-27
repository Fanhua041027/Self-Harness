#!/usr/bin/env python3
"""Issue and verify a hash-bound strict promotion receipt (offline only)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for directory in (ROOT / "eval" / "scripts", ROOT / "acceptance" / "scripts"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
from audit_invalid_cases import build_audit, verify_audit_artifact
from result_validity import load_json_object
from run_acceptance_gate import sha256_file, verify_acceptance_artifact

FORMAT = "self_harness.strict_promotion_receipt.v1"


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def identity(path: Path) -> dict[str, str]:
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"missing input: {path}")
    return {"path": str(path), "sha256": sha256_file(path)}


def load_candidate(queue_path: Path, candidate_id: str, branches_root: Path) -> tuple[dict[str, Any], Path, Path]:
    queue = load_json_object(queue_path)
    matches = [item for item in queue.get("candidates", []) if isinstance(item, dict) and item.get("candidate_id") == candidate_id]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one candidate {candidate_id!r}; found {len(matches)}")
    entry = matches[0]
    candidate_dir = Path(str(entry.get("candidate_dir", ""))).expanduser().resolve()
    root = branches_root.resolve()
    if candidate_dir != root and root not in candidate_dir.parents:
        raise ValueError(f"candidate directory is outside branches root: {candidate_dir}")
    manifest = candidate_dir / "manifest.json"
    result = candidate_dir / "eval" / "result.json"
    if Path(str(entry.get("eval_result", ""))).expanduser().resolve() != result.resolve():
        raise ValueError("queue eval_result is not bound to candidate directory")
    return entry, manifest, result


def build_receipt(*, queue_path: Path, candidate_id: str, branches_root: Path, baseline_result: Path, acceptance: Path, invalid_audit: Path) -> dict[str, Any]:
    queue_path = queue_path.resolve(); baseline_result = baseline_result.resolve(); acceptance = acceptance.resolve(); invalid_audit = invalid_audit.resolve()
    entry, manifest_path, candidate_result = load_candidate(queue_path, candidate_id, branches_root)
    manifest = load_json_object(manifest_path)
    if manifest.get("candidate_id") != candidate_id:
        raise ValueError("candidate manifest id does not match candidate")
    baseline = load_json_object(baseline_result)
    candidate = load_json_object(candidate_result)
    if acceptance.name != "acceptance.strict.json":
        raise ValueError("promotion requires acceptance.strict.json, not historical acceptance")
    acceptance_payload = load_json_object(acceptance)
    acceptance_hash = sha256_file(acceptance)
    verify_acceptance_artifact(artifact=acceptance_payload, baseline_result_path=baseline_result, candidate_result_path=candidate_result, artifact_path=acceptance, artifact_sha256=acceptance_hash, splits=("train", "heldout"), expected_repeats=2)
    if acceptance_payload.get("decision") != "accepted" or acceptance_payload.get("accepted") is not True:
        raise ValueError("strict acceptance is not accepted")
    rebuilt = build_audit([("baseline", baseline_result), ("candidate", candidate_result)])
    verify_audit_artifact(invalid_audit.resolve(), rebuilt)
    if rebuilt["total_invalid"] != 0:
        raise ValueError(f"promotion requires zero invalid cells, got {rebuilt['total_invalid']}")
    binding = {
        "candidate_id": candidate_id,
        "queue": identity(queue_path),
        "candidate_manifest": identity(manifest_path),
        "baseline_result": identity(baseline_result),
        "candidate_result": identity(candidate_result),
        "acceptance": identity(acceptance),
        "invalid_audit": identity(invalid_audit),
        "model": baseline.get("model"),
        "candidate_model": candidate.get("model"),
    }
    if binding["model"] != binding["candidate_model"]:
        raise ValueError("baseline and candidate model identities differ")
    return {"format": FORMAT, "decision": "promoted", "source_hashes_stable": True, "binding": binding}


def issue_or_verify(receipt_path: Path, receipt: dict[str, Any]) -> None:
    receipt_path = receipt_path.resolve()
    if receipt_path.exists():
        existing = load_json_object(receipt_path)
        if existing != receipt:
            raise ValueError(f"promotion receipt already exists with different binding: {receipt_path}")
        return
    atomic_write(receipt_path, receipt)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--branches-root", required=True, type=Path)
    parser.add_argument("--baseline-result", required=True, type=Path)
    parser.add_argument("--acceptance", required=True, type=Path)
    parser.add_argument("--invalid-audit", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--verify-artifact", action="store_true")
    args = parser.parse_args(argv)
    if args.verify_artifact and not args.receipt.expanduser().resolve().is_file():
        raise ValueError(f"promotion receipt not found: {args.receipt.resolve()}")
    receipt = build_receipt(queue_path=args.queue, candidate_id=args.candidate_id, branches_root=args.branches_root, baseline_result=args.baseline_result, acceptance=args.acceptance, invalid_audit=args.invalid_audit)
    if args.verify_artifact:
        existing = load_json_object(args.receipt)
        if existing != receipt:
            raise ValueError("promotion receipt does not match current inputs")
        print(f"verified promotion receipt: {args.receipt.resolve()}")
    else:
        issue_or_verify(args.receipt, receipt)
        print(f"promotion receipt: {args.receipt.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
