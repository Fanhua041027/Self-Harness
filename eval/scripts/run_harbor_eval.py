#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VALIDITY_DIR = Path(__file__).resolve().parent
if str(VALIDITY_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDITY_DIR))

from result_validity import load_json_object

ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")
DEFAULT_REPEATS = 2
VERIFIER_INFRASTRUCTURE_MARKERS = (
    "failed to fetch http://deb.debian.org",
    "unable to locate package curl",
    "curl: command not found",
    "uvx: command not found",
    "could not resolve host",
    "temporary failure resolving",
)


@dataclass(frozen=True)
class Case:
    case_id: str
    split: str
    stratum: str

    def render(self, *, model: str) -> str:
        return self.case_id.format(model=model)


@dataclass(frozen=True)
class Config:
    name: str
    runner: str
    model: str
    repeats: int
    evals_project: Path
    harness_workspace: Path
    task_root: Path | None
    agent_import_path: str
    environment: str
    agent_kwargs: tuple[str, ...]
    infrastructure_retries: int
    model_flag: str
    summary_flag: str
    pytest_args: tuple[str, ...]
    case_concurrency: int
    timeout_s: float | None
    env: dict[str, str]
    cases: tuple[Case, ...]
    agent_timeout_multiplier: float | None = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Harbor-backed eval stage.")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model", help="Override [eval].model")
    parser.add_argument("--repeats", type=int, help="Override [eval].repeats")
    parser.add_argument("--split", action="append", help="Run only this split. May be repeated.")
    parser.add_argument("--case-concurrency", type=int, help="Override [eval].case_concurrency")
    parser.add_argument("--reuse-existing", action="store_true")
    parser.add_argument(
        "--run-identity",
        help="Frozen experiment identity recorded in outputs and required for safe checkpoint reuse.",
    )
    parser.add_argument(
        "--expected-config-sha256",
        help="Reject execution unless the raw TOML configuration has this SHA256.",
    )
    args = parser.parse_args(argv)

    config_path = args.config.resolve()
    config_sha256 = sha256_file(config_path)
    if (
        args.expected_config_sha256
        and config_sha256 != args.expected_config_sha256.lower()
    ):
        raise RuntimeError(
            "evaluation config SHA256 mismatch; refusing to run a changed experiment config"
        )
    config = load_config(
        config_path,
        model_override=args.model,
        repeats_override=args.repeats,
        concurrency_override=args.case_concurrency,
    )
    selected_splits = set(args.split or sorted({case.split for case in config.cases}))
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    establish_run_identity(
        output_dir=output_dir,
        run_identity=args.run_identity,
        reuse_existing=args.reuse_existing,
        config_sha256=config_sha256,
    )
    resolved_config = config_to_json(config)
    resolved_config["run_identity"] = args.run_identity
    resolved_config["config_sha256"] = config_sha256
    write_json(output_dir / "config.resolved.json", resolved_config)

    split_results = []
    for split in sorted(selected_splits):
        cases = tuple(case for case in config.cases if case.split == split)
        if not cases:
            raise SystemExit(f"no cases configured for split {split!r}")
        for repeat_index in range(1, config.repeats + 1):
            repeat_dir = output_dir / "splits" / split / f"repeat-{repeat_index:02d}"
            if args.reuse_existing and (repeat_dir / "result.json").exists():
                result = validate_repeat_result(
                    load_json_object(repeat_dir / "result.json"),
                    config=config,
                    split=split,
                    cases=cases,
                    repeat_index=repeat_index,
                    repeat_dir=repeat_dir,
                )
            else:
                result = run_repeat(
                    config=config,
                    split=split,
                    cases=cases,
                    repeat_index=repeat_index,
                    repeat_dir=repeat_dir,
                    reuse_existing=args.reuse_existing,
                )
            split_results.append(result)

    aggregate = aggregate_results(
        config=config,
        split_results=split_results,
        run_identity=args.run_identity,
        config_sha256=config_sha256,
    )
    write_json(output_dir / "result.json", aggregate)
    print(
        f"wrote {output_dir / 'result.json'} "
        f"({aggregate['passed']}/{aggregate['total']} passed over {config.repeats} repeats)"
    )
    return 0


