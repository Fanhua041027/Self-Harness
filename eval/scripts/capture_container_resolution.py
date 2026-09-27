#!/usr/bin/env python3
"""在不拉取镜像的前提下记录冻结任务基础镜像的本地内容身份。"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from paper.audit_container_reproducibility import dockerfile_references
from paper.analyze_experiments import load_json


FORMAT = "self_harness.container_resolution.v1"


def selected_references(
    *, task_root: Path, case_ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    references: dict[str, list[dict[str, Any]]] = {}
    for case_id in sorted(case_ids):
        task_dir = task_root / case_id
        for dockerfile in sorted(task_dir.rglob("Dockerfile*"), key=lambda path: path.as_posix()):
            for item in dockerfile_references(dockerfile):
                references.setdefault(item["reference"], []).append(
                    {
                        "case_id": case_id,
                        "dockerfile": dockerfile.relative_to(task_dir).as_posix(),
                        "line": item["line"],
                    }
                )
    return references


def inspect_local_image(reference: str, docker_executable: str) -> dict[str, Any] | None:
    completed = subprocess.run(
        [docker_executable, "image", "inspect", reference],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return None
    payload = json.loads(completed.stdout)
    return payload[0] if payload else None


def stable_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_ids": snapshot["case_ids"],
        "docker_client_version": snapshot["docker_client_version"],
        "docker_server_version": snapshot["docker_server_version"],
        "images": [
            {
                key: image[key]
                for key in (
                    "reference",
                    "used_by",
                    "found",
                    "image_id",
                    "repo_digests",
                    "repo_tags",
                    "created",
                    "os",
                    "architecture",
                )
            }
            for image in snapshot["images"]
        ],
    }


def resolution_bundle_sha256(snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(
        stable_payload(snapshot), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_snapshot(
    *,
    task_root: Path,
    case_ids: set[str],
    docker_executable: str,
    docker_client_version: str,
    docker_server_version: str,
    inspector: Callable[[str, str], dict[str, Any] | None] = inspect_local_image,
) -> dict[str, Any]:
    references = selected_references(task_root=task_root, case_ids=case_ids)
    images = []
    for reference, uses in sorted(references.items()):
        inspected = inspector(reference, docker_executable)
        images.append(
            {
                "reference": reference,
                "used_by": uses,
                "found": inspected is not None,
                "image_id": inspected.get("Id") if inspected else None,
                "repo_digests": sorted(inspected.get("RepoDigests") or []) if inspected else [],
                "repo_tags": sorted(inspected.get("RepoTags") or []) if inspected else [],
                "created": inspected.get("Created") if inspected else None,
                "os": inspected.get("Os") if inspected else None,
                "architecture": inspected.get("Architecture") if inspected else None,
            }
        )
    snapshot = {
        "format": FORMAT,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "outcome_blind": True,
        "pull_performed": False,
        "docker_executable": docker_executable,
        "docker_client_version": docker_client_version,
        "docker_server_version": docker_server_version,
        "case_ids": sorted(case_ids),
        "reference_count": len(images),
        "resolved_count": sum(image["found"] for image in images),
        "complete": bool(images) and all(image["found"] for image in images),
        "images": images,
    }
    snapshot["bundle_sha256"] = resolution_bundle_sha256(snapshot)
    return snapshot


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    if snapshot.get("format") != FORMAT:
        raise ValueError("unexpected container resolution format")
    if snapshot.get("pull_performed") is not False:
        raise ValueError("container resolution must not pull images")
    case_ids = snapshot.get("case_ids")
    if not isinstance(case_ids, list) or not all(
        isinstance(case_id, str) and case_id for case_id in case_ids
    ):
        raise ValueError("container resolution case IDs must be sorted and unique")
    if case_ids != sorted(set(case_ids)):
        raise ValueError("container resolution case IDs must be sorted and unique")
    images = snapshot.get("images")
    if not isinstance(images, list):
        raise ValueError("container resolution images must be a list")
    if snapshot.get("reference_count") != len(images):
        raise ValueError("container resolution reference count mismatch")
    references = []
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get("reference"), str):
            raise ValueError("container resolution image reference is malformed")
        if image["reference"] in references:
            raise ValueError("container resolution contains duplicate references")
        if not isinstance(image.get("found"), bool):
            raise ValueError("container resolution found flag is malformed")
        if image["found"] and not isinstance(image.get("image_id"), str):
            raise ValueError("resolved container image is missing image ID")
        references.append(image["reference"])
    if resolution_bundle_sha256(snapshot) != snapshot.get("bundle_sha256"):
        raise ValueError("container resolution bundle hash mismatch")
    resolved = sum(image["found"] for image in images)
    if not isinstance(snapshot.get("resolved_count"), int) or isinstance(
        snapshot.get("resolved_count"), bool
    ):
        raise ValueError("container resolution resolved count is malformed")
    if resolved != snapshot.get("resolved_count"):
        raise ValueError("container resolution count mismatch")
    expected_complete = bool(images) and resolved == len(images)
    if not isinstance(snapshot.get("complete"), bool) or snapshot["complete"] != expected_complete:
        raise ValueError("container resolution completeness mismatch")


def docker_versions(docker_executable: str) -> tuple[str, str]:
    completed = subprocess.run(
        [docker_executable, "version", "--format", "{{.Client.Version}}|{{.Server.Version}}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    client, server = completed.stdout.strip().split("|", 1)
    return client, server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--docker-executable", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    manifest = load_json(args.manifest.resolve())
    case_ids = {str(item["case_id"]) for item in manifest["tasks"]}
    client, server = docker_versions(args.docker_executable)
    snapshot = build_snapshot(
        task_root=args.task_root.resolve(),
        case_ids=case_ids,
        docker_executable=args.docker_executable,
        docker_client_version=client,
        docker_server_version=server,
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"{output} ({snapshot['resolved_count']}/{snapshot['reference_count']} references resolved; "
        "no pull performed)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
