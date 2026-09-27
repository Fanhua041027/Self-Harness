from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parents[1] / "eval" / "scripts" / "audit_invalid_cases.py"
    spec = importlib.util.spec_from_file_location("self_harness_invalid_audit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_result(path: Path, cases: list[dict]) -> None:
    payload = {
        "splits": {
            "train": [
                {"repeat": 1, "case_results": cases},
            ]
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_build_audit_is_hash_bound_sorted_and_strict(tmp_path: Path) -> None:
    audit = load_module()
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    write_result(
        baseline,
        [
            {"case_id": "z", "passed": False, "status": "invalid", "infrastructure_error": "agent timeout"},
            {"case_id": "a", "passed": True, "status": "passed", "reward": 1.0},
        ],
    )
    write_result(
        candidate,
        [{"case_id": "b", "passed": False, "status": "failed", "reward": None}],
    )

    result = audit.build_audit([("baseline", baseline.resolve()), ("candidate", candidate.resolve())])

    assert result["format"] == audit.FORMAT
    assert result["total_invalid"] == 2
    assert result["invalid_by_side"] == {"baseline": 1, "candidate": 1}
    assert result["invalid_by_side_category"] == {
        "baseline:timeout": 1,
        "candidate:missing_reward": 1,
    }
    assert result["invalid_by_split_repeat"] == {
        "baseline:train/repeat-01": 1,
        "candidate:train/repeat-01": 1,
    }
    assert [(item["side"], item["case_id"]) for item in result["cells"]] == [
        ("baseline", "z"),
        ("candidate", "b"),
    ]
    assert result["sources"]["baseline"]["source_stable"] is True
    assert result["sources"]["baseline"]["sha256"] == audit.sha256_file(baseline)


def test_audit_rejects_source_change_during_read(monkeypatch, tmp_path: Path) -> None:
    audit = load_module()
    result_path = tmp_path / "result.json"
    write_result(result_path, [])
    values = iter(("before", "after"))
    monkeypatch.setattr(audit, "sha256_file", lambda _path: next(values))

    try:
        audit.audit_result(result_path, "baseline")
    except ValueError as exc:
        assert "changed while being audited" in str(exc)
    else:
        raise AssertionError("a changing source result was accepted")


def test_audit_rejects_duplicate_source_path(tmp_path: Path) -> None:
    audit = load_module()
    result_path = tmp_path / "result.json"
    write_result(result_path, [])

    try:
        audit.build_audit([("baseline", result_path), ("candidate", result_path)])
    except ValueError as exc:
        assert "duplicate source result" in str(exc)
    else:
        raise AssertionError("one result was accepted for both experiment sides")


def test_build_audit_rechecks_all_sources_at_completion(monkeypatch, tmp_path: Path) -> None:
    audit = load_module()
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    write_result(baseline, [])
    write_result(candidate, [])
    real_sha256 = audit.sha256_file
    baseline_calls = 0

    def changing_hash(path: Path) -> str:
        nonlocal baseline_calls
        if path.resolve() == baseline.resolve():
            baseline_calls += 1
            if baseline_calls >= 3:
                return "changed"
        return real_sha256(path)

    monkeypatch.setattr(audit, "sha256_file", changing_hash)
    try:
        audit.build_audit([("baseline", baseline), ("candidate", candidate)])
    except ValueError as exc:
        assert "baseline result changed before audit completion" in str(exc)
    else:
        raise AssertionError("source changed after its local audit was accepted")


def test_atomic_writer_cleans_temporary_file_on_replace_failure(monkeypatch, tmp_path: Path) -> None:
    audit = load_module()
    output = tmp_path / "audit.json"

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(audit.os, "replace", fail_replace)
    try:
        audit.write_json_atomic(output, {"format": audit.FORMAT})
    except OSError as exc:
        assert "replace failed" in str(exc)
    else:
        raise AssertionError("replace failure was ignored")
    assert not output.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_verify_audit_artifact_rejects_tampering(tmp_path: Path) -> None:
    audit = load_module()
    result_path = tmp_path / "result.json"
    artifact_path = tmp_path / "audit.json"
    write_result(result_path, [])
    rebuilt = audit.build_audit([("baseline", result_path)])
    audit.write_json_atomic(artifact_path, rebuilt)
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["total_invalid"] = 99
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    try:
        audit.verify_audit_artifact(artifact_path, rebuilt)
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("tampered audit artifact was accepted")


def test_audit_rejects_duplicate_side(tmp_path: Path) -> None:
    audit = load_module()
    result_path = tmp_path / "result.json"
    write_result(result_path, [])

    try:
        audit.build_audit([("baseline", result_path), ("baseline", result_path)])
    except ValueError as exc:
        assert "duplicate side" in str(exc)
    else:
        raise AssertionError("duplicate source side was accepted")
