from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def load_runner_module():
    path = Path(__file__).resolve().parents[1] / "eval" / "scripts" / "run_harbor_eval.py"
    spec = importlib.util.spec_from_file_location("self_harness_run_harbor_eval", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_trial(tmp_path: Path, output: str) -> Path:
    trial = tmp_path / "trial"
    verifier = trial / "verifier"
    verifier.mkdir(parents=True)
    (verifier / "test-stdout.txt").write_text(output, encoding="utf-8")
    result = trial / "result.json"
    result.write_text("{}", encoding="utf-8")
    return result


def test_detects_dependency_network_failure_before_tests(tmp_path: Path) -> None:
    runner = load_runner_module()
    result = make_trial(
        tmp_path,
        "E: Failed to fetch http://deb.debian.org/debian bookworm\n"
        "E: Unable to locate package curl\n"
        "/tests/test.sh: curl: command not found\n",
    )

    error = runner.detect_verifier_infrastructure_error(trial_result_path=result, reward=0.0)

    assert error == "failed to fetch http://deb.debian.org"


def test_does_not_reclassify_real_pytest_failure(tmp_path: Path) -> None:
    runner = load_runner_module()
    result = make_trial(
        tmp_path,
        "============================= test session starts ==============================\n"
        "collected 2 items\n"
        "FAILED tests/test_outputs.py::test_about_file\n",
    )

    error = runner.detect_verifier_infrastructure_error(trial_result_path=result, reward=0.0)

    assert error is None


def test_nonzero_reward_is_never_infrastructure_failure(tmp_path: Path) -> None:
    runner = load_runner_module()
    result = make_trial(tmp_path, "curl: command not found\n")

    error = runner.detect_verifier_infrastructure_error(trial_result_path=result, reward=1.0)

    assert error is None


def test_detects_structured_agent_timeout(tmp_path: Path) -> None:
    runner = load_runner_module()
    result = make_trial(tmp_path, "")

    error = runner.detect_verifier_infrastructure_error(
        trial_result_path=result,
        reward=0.0,
        exception_info={
            "exception_type": "AgentTimeoutError",
            "exception_message": "Agent execution timed out after 1800.0 seconds",
        },
    )

    assert error == "agent timeout: Agent execution timed out after 1800.0 seconds"


def test_detects_wrapper_invoke_timeout(tmp_path: Path) -> None:
    runner = load_runner_module()
    result = make_trial(tmp_path, "")
    agent_dir = result.parent / "agent"
    agent_dir.mkdir()
    (agent_dir / "invoke_state.json").write_text(
        json.dumps({"phase": "ainvoke_timeout", "invoke_timeout_sec": 900.0}),
        encoding="utf-8",
    )

    error = runner.detect_verifier_infrastructure_error(trial_result_path=result, reward=0.0)

    assert error == "agent invoke timeout after 900.0 seconds"


def test_harbor_case_retries_infrastructure_failure(monkeypatch, tmp_path: Path) -> None:
    runner = load_runner_module()
    task_root = tmp_path / "tasks"
    task = task_root / "sample-task"
    task.mkdir(parents=True)
    (task / "task.toml").write_text("version = '1'\n", encoding="utf-8")
    config = runner.Config(
        name="retry-test",
        runner="harbor_cli",
        model="openai:test-model",
        repeats=1,
        evals_project=tmp_path,
        harness_workspace=tmp_path,
        task_root=task_root,
        agent_import_path="example:Agent",
        environment="docker",
        agent_kwargs=(),
        infrastructure_retries=2,
        model_flag="--model",
        summary_flag="",
        pytest_args=(),
        case_concurrency=1,
        timeout_s=10,
        env={},
        cases=(),
    )
    attempts = iter(
        (
            {
                "stdout": "",
                "stderr": "",
                "returncode": 0,
                "reward": 0.0,
                "exception_info": None,
                "trace_metadata": None,
                "infrastructure_error": "failed to fetch http://deb.debian.org",
                "trial_result_path": "attempt-1/result.json",
            },
            {
                "stdout": "",
                "stderr": "",
                "returncode": 0,
                "reward": 1.0,
                "exception_info": None,
                "trace_metadata": None,
                "infrastructure_error": None,
                "trial_result_path": "attempt-2/result.json",
            },
        )
    )
    monkeypatch.setattr(runner.shutil, "which", lambda *args, **kwargs: "harbor")
    monkeypatch.setattr(runner, "run_harbor_attempt", lambda **kwargs: next(attempts))

    result = runner.run_harbor_case(
        config=config,
        case=runner.Case("sample-task", "train", "test"),
        split="train",
        repeat_index=1,
        case_index=0,
        repeat_dir=tmp_path / "repeat",
    )

    assert result["status"] == "passed"
    assert result["attempt_count"] == 2
    audit = json.loads((Path(result["artifacts_dir"]) / "attempts.json").read_text(encoding="utf-8"))
    assert [item["job_name"] for item in audit["attempts"]] == ["trial", "retry-02"]


def test_repeat_reuses_case_checkpoint(monkeypatch, tmp_path: Path) -> None:
    runner = load_runner_module()
    case = runner.Case("sample-task", "train", "test")
    config = runner.Config(
        name="checkpoint-test",
        runner="harbor_cli",
        model="openai:test-model",
        repeats=1,
        evals_project=tmp_path,
        harness_workspace=tmp_path,
        task_root=tmp_path,
        agent_import_path="example:Agent",
        environment="docker",
        agent_kwargs=(),
        infrastructure_retries=0,
        model_flag="--model",
        summary_flag="",
        pytest_args=(),
        case_concurrency=1,
        timeout_s=10,
        env={},
        cases=(case,),
    )
    repeat_dir = tmp_path / "repeat"

    def fake_run_case(**kwargs):
        artifacts = repeat_dir / "cases" / "sample-task"
        artifacts.mkdir(parents=True, exist_ok=True)
        return {
            "index": 0,
            "case_id": "sample-task",
            "split": "train",
            "stratum": "test",
            "repeat": 1,
            "passed": True,
            "status": "passed",
            "returncode": 0,
            "reward": 1.0,
            "duration_s": 1.0,
            "artifacts_dir": str(artifacts),
            "failure_message": None,
        }

    monkeypatch.setattr(runner, "run_case", fake_run_case)
    first = runner.run_repeat(
        config=config,
        split="train",
        cases=(case,),
        repeat_index=1,
        repeat_dir=repeat_dir,
        reuse_existing=False,
    )
    monkeypatch.setattr(runner, "run_case", lambda **kwargs: (_ for _ in ()).throw(AssertionError("reran case")))
    second = runner.run_repeat(
        config=config,
        split="train",
        cases=(case,),
        repeat_index=1,
        repeat_dir=repeat_dir,
        reuse_existing=True,
    )

    assert first["passed"] == second["passed"] == 1
