#!/usr/bin/env python3
"""捕获项目与 Harbor Python 环境的稳定依赖 bundle。"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FORMAT = "self_harness.environment_lock.v1"
_SHA256_HEX_LENGTH = 64


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_output(argv: list[str]) -> str:
    completed = subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


def runtime_snapshot(python: Path) -> dict[str, Any]:
    packages = sorted(
        {
            line.strip()
            for line in command_output(
                [str(python), "-m", "pip", "freeze", "--all", "--disable-pip-version-check"]
            ).splitlines()
            if line.strip()
        },
        key=str.casefold,
    )
    return {
        "executable": str(python.resolve()),
        "executable_sha256": sha256_file(python.resolve()),
        "version": command_output([str(python), "--version"]),
        "package_count": len(packages),
        "packages": packages,
    }


def stable_payload(lock: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_python": {
            key: lock["project_python"][key]
            for key in ("executable_sha256", "version", "packages")
        },
        "harbor_python": {
            key: lock["harbor_python"][key]
            for key in ("executable_sha256", "version", "packages")
        },
        "harbor_cli": {
            key: lock["harbor_cli"][key]
            for key in ("executable_sha256", "version")
        },
    }


def dependency_bundle_sha256(lock: dict[str, Any]) -> str:
    canonical = json.dumps(
        stable_payload(lock), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _require_nonblank_string(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"environment lock schema invalid: {label} must be a nonblank string")


def _require_sha256(value: Any, label: str) -> None:
    _require_nonblank_string(value, label)
    if len(value) != _SHA256_HEX_LENGTH or any(char not in "0123456789abcdef" for char in value.lower()):
        raise ValueError(f"environment lock schema invalid: {label} must be a 64-character SHA-256 hex digest")


def _validate_runtime_snapshot(snapshot: Any, label: str, *, packages_required: bool) -> None:
    if not isinstance(snapshot, dict):
        raise ValueError(f"environment lock schema invalid: {label} must be an object")
    _require_nonblank_string(snapshot.get("executable"), f"{label}.executable")
    _require_sha256(snapshot.get("executable_sha256"), f"{label}.executable_sha256")
    _require_nonblank_string(snapshot.get("version"), f"{label}.version")
    if not packages_required:
        return
    package_count = snapshot.get("package_count")
    if isinstance(package_count, bool) or not isinstance(package_count, int) or package_count < 0:
        raise ValueError(f"environment lock schema invalid: {label}.package_count must be a non-negative integer")
    packages = snapshot.get("packages")
    if not isinstance(packages, list) or any(not isinstance(item, str) or not item.strip() for item in packages):
        raise ValueError(f"environment lock schema invalid: {label}.packages must be a list of nonblank strings")
    if len(packages) != package_count:
        raise ValueError(f"environment lock schema invalid: {label}.package_count does not match packages length")
    if packages != sorted(packages, key=str.casefold):
        raise ValueError(f"environment lock schema invalid: {label}.packages must be case-insensitively sorted")
    if len(set(packages)) != len(packages):
        raise ValueError(f"environment lock schema invalid: {label}.packages must not contain duplicates")


def build_lock(
    *, project_python: Path, harbor_python: Path, harbor_executable: Path
) -> dict[str, Any]:
    lock = {
        "format": FORMAT,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "project_python": runtime_snapshot(project_python),
        "harbor_python": runtime_snapshot(harbor_python),
        "harbor_cli": {
            "executable": str(harbor_executable.resolve()),
            "executable_sha256": sha256_file(harbor_executable.resolve()),
            "version": command_output([str(harbor_executable), "--version"]),
        },
    }
    lock["bundle_sha256"] = dependency_bundle_sha256(lock)
    return lock


def validate_lock(lock: dict[str, Any]) -> None:
    if not isinstance(lock, dict) or lock.get("format") != FORMAT:
        raise ValueError("unexpected environment lock format")
    _require_sha256(lock.get("bundle_sha256"), "bundle_sha256")
    try:
        bundle_matches = dependency_bundle_sha256(lock) == lock.get("bundle_sha256")
    except (KeyError, TypeError):
        raise ValueError("environment lock schema invalid: required bundle fields are missing") from None
    if not bundle_matches:
        raise ValueError("environment dependency bundle hash mismatch")
    captured_at = lock.get("captured_at")
    _require_nonblank_string(captured_at, "captured_at")
    try:
        parsed_capture_time = datetime.fromisoformat(captured_at)
    except ValueError:
        raise ValueError("environment lock schema invalid: captured_at must be ISO-8601") from None
    if parsed_capture_time.tzinfo is None or parsed_capture_time.utcoffset() is None:
        raise ValueError("environment lock schema invalid: captured_at must include a timezone")
    _validate_runtime_snapshot(lock.get("project_python"), "project_python", packages_required=True)
    _validate_runtime_snapshot(lock.get("harbor_python"), "harbor_python", packages_required=True)
    _validate_runtime_snapshot(lock.get("harbor_cli"), "harbor_cli", packages_required=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-python", required=True, type=Path)
    parser.add_argument("--harbor-python", required=True, type=Path)
    parser.add_argument("--harbor-executable", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    lock = build_lock(
        project_python=args.project_python.resolve(),
        harbor_python=args.harbor_python.resolve(),
        harbor_executable=args.harbor_executable.resolve(),
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
