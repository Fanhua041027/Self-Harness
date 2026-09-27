from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


def load_workflow_module():
    path = Path(__file__).parents[1] / "workflow" / "scripts" / "run_self_harness_loop.py"
    spec = importlib.util.spec_from_file_location("self_harness_loop_resume", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reuse_mode_generates_missing_diagnosis(tmp_path: Path, monkeypatch) -> None:
    workflow = load_workflow_module()
    branch_paths = workflow.BranchPaths(tmp_path, "baseline")

    def write_diagnosis(_template, *, placeholders):
        output = placeholders["diagnosis"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("diagnosis", encoding="utf-8")

    monkeypatch.setattr(workflow, "run_external_template", write_diagnosis)
    result = workflow.resolve_diagnosis(
        requested=None,
        command_template="diagnose",
        branch_paths=branch_paths,
        baseline_result=tmp_path / "baseline.json",
        reuse_existing=True,
    )

    assert result.read_text(encoding="utf-8") == "diagnosis"


def test_reuse_mode_generates_missing_proposer_response(tmp_path: Path, monkeypatch) -> None:
    workflow = load_workflow_module()
    branch_paths = workflow.BranchPaths(tmp_path, "baseline")

    def write_response(_template, *, placeholders):
        output = placeholders["response"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(workflow, "run_external_template", write_response)
    result = workflow.resolve_proposer_response(
        requested=None,
        command_template="propose",
        branch_paths=branch_paths,
        reuse_existing=True,
    )

    assert result.read_text(encoding="utf-8") == "{}"


def test_windows_command_split_preserves_backslashes(monkeypatch) -> None:
    workflow = load_workflow_module()
    monkeypatch.setattr(os, "name", "nt")

    argv = workflow.split_external_command(
        '"C:\\Project Files\\venv\\python.exe" "C:\\Project Files\\script.py" --output "C:\\Run Files\\out.json"'
    )

    assert argv == [
        "C:\\Project Files\\venv\\python.exe",
        "C:\\Project Files\\script.py",
        "--output",
        "C:\\Run Files\\out.json",
    ]


def test_workflow_json_loader_accepts_bom_and_rejects_duplicate_keys(tmp_path: Path) -> None:
    workflow = load_workflow_module()
    valid = tmp_path / "valid.json"
    valid.write_text('{"format": "fixture"}', encoding="utf-8-sig")
    assert workflow.read_json(valid) == {"format": "fixture"}

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"outer": {"status": "failed", "status": "invalid"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key: status"):
        workflow.read_json(duplicate)