def load_config(
    path: Path,
    *,
    model_override: str | None,
    repeats_override: int | None,
    concurrency_override: int | None,
) -> Config:
    config_path = path.resolve()
    tomllib = import_toml_reader()
    raw = tomllib.loads(config_path.read_text())
    eval_config = dict(raw.get("eval", {}))
    env_config = {
        str(key): expand_env(str(value))
        for key, value in dict(raw.get("env", {})).items()
        if str(value).strip()
    }
    base_dir = config_path.parent

    model = model_override or expand_env(str(eval_config.get("model", ""))).strip()
    if not model:
        raise ValueError("[eval].model or --model is required")

    repeats = repeats_override if repeats_override is not None else int(eval_config.get("repeats", DEFAULT_REPEATS))
    if repeats < 1:
        raise ValueError("repeats must be at least 1")

    case_concurrency = (
        concurrency_override
        if concurrency_override is not None
        else int(eval_config.get("case_concurrency", 1) or 1)
    )
    if case_concurrency < 1:
        raise ValueError("case_concurrency must be at least 1")

    cases = tuple(
        Case(
            case_id=str(item["case_id"]),
            split=str(item.get("split", "train")),
            stratum=str(item.get("stratum", "default")),
        )
        for item in raw.get("cases", [])
    )
    if not cases:
        raise ValueError("config must define at least one [[cases]] entry")

    timeout_raw = eval_config.get("timeout_s")
    timeout_s = float(timeout_raw) if timeout_raw is not None else None

    multiplier_raw = eval_config.get("agent_timeout_multiplier")
    agent_timeout_multiplier = float(multiplier_raw) if multiplier_raw is not None else None
    if agent_timeout_multiplier is not None and agent_timeout_multiplier <= 0:
        raise ValueError("[eval].agent_timeout_multiplier must be > 0")

    runner = str(eval_config.get("runner", "pytest"))
    if runner not in {"pytest", "harbor_cli"}:
        raise ValueError("[eval].runner must be 'pytest' or 'harbor_cli'")
    task_root_raw = eval_config.get("task_root")
    task_root = resolve_path(base_dir, str(task_root_raw)) if task_root_raw else None
    if runner == "harbor_cli" and task_root is None:
        raise ValueError("[eval].task_root is required for harbor_cli runner")
    infrastructure_retries = int(eval_config.get("infrastructure_retries", 0) or 0)
    if infrastructure_retries < 0:
        raise ValueError("[eval].infrastructure_retries must be >= 0")

    return Config(
        name=str(eval_config.get("name", "harbor-eval")),
        runner=runner,
        model=model,
        repeats=repeats,
        evals_project=resolve_path(base_dir, str(eval_config.get("evals_project", "."))),
        harness_workspace=resolve_path(base_dir, str(eval_config["harness_workspace"])),
        task_root=task_root,
        agent_import_path=str(eval_config["agent_import_path"]),
        environment=str(eval_config.get("environment", "docker")),
        agent_kwargs=tuple(str(item) for item in eval_config.get("agent_kwargs", [])),
        infrastructure_retries=infrastructure_retries,
        model_flag=str(eval_config.get("model_flag", "--model")),
        summary_flag=str(eval_config.get("summary_flag", "--evals-report-file")),
        pytest_args=tuple(str(item) for item in eval_config.get("pytest_args", ["-q"])),
        case_concurrency=case_concurrency,
        timeout_s=timeout_s,
        agent_timeout_multiplier=agent_timeout_multiplier,
        env=env_config,
        cases=cases,
    )


