from __future__ import annotations

from eval.scripts import capture_environment_lock


def fixture_lock() -> dict:
    return {
        "format": "self_harness.environment_lock.v1",
        "captured_at": "2026-08-12T00:00:00+00:00",
        "project_python": {
            "executable": "project-python",
            "executable_sha256": "a" * 64,
            "version": "Python 3.13.12",
            "package_count": 2,
            "packages": ["alpha==1", "beta==2"],
        },
        "harbor_python": {
            "executable": "harbor-python",
            "executable_sha256": "b" * 64,
            "version": "Python 3.12.0",
            "package_count": 1,
            "packages": ["harbor==0.20.0"],
        },
        "harbor_cli": {
            "executable": "harbor",
            "executable_sha256": "c" * 64,
            "version": "0.20.0",
        },
    }


def test_dependency_bundle_ignores_capture_time_and_paths() -> None:
    first = fixture_lock()
    second = fixture_lock()
    second["captured_at"] = "later"
    second["project_python"]["executable"] = "different-path"

    assert capture_environment_lock.dependency_bundle_sha256(
        first
    ) == capture_environment_lock.dependency_bundle_sha256(second)


def test_validate_lock_detects_package_mutation() -> None:
    lock = fixture_lock()
    lock["bundle_sha256"] = capture_environment_lock.dependency_bundle_sha256(lock)
    capture_environment_lock.validate_lock(lock)
    lock["project_python"]["packages"].append("changed==1")

    try:
        capture_environment_lock.validate_lock(lock)
    except ValueError as exc:
        assert "bundle hash mismatch" in str(exc)
    else:
        raise AssertionError("mutated environment lock was accepted")


def test_validate_lock_rejects_package_count_mismatch_even_if_bundle_is_recomputed() -> None:
    lock = fixture_lock()
    lock["bundle_sha256"] = capture_environment_lock.dependency_bundle_sha256(lock)
    lock["project_python"]["package_count"] += 1
    lock["bundle_sha256"] = capture_environment_lock.dependency_bundle_sha256(lock)

    try:
        capture_environment_lock.validate_lock(lock)
    except ValueError as exc:
        assert "package_count does not match" in str(exc)
    else:
        raise AssertionError("package-count-inconsistent environment lock was accepted")


def test_validate_lock_rejects_unsorted_packages_and_malformed_digest() -> None:
    lock = fixture_lock()
    lock["bundle_sha256"] = capture_environment_lock.dependency_bundle_sha256(lock)
    lock["project_python"]["packages"] = ["beta==2", "alpha==1"]
    lock["bundle_sha256"] = capture_environment_lock.dependency_bundle_sha256(lock)

    try:
        capture_environment_lock.validate_lock(lock)
    except ValueError as exc:
        assert "case-insensitively sorted" in str(exc)
    else:
        raise AssertionError("unsorted environment package list was accepted")

    lock = fixture_lock()
    lock["bundle_sha256"] = "not-a-sha256"
    try:
        capture_environment_lock.validate_lock(lock)
    except ValueError as exc:
        assert "64-character SHA-256" in str(exc)
    else:
        raise AssertionError("malformed environment digest was accepted")
