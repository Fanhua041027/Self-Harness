from __future__ import annotations

import pytest
from pathlib import Path

from eval.scripts import capture_container_resolution


def write_task(root: Path, name: str, dockerfile: str) -> None:
    task = root / name
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text("version='1'\n", encoding="utf-8")
    (task / "environment" / "Dockerfile").write_text(dockerfile, encoding="utf-8")


def test_snapshot_records_content_ids_without_pull(tmp_path) -> None:
    write_task(tmp_path, "a", "FROM python:3.13-slim\n")
    write_task(tmp_path, "b", "FROM python:3.13-slim\nFROM ubuntu:24.04\n")

    def inspector(reference: str, docker: str):
        if reference == "ubuntu:24.04":
            return None
        return {
            "Id": "sha256:" + "a" * 64,
            "RepoDigests": ["python@sha256:" + "b" * 64],
            "RepoTags": [reference],
            "Created": "2026-01-01T00:00:00Z",
            "Os": "linux",
            "Architecture": "amd64",
        }

    snapshot = capture_container_resolution.build_snapshot(
        task_root=tmp_path,
        case_ids={"a", "b"},
        docker_executable="docker",
        docker_client_version="1",
        docker_server_version="1",
        inspector=inspector,
    )

    assert snapshot["reference_count"] == 2
    assert snapshot["resolved_count"] == 1
    assert snapshot["complete"] is False
    assert snapshot["pull_performed"] is False
    capture_container_resolution.validate_snapshot(snapshot)


def test_resolution_bundle_detects_content_id_mutation(tmp_path) -> None:
    write_task(tmp_path, "a", "FROM ubuntu:24.04\n")
    snapshot = capture_container_resolution.build_snapshot(
        task_root=tmp_path,
        case_ids={"a"},
        docker_executable="docker",
        docker_client_version="1",
        docker_server_version="1",
        inspector=lambda *_: {
            "Id": "sha256:" + "a" * 64,
            "RepoDigests": [],
            "RepoTags": ["ubuntu:24.04"],
            "Created": "now",
            "Os": "linux",
            "Architecture": "amd64",
        },
    )
    snapshot["images"][0]["image_id"] = "sha256:" + "c" * 64

    try:
        capture_container_resolution.validate_snapshot(snapshot)
    except ValueError as exc:
        assert "bundle hash mismatch" in str(exc)
    else:
        raise AssertionError("mutated container resolution was accepted")


def test_snapshot_rejects_inconsistent_reference_count(tmp_path) -> None:
    write_task(tmp_path, "a", "FROM ubuntu:24.04\n")
    snapshot = capture_container_resolution.build_snapshot(
        task_root=tmp_path,
        case_ids={"a"},
        docker_executable="docker",
        docker_client_version="1",
        docker_server_version="1",
        inspector=lambda *_: None,
    )
    snapshot["reference_count"] = 0
    snapshot["bundle_sha256"] = capture_container_resolution.resolution_bundle_sha256(snapshot)

    try:
        capture_container_resolution.validate_snapshot(snapshot)
    except ValueError as exc:
        assert "reference count mismatch" in str(exc)
    else:
        raise AssertionError("inconsistent reference count was accepted")


def test_snapshot_rejects_empty_complete_evidence(tmp_path) -> None:
    snapshot = capture_container_resolution.build_snapshot(
        task_root=tmp_path,
        case_ids={"a"},
        docker_executable="docker",
        docker_client_version="1",
        docker_server_version="1",
        inspector=lambda *_: None,
    )
    snapshot["complete"] = True
    snapshot["bundle_sha256"] = capture_container_resolution.resolution_bundle_sha256(snapshot)

    try:
        capture_container_resolution.validate_snapshot(snapshot)
    except ValueError as exc:
        assert "completeness mismatch" in str(exc)
    else:
        raise AssertionError("empty complete evidence was accepted")


def test_manifest_loader_accepts_bom_and_rejects_nested_duplicate_key(tmp_path: Path) -> None:
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf{\"ok\": true}")
    assert capture_container_resolution.load_json(bom)["ok"] is True

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"outer": {"value": 1, "value": 2}}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        capture_container_resolution.load_json(duplicate)
