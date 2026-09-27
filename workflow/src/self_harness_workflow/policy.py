from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_files(root: Path, paths: Iterable[str]) -> dict[str, str]:
    result = {}
    for raw in paths:
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe readonly path: {raw}")
        target = root / relative
        if not target.is_file():
            raise ValueError(f"readonly file missing: {raw}")
        result[relative.as_posix()] = sha256_file(target)
    return result


def verify_snapshot(root: Path, expected: Mapping[str, str]) -> None:
    actual = snapshot_files(root, expected)
    changed = sorted(path for path in expected if actual.get(path) != expected[path])
    if changed:
        raise RuntimeError(f"readonly policy violation: {changed}")


@dataclass(frozen=True)
class EditBudget:
    max_files: int = 1
    max_surfaces: int = 1
    max_added_chars: int = 4000
    max_changed_lines: int = 80
    max_ast_nodes: int = 120
    max_prompt_token_delta: int = 800


def validate_candidate_edit(
    *, before: str, after: str, changed_files: int, changed_surfaces: int, budget: EditBudget = EditBudget()
) -> dict[str, Any]:
    if changed_files > budget.max_files or changed_surfaces > budget.max_surfaces:
        raise ValueError("candidate must change one file and one declared surface")
    added_chars = max(0, len(after) - len(before))
    before_lines, after_lines = before.splitlines(), after.splitlines()
    # Count actual changed lines via edit distance, not positional zip (which
    # overcounts every line that shifts after an insertion/deletion).
    matcher = SequenceMatcher(None, before_lines, after_lines)
    changed_lines = sum(
        len(range(a, b)) + len(range(c, d))
        for tag, a, b, c, d in matcher.get_opcodes()
        if tag != "equal"
    )
    try:
        ast_delta = max(0, sum(1 for _ in ast.walk(ast.parse(after))) - sum(1 for _ in ast.walk(ast.parse(before))))
    except SyntaxError as exc:
        raise ValueError("candidate is not valid Python") from exc
    token_delta = max(0, _approx_tokens(after) - _approx_tokens(before))
    metrics = {"added_chars": added_chars, "changed_lines": changed_lines, "ast_node_delta": ast_delta,
               "prompt_token_delta": token_delta, "token_count_method": "local_byte_approximation"}
    if not (added_chars <= budget.max_added_chars and changed_lines <= budget.max_changed_lines
            and ast_delta <= budget.max_ast_nodes and token_delta <= budget.max_prompt_token_delta):
        raise ValueError(f"candidate exceeds minimal edit budget: {metrics}")
    return metrics


def _approx_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4


class PhaseState:
    ORDER = ("baseline", "weakness-mining", "propose", "validate", "merge-retest", "freeze", "sealed-once")

    def __init__(self) -> None:
        self.completed: list[str] = []
        self.sealed_status: str | None = None

    def complete(self, phase: str, *, sealed_status: str = "completed") -> None:
        if self.sealed_status is not None:
            raise RuntimeError("experiment is sealed; evolution cannot continue")
        expected = self.ORDER[len(self.completed)] if len(self.completed) < len(self.ORDER) else None
        if phase != expected:
            raise RuntimeError(f"phase order violation: expected {expected!r}, got {phase!r}")
        self.completed.append(phase)
        if phase == "sealed-once":
            if sealed_status not in {"completed", "aborted"}:
                raise ValueError("sealed status must be completed or aborted")
            self.sealed_status = sealed_status


def validate_split_manifest(tasks: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    assignments: dict[str, str] = {}
    allowed = {"evolution", "regression", "sealed"}
    for task in tasks:
        task_id, split = str(task.get("id") or ""), str(task.get("split") or "")
        if not task_id or split not in allowed or task_id in assignments:
            raise ValueError("each task must have one unique evolution/regression/sealed assignment")
        assignments[task_id] = split
    return assignments


def compatible_pair(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    return first.get("component") != second.get("component") and set(first.get("files", [])).isdisjoint(second.get("files", []))


def combination_states(parent: str, first: str, second: str) -> tuple[str, str, str]:
    return f"{parent}+{first}", f"{parent}+{second}", f"{parent}+{first}+{second}"


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()