def import_toml_reader() -> Any:
    try:
        import tomllib

        return tomllib
    except ModuleNotFoundError:
        try:
            import tomli

            return tomli
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "TOML config parsing requires Python 3.11+ or the 'tomli' package on Python 3.10."
            ) from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def establish_run_identity(
    *,
    output_dir: Path,
    run_identity: str | None,
    reuse_existing: bool,
    config_sha256: str | None = None,
) -> None:
    """绑定输出目录与冻结实验身份，阻止无法证明同源的 checkpoint 复用。"""
    marker_path = output_dir / "run.identity.json"
    if marker_path.exists():
        marker = load_json_object(marker_path)
        recorded = marker.get("run_identity")
        recorded_config = marker.get("config_sha256")
        if recorded != run_identity or recorded_config != config_sha256:
            raise RuntimeError(
                "run identity mismatch; refusing to mix or reuse experiment outputs: "
                f"recorded={recorded!r}, requested={run_identity!r}, "
                f"recorded_config={recorded_config!r}, requested_config={config_sha256!r}"
            )
        return

    existing_checkpoints = [
        path
        for path in output_dir.rglob("result.json")
        if "rerun_history" not in path.parts
    ]
    if reuse_existing and run_identity is not None and existing_checkpoints:
        raise RuntimeError(
            "cannot reuse checkpoints without a run identity marker; start from a clean output "
            "directory or restore the original run.identity.json"
        )
    if run_identity is not None:
        write_json(
            marker_path,
            {
                "format": "self_harness.run_identity.v2",
                "run_identity": run_identity,
                "config_sha256": config_sha256,
            },
        )


def run_repeat(
    *,
    config: Config,
    split: str,
    cases: tuple[Case, ...],
    repeat_index: int,
    repeat_dir: Path,
    reuse_existing: bool = False,
) -> dict[str, Any]:
    repeat_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    if not cases:
        raise ValueError(f"split {split!r} has no cases")
    max_workers = min(config.case_concurrency, len(cases))
    outcomes = []
    pending_cases = []
    for index, case in enumerate(cases):
        rendered = expand_env(case.render(model=config.model))
        checkpoint = case_directory(repeat_dir, rendered) / "result.json"
        if reuse_existing and checkpoint.exists():
            outcomes.append(
                validate_case_checkpoint(
                    load_json_object(checkpoint),
                    case_id=rendered,
                    split=split,
                    repeat=repeat_index,
                )
            )
        else:
            pending_cases.append((index, case))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                run_case,
                config=config,
                case=case,
                split=split,
                repeat_index=repeat_index,
                case_index=index,
                repeat_dir=repeat_dir,
            )
            for index, case in pending_cases
        ]
        for future in concurrent.futures.as_completed(futures):
            outcome = future.result()
            outcomes.append(outcome)
            write_json(Path(outcome["artifacts_dir"]) / "result.json", outcome)
    outcomes.sort(key=lambda item: item["index"])
    passed = sum(1 for outcome in outcomes if outcome["passed"])
    result = {
        "split": split,
        "repeat": repeat_index,
        "model": config.model,
        "passed": passed,
        "total": len(outcomes),
        "correctness": passed / len(outcomes) if outcomes else 0.0,
        "duration_s": round(time.time() - started_at, 3),
        "case_results": outcomes,
    }
    write_json(repeat_dir / "result.json", result)
    return result


