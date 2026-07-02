#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import json
import os
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")
DEFAULT_REPEATS = 2


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
    model: str
    repeats: int
    evals_project: Path
    harness_workspace: Path
    agent_import_path: str
    model_flag: str
    summary_flag: str
    pytest_args: tuple[str, ...]
    case_concurrency: int
    timeout_s: float | None
    env: dict[str, str]
    cases: tuple[Case, ...]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Harbor-backed eval stage.")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model", help="Override [eval].model")
    parser.add_argument("--repeats", type=int, help="Override [eval].repeats")
    parser.add_argument("--split", action="append", help="Run only this split. May be repeated.")
    parser.add_argument("--case-concurrency", type=int, help="Override [eval].case_concurrency")
    parser.add_argument("--reuse-existing", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(
        args.config,
        model_override=args.model,
        repeats_override=args.repeats,
        concurrency_override=args.case_concurrency,
    )
    selected_splits = set(args.split or sorted({case.split for case in config.cases}))
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "config.resolved.json", config_to_json(config))

    split_results = []
    for split in sorted(selected_splits):
        cases = tuple(case for case in config.cases if case.split == split)
        if not cases:
            raise SystemExit(f"no cases configured for split {split!r}")
        for repeat_index in range(1, config.repeats + 1):
            repeat_dir = output_dir / "splits" / split / f"repeat-{repeat_index:02d}"
            if args.reuse_existing and (repeat_dir / "result.json").exists():
                result = json.loads((repeat_dir / "result.json").read_text())
            else:
                result = run_repeat(
                    config=config,
                    split=split,
                    cases=cases,
                    repeat_index=repeat_index,
                    repeat_dir=repeat_dir,
                )
            split_results.append(result)

    aggregate = aggregate_results(config=config, split_results=split_results)
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

    return Config(
        name=str(eval_config.get("name", "harbor-eval")),
        model=model,
        repeats=repeats,
        evals_project=resolve_path(base_dir, str(eval_config["evals_project"])),
        harness_workspace=resolve_path(base_dir, str(eval_config["harness_workspace"])),
        agent_import_path=str(eval_config["agent_import_path"]),
        model_flag=str(eval_config.get("model_flag", "--model")),
        summary_flag=str(eval_config.get("summary_flag", "--evals-report-file")),
        pytest_args=tuple(str(item) for item in eval_config.get("pytest_args", ["-q"])),
        case_concurrency=case_concurrency,
        timeout_s=timeout_s,
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


def run_repeat(
    *,
    config: Config,
    split: str,
    cases: tuple[Case, ...],
    repeat_index: int,
    repeat_dir: Path,
) -> dict[str, Any]:
    repeat_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    max_workers = min(config.case_concurrency, len(cases))
    outcomes = []
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
            for index, case in enumerate(cases)
        ]
        for future in concurrent.futures.as_completed(futures):
            outcomes.append(future.result())
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
    rendered = expand_env(case.render(model=config.model))
    case_dir = repeat_dir / "cases" / safe_slug(rendered)
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
            timeout=config.timeout_s,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        returncode = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = normalize_output(exc.stdout)
        stderr = normalize_output(exc.stderr) + f"\nTIMEOUT after {config.timeout_s}s\n"
        returncode = 124
    (case_dir / "stdout.log").write_text(stdout)
    (case_dir / "stderr.log").write_text(stderr)

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
        return json.loads(path.read_text())
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


def aggregate_results(*, config: Config, split_results: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(int(item["passed"]) for item in split_results)
    total = sum(int(item["total"]) for item in split_results)
    by_split: dict[str, list[dict[str, Any]]] = {}
    for item in split_results:
        by_split.setdefault(str(item["split"]), []).append(item)
    return {
        "name": config.name,
        "model": config.model,
        "repeats": config.repeats,
        "passed": passed,
        "total": total,
        "correctness": passed / total if total else 0.0,
        "splits": by_split,
    }


def config_to_json(config: Config) -> dict[str, Any]:
    return {
        "name": config.name,
        "model": config.model,
        "repeats": config.repeats,
        "evals_project": str(config.evals_project),
        "harness_workspace": str(config.harness_workspace),
        "agent_import_path": config.agent_import_path,
        "model_flag": config.model_flag,
        "summary_flag": config.summary_flag,
        "pytest_args": list(config.pytest_args),
        "case_concurrency": config.case_concurrency,
        "timeout_s": config.timeout_s,
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
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")[:180] or "case"


def normalize_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
