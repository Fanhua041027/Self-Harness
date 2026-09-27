from __future__ import annotations

from pathlib import Path

from paper import audit_container_reproducibility


def write_task(root: Path, name: str, dockerfile: str) -> None:
    task = root / name
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text("version='1'\n", encoding="utf-8")
    (task / "environment" / "Dockerfile").write_text(dockerfile, encoding="utf-8")


def test_classifies_digest_tag_latest_and_variable() -> None:
    classify = audit_container_reproducibility.classify_reference

    assert classify("ubuntu@sha256:" + "a" * 64) == "digest_pinned"
    assert classify("python:3.13-slim") == "explicit_tag"
    assert classify("ubuntu:latest") == "floating_tag"
    assert classify("ubuntu") == "floating_tag"
    assert classify("${BASE_IMAGE}") == "variable"


def test_build_audit_counts_multistage_references(tmp_path: Path) -> None:
    write_task(
        tmp_path,
        "digest-task",
        "FROM python@sha256:" + "a" * 64 + " AS build\nFROM ubuntu@sha256:" + "b" * 64 + "\n",
    )
    write_task(tmp_path, "tag-task", "FROM --platform=linux/amd64 python:3.13-slim\n")

    audit = audit_container_reproducibility.build_audit(tmp_path)

    assert audit["task_count"] == 2
    assert audit["base_reference_count"] == 3
    assert audit["pinning_counts"] == {"digest_pinned": 2, "explicit_tag": 1}
    assert audit["tasks_all_bases_digest_pinned"] == 1