def run_case(
    *,
    config: Config,
    case: Case,
    split: str,
    repeat_index: int,
    case_index: int,
    repeat_dir: Path,
) -> dict[str, Any]:
    if config.runner == "harbor_cli":
        return run_harbor_case(
            config=config,
            case=case,
            split=split,
            repeat_index=repeat_index,
            case_index=case_index,
            repeat_dir=repeat_dir,
        )

    rendered = expand_env(case.render(model=config.model))
    case_dir = case_directory(repeat_dir, rendered)
    case_dir.mkdir(parents=True, exist_ok=True)
    summary_path = case_dir / "summary.json"
    junit_path = case_dir / "junit.xml"
    command = build_pytest_command(config=config, rendered_case=rendered, summary_path=summary_path, junit_path=junit_path)
    env = build_env(config=config, split=split, repeat_index=repeat_index)
    write_json(
        case_dir / "command.json",
        {
            "argv": command,
            "shell": shlex.join(command),
            "cwd": str(config.evals_project),
            "env_subset": {
                "PYTHONPATH": env.get("PYTHONPATH", ""),
                "SELF_HARNESS_AGENT_IMPORT_PATH": env.get("SELF_HARNESS_AGENT_IMPORT_PATH", ""),
                "SELF_HARNESS_EVAL_SPLIT": split,
                "SELF_HARNESS_EVAL_REPEAT": str(repeat_index),
            },
            "timeout_s": config.timeout_s,
        },
    )
    started_at = time.time()
    try:
        completed = subprocess.run(
            command,
            cwd=config.evals_project,
            env=env,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout_s,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        returncode = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = normalize_output(exc.stdout)
        stderr = normalize_output(exc.stderr) + f"\nTIMEOUT after {config.timeout_s}s\n"
        returncode = 124
    (case_dir / "stdout.log").write_text(stdout, encoding="utf-8")
    (case_dir / "stderr.log").write_text(stderr, encoding="utf-8")

    junit_status = parse_junit_status(junit_path) if junit_path.exists() else None
    summary_payload = read_json_if_exists(summary_path)
    trace_metadata = extract_local_trace_metadata(summary_payload)
    passed = bool(junit_status and junit_status["passed"] and returncode == 0)
    failure_message = None
    if not passed:
        failure_message = (
            (junit_status or {}).get("failure_message")
            or stderr.strip()
            or stdout.strip()
            or f"pytest returncode={returncode}"
        )
    outcome = {
        "index": case_index,
        "case_id": rendered,
        "case_id_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        "split": split,
        "stratum": case.stratum,
        "repeat": repeat_index,
        "passed": passed,
        "status": "passed" if passed else "failed",
        "returncode": returncode,
        "duration_s": round(time.time() - started_at, 3),
        "artifacts_dir": str(case_dir),
        "failure_message": failure_message,
    }
    if trace_metadata:
        outcome["messages_path"] = trace_metadata["messages_path"]
        outcome["trace_metadata"] = trace_metadata
    return outcome


def run_harbor_case(
    *,
    config: Config,
    case: Case,
    split: str,
    repeat_index: int,
    case_index: int,
    repeat_dir: Path,
) -> dict[str, Any]:
    if config.task_root is None:
        raise ValueError("harbor_cli runner requires task_root")
    rendered = expand_env(case.render(model=config.model))
    task_name = rendered.replace("_", "-")
    task_path = config.task_root / task_name
    if not (task_path / "task.toml").is_file():
        raise FileNotFoundError(f"Harbor task not found: {task_path}")

    case_dir = case_directory(repeat_dir, rendered)
    jobs_dir = case_dir / "harbor"
    case_dir.mkdir(parents=True, exist_ok=True)
    harbor_executable = shutil.which("harbor", path=str(Path(sys.executable).parent))
    if harbor_executable is None:
        raise RuntimeError(f"harbor executable not found beside {sys.executable}")
    env = build_env(config=config, split=split, repeat_index=repeat_index)
    env["PYTHONUTF8"] = "1"
    started_at = time.time()
    attempt_records = []
    final = None
    for attempt_index in range(1, config.infrastructure_retries + 2):
        preferred_job_name = "trial" if attempt_index == 1 else f"retry-{attempt_index:02d}"
        job_name = available_harbor_job_name(jobs_dir, preferred_job_name)
        command = build_harbor_command(
            harbor_executable=harbor_executable,
            task_path=task_path,
            config=config,
            jobs_dir=jobs_dir,
            job_name=job_name,
        )
        write_json(
            case_dir / f"command-attempt-{attempt_index:02d}.json",
            {
                "argv": command,
                "shell": shlex.join(command),
                "cwd": str(config.evals_project),
                "env_subset": {
                    "PYTHONPATH": env.get("PYTHONPATH", ""),
                    "SELF_HARNESS_AGENT_IMPORT_PATH": env.get("SELF_HARNESS_AGENT_IMPORT_PATH", ""),
                    "SELF_HARNESS_EVAL_SPLIT": split,
                    "SELF_HARNESS_EVAL_REPEAT": str(repeat_index),
                },
                "timeout_s": config.timeout_s,
            },
        )
        attempt = run_harbor_attempt(
            command=command,
            cwd=config.evals_project,
            env=env,
            timeout_s=config.timeout_s,
            job_root=jobs_dir / job_name,
        )
        (case_dir / f"stdout-attempt-{attempt_index:02d}.log").write_text(
            attempt["stdout"], encoding="utf-8"
        )
        (case_dir / f"stderr-attempt-{attempt_index:02d}.log").write_text(
            attempt["stderr"], encoding="utf-8"
        )
        attempt_records.append(
            {
                "attempt": attempt_index,
                "job_name": job_name,
                "returncode": attempt["returncode"],
                "reward": attempt["reward"],
                "infrastructure_error": attempt["infrastructure_error"],
                "trial_result_path": attempt["trial_result_path"],
            }
        )
        final = attempt
        if attempt["infrastructure_error"] is None:
            break

    if final is None:
        raise RuntimeError("Harbor attempt loop did not run")
    write_json(case_dir / "attempts.json", {"attempts": attempt_records})
    stdout = final["stdout"]
    stderr = final["stderr"]
    returncode = final["returncode"]
    (case_dir / "stdout.log").write_text(stdout, encoding="utf-8")
    (case_dir / "stderr.log").write_text(stderr, encoding="utf-8")
    reward = final["reward"]
    exception_info = final["exception_info"]
    trace_metadata = final["trace_metadata"]
    infrastructure_error = final["infrastructure_error"]
    passed = returncode == 0 and exception_info is None and isinstance(reward, (int, float)) and reward > 0
    failure_message = None
    if not passed:
        failure_message = (
            f"Verifier infrastructure error: {infrastructure_error}"
            if infrastructure_error
            else json.dumps(exception_info, sort_keys=True)
            if exception_info
            else f"Harbor reward={reward!r}, returncode={returncode}"
        )
    outcome = {
        "index": case_index,
        "case_id": rendered,
        "case_id_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        "split": split,
        "stratum": case.stratum,
        "repeat": repeat_index,
        "passed": passed,
        "status": "passed" if passed else "invalid" if infrastructure_error else "failed",
        "returncode": returncode,
        "reward": reward,
        "attempt_count": len(attempt_records),
        "infrastructure_error": infrastructure_error,
        "duration_s": round(time.time() - started_at, 3),
        "artifacts_dir": str(case_dir),
        "failure_message": failure_message,
    }
    if trace_metadata:
        outcome["messages_path"] = trace_metadata["messages_path"]
        outcome["trace_metadata"] = trace_metadata
    return outcome


def available_harbor_job_name(jobs_dir: Path, preferred: str) -> str:
    if not (jobs_dir / preferred).exists():
        return preferred
    suffix = 2
    while (jobs_dir / f"{preferred}-resume-{suffix:02d}").exists():
        suffix += 1
    return f"{preferred}-resume-{suffix:02d}"


def build_harbor_command(
    *,
    harbor_executable: str,
    task_path: Path,
    config: Config,
    jobs_dir: Path,
    job_name: str,
) -> list[str]:
    command = [
        harbor_executable,
        "run",
        "--path",
        str(task_path),
        "--agent",
        config.agent_import_path,
        "--model",
        config.model,
        "--env",
        config.environment,
        "--n-concurrent",
        "1",
        "--n-attempts",
        "1",
        "--job-name",
        job_name,
        "--jobs-dir",
        str(jobs_dir),
        "--yes",
        "--quiet",
    ]
    for item in config.agent_kwargs:
        command.extend(["--agent-kwarg", item])
    if config.agent_timeout_multiplier is not None:
        command.extend(["--agent-timeout-multiplier", str(config.agent_timeout_multiplier)])
    return command


def run_harbor_attempt(
    *,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_s: float | None,
    job_root: Path,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        returncode = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = normalize_output(exc.stdout)
        stderr = normalize_output(exc.stderr) + f"\nTIMEOUT after {timeout_s}s\n"
        returncode = 124

    trial_paths = sorted(job_root.glob("*/result.json")) if job_root.exists() else []
    trial_result_path = trial_paths[0] if len(trial_paths) == 1 else None
    trial_result = read_json_if_exists(trial_result_path) if trial_result_path is not None else None
    reward = None
    exception_info = None
    trace_metadata = None
    if isinstance(trial_result, dict):
        verifier_result = trial_result.get("verifier_result")
        if isinstance(verifier_result, dict):
            rewards = verifier_result.get("rewards")
            if isinstance(rewards, dict):
                reward = rewards.get("reward")
        exception_info = trial_result.get("exception_info")
        trace_metadata = normalize_trace_metadata(trial_result)
    infrastructure_error = detect_verifier_infrastructure_error(
        trial_result_path=trial_result_path,
        reward=reward,
        exception_info=exception_info,
    )
    return {
        "stdout": stdout,
        "stderr": stderr,
        "returncode": returncode,
        "reward": reward,
        "exception_info": exception_info,
        "trace_metadata": trace_metadata,
        "infrastructure_error": infrastructure_error,
        "trial_result_path": str(trial_result_path) if trial_result_path is not None else None,
    }


def detect_verifier_infrastructure_error(
    *,
    trial_result_path: Path | None,
    reward: Any,
    exception_info: Any = None,
) -> str | None:
    if isinstance(exception_info, dict):
        exception_type = str(exception_info.get("exception_type") or "unknown exception")
        message = str(exception_info.get("exception_message") or exception_type)
        if exception_type == "RuntimeError" and "Docker compose command failed" in message:
            return "docker compose command failed"
        if exception_type == "AgentTimeoutError":
            return f"agent timeout: {message}"
        return f"harbor exception: {exception_type}: {message}"
    if trial_result_path is None:
        return "harbor did not produce a trial result"
    if reward is None:
        return "harbor trial has no verifier reward"
    if isinstance(reward, bool) or not isinstance(reward, (int, float)):
        return f"harbor trial has non-numeric verifier reward: {reward!r}"
    if reward > 0:
        return None
    invoke_state = read_json_if_exists(trial_result_path.parent / "agent" / "invoke_state.json")
    if isinstance(invoke_state, dict) and invoke_state.get("phase") == "ainvoke_timeout":
        timeout = invoke_state.get("invoke_timeout_sec")
        return f"agent invoke timeout after {timeout} seconds"
    verifier_dir = trial_result_path.parent / "verifier"
    if not verifier_dir.exists():
        return None
    output = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(verifier_dir.glob("test-*.txt"))
    ).lower()
    if "test session starts" in output or re.search(r"\bcollected\s+\d+\s+items?\b", output):
        return None
    for marker in VERIFIER_INFRASTRUCTURE_MARKERS:
        if marker in output:
            return marker
    return None


def build_pytest_command(config: Config, *, rendered_case: str, summary_path: Path, junit_path: Path) -> list[str]:
    command = ["uv", "run", "--project", str(config.evals_project), "pytest"]
    if config.model_flag:
        command.extend([config.model_flag, config.model])
    if config.summary_flag:
        command.extend([config.summary_flag, str(summary_path)])
    command.extend(["--junitxml", str(junit_path)])
    command.extend(config.pytest_args)
    command.append(rendered_case)
    return command


def build_env(config: Config, *, split: str, repeat_index: int) -> dict[str, str]:
    env = os.environ.copy()
    env.update(config.env)
    env["SELF_HARNESS_AGENT_IMPORT_PATH"] = config.agent_import_path
    env["SELF_HARNESS_EVAL_SPLIT"] = split
    env["SELF_HARNESS_EVAL_REPEAT"] = str(repeat_index)
    pythonpath_parts = [
        str(config.harness_workspace),
        str(config.evals_project.parent),
        env.get("PYTHONPATH", ""),
    ]
    env["PYTHONPATH"] = os.pathsep.join(part for part in pythonpath_parts if part)
    return env


def parse_junit_status(junit_path: Path) -> dict[str, Any]:
    root = ET.parse(junit_path).getroot()
    testcases = root.findall(".//testcase")
    failures = root.findall(".//failure")
    errors = root.findall(".//error")
    skipped = root.findall(".//skipped")
    messages = []
    for node in [*failures, *errors]:
        message = node.attrib.get("message") or (node.text or "")
        if message.strip():
            messages.append(message.strip())
    return {
        "tests": len(testcases),
        "failures": len(failures),
        "errors": len(errors),
        "skipped": len(skipped),
        "passed": bool(testcases) and not failures and not errors and not skipped,
        "failure_message": "\n".join(messages) if messages else None,
    }


def read_json_if_exists(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return load_json_object(path)
    except json.JSONDecodeError:
        return None


def extract_local_trace_metadata(payload: Any) -> dict[str, Any] | None:
    metadata = find_local_trace_metadata(payload)
    if not metadata:
        return None
    messages_path = local_existing_path(metadata.get("messages_path"))
    if messages_path is None:
        return None
    result = dict(metadata)
    result["messages_path"] = str(messages_path)
    events_path = local_existing_path(result.get("events_path"))
    if events_path is not None:
        result["events_path"] = str(events_path)
    return result


def find_local_trace_metadata(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        metadata = normalize_trace_metadata(value)
        if metadata:
            return metadata
        for item in value.values():
            found = find_local_trace_metadata(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_local_trace_metadata(item)
            if found:
                return found
    elif isinstance(value, str):
        metadata = metadata_from_failure_text(value)
        if metadata:
            return metadata
    return None


def normalize_trace_metadata(value: dict[str, Any]) -> dict[str, Any] | None:
    if "messages_path" in value:
        return dict(value)
    agent_result = value.get("agent_result")
    if isinstance(agent_result, dict):
        metadata = agent_result.get("metadata")
        if isinstance(metadata, dict) and "messages_path" in metadata:
            return dict(metadata)
    metadata = value.get("metadata")
    if isinstance(metadata, dict) and "messages_path" in metadata:
        return dict(metadata)
    return None


def metadata_from_failure_text(text: str) -> dict[str, Any] | None:
    marker = "metadata="
    start = text.find(marker)
    if start < 0:
        return None
    raw = text[start + len(marker) :].lstrip()
    braced = balanced_braced_prefix(raw)
    if braced is None:
        return None
    try:
        parsed = ast.literal_eval(braced)
    except (SyntaxError, ValueError):
        return None
    if isinstance(parsed, dict) and "messages_path" in parsed:
        return dict(parsed)
    return None


def balanced_braced_prefix(text: str) -> str | None:
    if not text.startswith("{"):
        return None
    depth = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[: index + 1]
    return None


def local_existing_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    try:
        return path.resolve() if path.exists() else None
    except OSError:
        return None


def aggregate_results(
    *,
    config: Config,
    split_results: list[dict[str, Any]],
    run_identity: str | None = None,
    config_sha256: str | None = None,
) -> dict[str, Any]:
    passed = sum(int(item["passed"]) for item in split_results)
    total = sum(int(item["total"]) for item in split_results)
    by_split: dict[str, list[dict[str, Any]]] = {}
    for item in split_results:
        by_split.setdefault(str(item["split"]), []).append(item)
    return {
        "name": config.name,
        "model": config.model,
        "run_identity": run_identity,
        "config_sha256": config_sha256,
        "repeats": config.repeats,
        "passed": passed,
        "total": total,
        "correctness": passed / total if total else 0.0,
        "splits": by_split,
    }


def config_to_json(config: Config) -> dict[str, Any]:
    return {
        "name": config.name,
        "runner": config.runner,
        "model": config.model,
        "repeats": config.repeats,
        "evals_project": str(config.evals_project),
        "harness_workspace": str(config.harness_workspace),
        "task_root": str(config.task_root) if config.task_root is not None else None,
        "agent_import_path": config.agent_import_path,
        "environment": config.environment,
        "agent_kwargs": list(config.agent_kwargs),
        "infrastructure_retries": config.infrastructure_retries,
        "model_flag": config.model_flag,
        "summary_flag": config.summary_flag,
        "pytest_args": list(config.pytest_args),
        "case_concurrency": config.case_concurrency,
        "timeout_s": config.timeout_s,
        "agent_timeout_multiplier": config.agent_timeout_multiplier,
        "env": config.env,
        "cases": [case.__dict__ for case in config.cases],
    }


def resolve_path(base_dir: Path, raw: str) -> Path:
    path = Path(expand_env(raw)).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def expand_env(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in os.environ:
            raise KeyError(f"environment variable {key!r} is required")
        return os.environ[key]

    return ENV_PATTERN.sub(replace, value)


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")[:160] or "case"


def case_directory(repeat_dir: Path, case_id: str) -> Path:
    """Return a collision-resistant, human-readable directory for one case."""
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:12]
    return repeat_dir / "cases" / f"{safe_slug(case_id)}--{digest}"


def validate_repeat_result(
    payload: dict[str, Any],
    *,
    config: Config,
    split: str,
    cases: tuple[Case, ...],
    repeat_index: int,
    repeat_dir: Path | None = None,
) -> dict[str, Any]:
    """Validate a repeat aggregate before it can be reused or aggregated."""
    if payload.get("split") != split or type(payload.get("repeat")) is not int or payload.get("repeat") != repeat_index:
        raise ValueError(
            f"repeat result identity mismatch: expected split={split!r}, repeat={repeat_index}"
        )
    if payload.get("model") != config.model:
        raise ValueError(
            f"repeat result model mismatch: expected {config.model!r}, got {payload.get('model')!r}"
        )
    case_results = payload.get("case_results")
    if not isinstance(case_results, list) or len(case_results) != len(cases):
        raise ValueError(
            f"repeat result case count mismatch: expected {len(cases)}, got "
            f"{len(case_results) if isinstance(case_results, list) else case_results!r}"
        )
    expected_ids = [expand_env(case.render(model=config.model)) for case in cases]
    expected_by_id = {case_id: case for case_id in expected_ids}
    if len(expected_by_id) != len(expected_ids):
        raise ValueError(f"duplicate case ids configured for split {split!r}")
    seen: set[str] = set()
    for outcome in case_results:
        if not isinstance(outcome, dict):
            raise ValueError("repeat result contains a non-object case result")
        case_id = outcome.get("case_id")
        if case_id not in expected_by_id or case_id in seen:
            raise ValueError(f"repeat result case identity mismatch: {case_id!r}")
        seen.add(case_id)
        if outcome.get("split") != split or type(outcome.get("repeat")) is not int or outcome.get("repeat") != repeat_index:
            raise ValueError(f"repeat result case metadata mismatch for {case_id!r}")
        if repeat_dir is not None:
            checkpoint = case_directory(repeat_dir, case_id) / "result.json"
            if not checkpoint.is_file():
                raise ValueError(f"missing case checkpoint for reused result: {case_id!r}")
            validate_case_checkpoint(
                load_json_object(checkpoint),
                case_id=case_id,
                split=split,
                repeat=repeat_index,
            )
    if seen != set(expected_ids):
        raise ValueError(f"repeat result case ids do not match configured cases for split {split!r}")
    total = payload.get("total")
    passed = payload.get("passed")
    if type(total) is not int or type(passed) is not int or total != len(case_results):
        raise ValueError("repeat result aggregate counts are inconsistent")
    calculated_passed = sum(outcome.get("passed") is True for outcome in case_results)
    if passed != calculated_passed or passed < 0 or passed > total:
        raise ValueError("repeat result aggregate passed count is inconsistent")
    return payload


def validate_case_checkpoint(payload: dict[str, Any], *, case_id: str, split: str, repeat: int) -> dict[str, Any]:
    expected_hash = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
    if (
        payload.get("case_id") != case_id
        or payload.get("case_id_sha256") != expected_hash
        or payload.get("split") != split
        or payload.get("repeat") != repeat
    ):
        raise ValueError(
            f"checkpoint identity mismatch for {case_id!r}: "
            f"expected split={split!r}, repeat={repeat}, got "
            f"case_id={payload.get('case_id')!r}, split={payload.get('split')!r}, repeat={payload.get('repeat')!r}"
        )
    return payload


def normalize_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
