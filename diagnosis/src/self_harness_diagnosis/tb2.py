from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_tb2_verifier_evidence(*, messages_path: Path | None = None, job_dir: Path | None = None) -> dict[str, Any]:
    """Load concise TB2 terminal verifier evidence from a Harbor trial.

    Callers must pass either `job_dir` or an agent `messages_path` located under
    the Harbor trial directory. No default data roots or host-specific paths are
    assumed.
    """

    resolved_job_dir = job_dir or _job_dir_from_messages_path(messages_path)
    if resolved_job_dir is None:
        return {}
    result = _read_json_file(resolved_job_dir / "result.json")
    reward_text = _read_text_file(resolved_job_dir / "verifier" / "reward.txt", max_chars=200)
    ctrf = _read_json_file(resolved_job_dir / "verifier" / "ctrf.json")
    stdout = _read_text_file(resolved_job_dir / "verifier" / "test-stdout.txt", max_chars=350_000)
    stdout_summary = _summarize_verifier_stdout(stdout) if stdout else {}
    evidence: dict[str, Any] = {
        "job_dir": str(resolved_job_dir),
        "task_name": result.get("task_name") if isinstance(result, dict) else None,
        "trial_name": result.get("trial_name") if isinstance(result, dict) else None,
        "rewards": _extract_rewards(result),
        "reward_text": reward_text,
    }
    if ctrf:
        evidence["ctrf_summary"] = _ctrf_summary(ctrf)
    if stdout_summary:
        evidence.update(stdout_summary)
    terminal = _terminal_summary_from_evidence(evidence)
    if terminal:
        evidence["terminal_summary"] = terminal
    return evidence


def _job_dir_from_messages_path(messages_path: Path | None) -> Path | None:
    if messages_path is None:
        return None
    path = messages_path.expanduser().resolve()
    for parent in path.parents:
        if (parent / "result.json").exists() and (parent / "verifier").exists():
            return parent
    return None


def _extract_rewards(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    verifier_result = result.get("verifier_result")
    if isinstance(verifier_result, dict) and isinstance(verifier_result.get("rewards"), dict):
        return dict(verifier_result["rewards"])
    return {}


def _ctrf_summary(ctrf: dict[str, Any]) -> dict[str, Any]:
    results = ctrf.get("results")
    if isinstance(results, dict):
        summary = results.get("summary")
        if isinstance(summary, dict):
            return dict(summary)
    return {}


def _summarize_verifier_stdout(stdout: str) -> dict[str, Any]:
    failed_tests: list[dict[str, str]] = []
    snippets: list[str] = []
    missing_paths: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if not stripped:
            continue
        if "failed " in lower or "assertionerror:" in lower or "filenotfounderror:" in lower:
            snippets.append(stripped)
        if "assertionerror:" in lower or "filenotfounderror:" in lower:
            failed_tests.append({"name": _infer_test_name(stripped), "status": "failed", "trace_excerpt": stripped})
        for path in re.findall(r"(/[A-Za-z0-9_./-]+)", stripped):
            if any(needle in lower for needle in ("does not exist", "not found", "no such file", "could not find")):
                missing_paths.append(path)
    result: dict[str, Any] = {}
    if failed_tests:
        result["failed_tests"] = failed_tests[:20]
    if snippets:
        result["failure_snippets"] = snippets[:20]
        result["stdout_tail_excerpt"] = "\n".join(stdout.splitlines()[-80:])
    if missing_paths:
        result["missing_artifact_paths"] = sorted(set(missing_paths))
    return result


def _terminal_summary_from_evidence(evidence: dict[str, Any]) -> str:
    snippets = evidence.get("failure_snippets")
    if isinstance(snippets, list):
        for snippet in snippets:
            text = str(snippet).strip()
            if text:
                return text
    reward = evidence.get("reward_text")
    if str(reward or "").strip():
        return f"Verifier reward text: {reward}"
    return ""


def _infer_test_name(line: str) -> str:
    match = re.search(r"([A-Za-z0-9_./-]+::[A-Za-z0-9_./-]+)", line)
    return match.group(1) if match else "verifier_failure"


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_text_file(path: Path, *, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]
