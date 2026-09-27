from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def load_builder_module():
    path = Path(__file__).resolve().parents[1] / "eval" / "scripts" / "build_sealed_split.py"
    spec = importlib.util.spec_from_file_location("self_harness_sealed_builder", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_task(path: Path, *, media: bool = False) -> None:
    path.mkdir(parents=True)
    (path / "task.toml").write_text(
        """version = "1.0"
[metadata]
category = "fixture"
difficulty = "medium"
""",
        encoding="utf-8",
    )
    (path / "instruction.md").write_text("fixture", encoding="utf-8")
    if media:
        (path / "input.png").write_bytes(b"png")


def make_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    runs_root = tmp_path / "runs"
    task_root = runs_root / "terminal-bench-2-reliable-v2"
    write_task(task_root / "clean-task")
    for index in range(21):
        write_task(task_root / f"sealed-{index:02d}")
    write_task(task_root / "media-task", media=True)
    config = tmp_path / "eval" / "configs" / "clean.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """[eval]
name = "fixture"
[[cases]]
case_id = "clean-task"
split = "train"
stratum = "fixture"
""",
        encoding="utf-8",
    )
    return runs_root, task_root, config


def test_build_and_verify_frozen_sealed_split(tmp_path: Path) -> None:
    builder = load_builder_module()
    runs_root, task_root, clean_config = make_fixture(tmp_path)
    output = tmp_path / "configs" / "splits" / "sealed.json"
    config_output = tmp_path / "eval" / "configs" / "sealed.toml"

    manifest = builder.build_manifest(
        task_root=task_root,
        clean_config=clean_config,
        runs_root=runs_root,
    )
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    output.parent.mkdir(parents=True)
    output.write_bytes(manifest_bytes)
    output.with_suffix(".json.sha256").write_text(
        builder.sha256_bytes(manifest_bytes) + "\n", encoding="ascii"
    )
    config_output.parent.mkdir(parents=True, exist_ok=True)
    config_output.write_bytes(
        builder.render_config([item["case_id"] for item in manifest["tasks"]]).encode("utf-8")
    )

    builder.verify(manifest_path=output, config_path=config_output)
    assert manifest["task_count"] == 21
    assert {item["case_id"] for item in manifest["excluded_unused_tasks"]} == {"media-task"}
    assert "clean-task" not in {item["case_id"] for item in manifest["tasks"]}


def test_verify_detects_task_mutation(tmp_path: Path) -> None:
    builder = load_builder_module()
    runs_root, task_root, clean_config = make_fixture(tmp_path)
    output = tmp_path / "configs" / "splits" / "sealed.json"
    config_output = tmp_path / "eval" / "configs" / "sealed.toml"
    manifest = builder.build_manifest(task_root=task_root, clean_config=clean_config, runs_root=runs_root)
    data = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    output.parent.mkdir(parents=True)
    output.write_bytes(data)
    output.with_suffix(".json.sha256").write_text(builder.sha256_bytes(data) + "\n", encoding="ascii")
    config_output.parent.mkdir(parents=True, exist_ok=True)
    config_output.write_bytes(
        builder.render_config([item["case_id"] for item in manifest["tasks"]]).encode("utf-8")
    )
    (task_root / "sealed-00" / "instruction.md").write_text("changed", encoding="utf-8")

    with pytest.raises(ValueError, match="sealed task hash mismatch: sealed-00"):
        builder.verify(manifest_path=output, config_path=config_output)


def test_verify_detects_eval_config_mutation(tmp_path: Path) -> None:
    builder = load_builder_module()
    runs_root, task_root, clean_config = make_fixture(tmp_path)
    output = tmp_path / "configs" / "splits" / "sealed.json"
    config_output = tmp_path / "eval" / "configs" / "sealed.toml"
    manifest = builder.build_manifest(
        task_root=task_root, clean_config=clean_config, runs_root=runs_root
    )
    data = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    output.parent.mkdir(parents=True)
    output.write_bytes(data)
    output.with_suffix(".json.sha256").write_text(
        builder.sha256_bytes(data) + "\n", encoding="ascii"
    )
    config_output.parent.mkdir(parents=True, exist_ok=True)
    canonical = builder.render_config([item["case_id"] for item in manifest["tasks"]])
    config_output.write_text(canonical.replace("repeats = 2", "repeats = 3"), encoding="utf-8")

    with pytest.raises(ValueError, match="config bytes differ"):
        builder.verify(manifest_path=output, config_path=config_output)


def test_build_rejects_prior_evaluation_result(tmp_path: Path) -> None:
    builder = load_builder_module()
    runs_root, task_root, clean_config = make_fixture(tmp_path)
    prior = runs_root / "prior" / "result.json"
    prior.parent.mkdir()
    prior.write_text(json.dumps({"case_id": "sealed-03"}), encoding="utf-8")

    with pytest.raises(ValueError, match="sealed candidates already appear"):
        builder.build_manifest(task_root=task_root, clean_config=clean_config, runs_root=runs_root)
