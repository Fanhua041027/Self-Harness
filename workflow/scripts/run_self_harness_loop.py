#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import copy
import errno
import hashlib
import json
import math
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

VALIDITY_DIR = Path(__file__).resolve().parents[2] / "eval" / "scripts"
if str(VALIDITY_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDITY_DIR))

from result_validity import load_json_object, reject_duplicate_json_keys
from self_harness_workflow.policy import validate_candidate_edit

ROOT = Path(__file__).resolve().parents[2]
QUEUE_FORMAT = "self_harness.candidate_queue.v0"
BRANCH_STATE_FORMAT = "self_harness.branch_state.v0"
DEFAULT_CANDIDATE_ENV_VAR = "SELF_HARNESS_CANDIDATE_WORKSPACE"
BASELINE_BRANCH_ID = "baseline"
DEFAULT_COMMAND_TIMEOUT_S = 172800.0
LOCK_FILENAME = ".orchestrator.lock"
LOCK_FORMAT = "self_harness.orchestrator_lock.v1"
FINALIZATION_FILENAME = "finalization.intent.json"
FINALIZATION_FORMAT = "self_harness.finalization_intent.v1"
FORCE_CLEANUP_TIMEOUT_S = 5.0
PORTABLE_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class OrchestratorLockError(RuntimeError):
    pass


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError(errno.EIO, "lock file write made no progress")
        remaining = remaining[written:]


def _is_lock_contention_error(exc: OSError) -> bool:
    contention_errnos = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
    if hasattr(errno, "EWOULDBLOCK"):
        contention_errnos.add(errno.EWOULDBLOCK)
    return exc.errno in contention_errnos or getattr(exc, "winerror", None) in {32, 33}


def _lock_fd(fd: int) -> None:
    if os.fstat(fd).st_size == 0:
        os.lseek(fd, 0, os.SEEK_SET)
        _write_all(fd, b"\0")
        os.fsync(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _read_lock_metadata(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        handle.seek(1)
        raw = handle.read().decode("utf-8")
    payload = json.loads(raw, object_pairs_hook=reject_duplicate_json_keys)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} lock metadata must be a JSON object")
    return payload


def _lock_owner_summary(path: Path) -> str:
    try:
        owner_payload = _read_lock_metadata(path)
    except (OSError, ValueError, UnicodeError):
        return "unavailable or malformed metadata"
    if owner_payload.get("format") != LOCK_FORMAT:
        return f"unsupported lock format: {owner_payload.get('format')!r}"
    return json.dumps(
        {
            key: owner_payload.get(key)
            for key in ("pid", "host", "work_dir", "started_at")
            if isinstance(owner_payload.get(key), (str, int, float))
        },
        sort_keys=True,
    )


class OrchestratorLock:
    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir.resolve()
        self.path = self.work_dir / LOCK_FILENAME
        self.token = uuid.uuid4().hex
        self._fd: int | None = None

    def acquire(self) -> "OrchestratorLock":
        if self._fd is not None:
            raise OrchestratorLockError("orchestrator lock is already acquired by this object")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError as close_exc:
                exc.add_note(f"failed to close lock descriptor: {close_exc!r}")
            raise
        try:
            _lock_fd(fd)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError as close_exc:
                exc.add_note(f"failed to close contending lock descriptor: {close_exc!r}")
            if _is_lock_contention_error(exc):
                owner = _lock_owner_summary(self.path)
                raise OrchestratorLockError(
                    f"work directory is already locked: {self.path}; owner={owner}"
                ) from exc
            raise

        metadata = {
            "format": LOCK_FORMAT,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "work_dir": str(self.work_dir),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "token": self.token,
        }
        try:
            payload = json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode("utf-8")
            os.lseek(fd, 1, os.SEEK_SET)
            os.ftruncate(fd, 1)
            _write_all(fd, payload)
            os.fsync(fd)
        except BaseException as exc:
            try:
                _unlock_fd(fd)
            except BaseException as unlock_exc:
                exc.add_note(f"failed to unlock after metadata write failure: {unlock_exc!r}")
            try:
                os.close(fd)
            except BaseException as close_exc:
                exc.add_note(f"failed to close after metadata write failure: {close_exc!r}")
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        errors: list[BaseException] = []
        try:
            _unlock_fd(fd)
        except BaseException as exc:
            errors.append(exc)
        try:
            os.close(fd)
        except BaseException as exc:
            errors.append(exc)
        if len(errors) == 1:
            raise OrchestratorLockError(f"failed to release orchestrator lock: {errors[0]!r}") from errors[0]
        if errors:
            raise ExceptionGroup("failed to release orchestrator lock", errors)


@contextmanager
def orchestrator_lock(work_dir: Path) -> Iterator[OrchestratorLock]:
    lock = OrchestratorLock(work_dir).acquire()
    try:
        yield lock
    except BaseException as exc:
        try:
            lock.release()
        except BaseException as cleanup_exc:
            exc.add_note(f"orchestrator lock cleanup failed: {cleanup_exc!r}")
        raise
    else:
        lock.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the public Self-Harness eval/propose/eval/accept loop.",
        allow_abbrev=False,
    )
    parser.add_argument("--eval-config", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--surface", action="append", default=[], help="Initial surface spec as name=path. Repeatable.")
    parser.add_argument("--route-count", type=int, default=4)
    diagnosis_group = parser.add_mutually_exclusive_group()
    diagnosis_group.add_argument("--diagnosis", type=Path, help="Ready-made TB2 diagnosis brief. If omitted, use --diagnosis-command.")
    diagnosis_group.add_argument("--diagnosis-command", help="Command template that writes {diagnosis}.")
    proposer_group = parser.add_mutually_exclusive_group()
    proposer_group.add_argument("--proposer-response", type=Path, help="Ready-made proposer response JSON.")
    proposer_group.add_argument("--proposer-command", help="Command template that writes {response}.")
    parser.add_argument("--candidate-env-var", default=DEFAULT_CANDIDATE_ENV_VAR)
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=1,
        help="Number of pending candidates to evaluate this run. Use 0 for all pending candidates.",
    )
    parser.add_argument("--reuse-existing", action="store_true", help="Reuse existing stage artifacts when present.")
    return parser


def _require_input_file(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError(f"{label} must be a regular file: {resolved}")
    return resolved


def _validate_preflight_args(args: argparse.Namespace, work_dir: Path) -> None:
    if args.max_candidates < 0:
        raise RuntimeError("--max-candidates must be >= 0")
    if args.route_count < 1:
        raise RuntimeError("--route-count must be >= 1")
    if not PORTABLE_ENV_NAME.fullmatch(str(args.candidate_env_var)):
        raise RuntimeError("--candidate-env-var must be a portable environment variable name")
    if work_dir.exists() and not work_dir.is_dir():
        raise RuntimeError(f"--work-dir must be a directory: {work_dir}")

    args.eval_config = _require_input_file(args.eval_config, label="--eval-config")
    if args.diagnosis is not None:
        args.diagnosis = _require_input_file(args.diagnosis, label="--diagnosis")
    if args.proposer_response is not None:
        args.proposer_response = _require_input_file(args.proposer_response, label="--proposer-response")
    if args.surface:
        parse_surface_args(tuple(args.surface))
    elif not work_dir.exists():
        raise RuntimeError("first run must pass at least one --surface name=path")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    work_dir = args.work_dir.expanduser().resolve()
    _validate_preflight_args(args, work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    with orchestrator_lock(work_dir):
        return _run_workflow(args, work_dir)


def _run_workflow(args: argparse.Namespace, work_dir: Path) -> int:
    paths = WorkflowPaths(work_dir)
    eval_config = args.eval_config.expanduser().resolve()

    branch_state = load_or_init_branch_state(
        path=paths.branch_state,
        initial_surfaces=tuple(args.surface),
        baseline_eval_dir=paths.baseline_eval,
        baseline_result=paths.baseline_result,
    )
    if not paths.baseline_result.exists():
        run_baseline_eval(eval_config=eval_config, output_dir=paths.baseline_eval, reuse_existing=False)
    write_json(paths.branch_state, branch_state)

    queue = load_queue(paths.queue)
    recover_finalization(
        journal_path=paths.finalization,
        queue=queue,
        queue_path=paths.queue,
        branch_state=branch_state,
        branch_state_path=paths.branch_state,
    )
    validate_workflow_consistency(queue, branch_state)
    finalized = finalize_merge_groups(
        queue=queue,
        queue_path=paths.queue,
        branch_state=branch_state,
        branch_state_path=paths.branch_state,
        finalization_path=paths.finalization,
        eval_config=eval_config,
        candidate_env_var=args.candidate_env_var,
        reuse_existing=args.reuse_existing,
    )
    if finalized:
        print(
            "workflow complete: "
            f"active_branch={branch_state['active_branch_id']}, "
            f"queued={len(queue['candidates'])}, finalized_merge_groups={finalized}"
        )
        return 0
    if pending_candidates(queue):
        processed = process_pending_candidates(
            queue=queue,
            queue_path=paths.queue,
            branch_state=branch_state,
            branch_state_path=paths.branch_state,
            finalization_path=paths.finalization,
            eval_config=eval_config,
            candidate_env_var=args.candidate_env_var,
            max_candidates=args.max_candidates,
            reuse_existing=args.reuse_existing,
        )
        print(
            "workflow complete: "
            f"active_branch={branch_state['active_branch_id']}, "
            f"queued={len(queue['candidates'])}, evaluated_this_run={processed}"
        )
        return 0

    active_branch = get_active_branch(branch_state)
    branch_paths = paths.for_branch(str(active_branch["branch_id"]))
    active_proposer_surfaces = surface_args_from_branch(active_branch, key="proposer_surfaces")
    if not active_proposer_surfaces:
        raise RuntimeError(f"active branch {active_branch['branch_id']!r} has no surfaces")

    diagnosis_path = resolve_diagnosis(
        requested=args.diagnosis,
        command_template=args.diagnosis_command,
        branch_paths=branch_paths,
        baseline_result=Path(str(active_branch["baseline_result"])).expanduser().resolve(),
        reuse_existing=args.reuse_existing,
    )
    run_build_proposer_prompt(
        diagnosis=diagnosis_path,
        surfaces=active_proposer_surfaces,
        output_path=branch_paths.proposer_prompt,
        route_count=args.route_count,
        reuse_existing=args.reuse_existing,
    )
    response_path = resolve_proposer_response(
        requested=args.proposer_response,
        command_template=args.proposer_command,
        branch_paths=branch_paths,
        reuse_existing=args.reuse_existing,
    )
    run_parse_proposer_response(
        diagnosis=diagnosis_path,
        surfaces=active_proposer_surfaces,
        response=response_path,
        output_path=branch_paths.proposal_bundle,
        route_count=args.route_count,
        reuse_existing=args.reuse_existing,
    )

    queue = enqueue_candidates(
        queue=queue,
        proposal_bundle=read_json(branch_paths.proposal_bundle),
        proposal_bundle_path=branch_paths.proposal_bundle,
        parent_branch=active_branch,
        candidates_dir=branch_paths.candidates_dir,
    )
    write_json(paths.queue, queue)

    processed = process_pending_candidates(
        queue=queue,
        queue_path=paths.queue,
        branch_state=branch_state,
        branch_state_path=paths.branch_state,
        finalization_path=paths.finalization,
        eval_config=eval_config,
        candidate_env_var=args.candidate_env_var,
        max_candidates=args.max_candidates,
        reuse_existing=args.reuse_existing,
    )
    print(
        "workflow complete: "
        f"active_branch={branch_state['active_branch_id']}, "
        f"queued={len(queue['candidates'])}, evaluated_this_run={processed}"
    )
    return 0


class WorkflowPaths:
    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self.baseline_eval = work_dir / "baseline_eval"
        self.baseline_result = self.baseline_eval / "result.json"
        self.branches_dir = work_dir / "branches"
        self.branch_state = work_dir / "branch_state.json"
        self.queue = work_dir / "candidate_queue.json"
        self.finalization = work_dir / FINALIZATION_FILENAME

    def for_branch(self, branch_id: str) -> BranchPaths:
        return BranchPaths(self.work_dir, branch_id)


class BranchPaths:
    def __init__(self, work_dir: Path, branch_id: str) -> None:
        self.work_dir = work_dir
        self.branch_id = branch_id
        self.branch_dir = work_dir / "branches" / safe_slug(branch_id)
        self.diagnosis_dir = self.branch_dir / "diagnosis"
        self.diagnosis = self.diagnosis_dir / "diagnosis.md"
        self.proposer_dir = self.branch_dir / "proposer"
        self.proposer_prompt = self.proposer_dir / "prompt.txt"
        self.proposer_response = self.proposer_dir / "proposer_response.json"
        self.proposal_bundle = self.proposer_dir / "proposal_bundle.json"
        self.candidates_dir = self.branch_dir / "candidates"


def load_or_init_branch_state(
    *,
    path: Path,
    initial_surfaces: tuple[str, ...],
    baseline_eval_dir: Path,
    baseline_result: Path,
) -> dict[str, Any]:
    if path.exists():
        state = read_json(path)
        validate_branch_state(state)
        return state
    if not initial_surfaces:
        raise RuntimeError("first run must pass at least one --surface name=path")
    surfaces = parse_surface_args(initial_surfaces)
    return {
        "format": BRANCH_STATE_FORMAT,
        "active_branch_id": BASELINE_BRANCH_ID,
        "branches": [
            {
                "branch_id": BASELINE_BRANCH_ID,
                "parent_branch_id": None,
                "status": "active",
                "depth": 0,
                "eval_surfaces": surfaces,
                "proposer_surfaces": surfaces,
                "baseline_eval_dir": str(baseline_eval_dir),
                "baseline_result": str(baseline_result),
                "created_at": int(time.time()),
            }
        ],
    }


def validate_branch_state(state: dict[str, Any]) -> None:
    if state.get("format") != BRANCH_STATE_FORMAT:
        raise ValueError(f"branch state format must be {BRANCH_STATE_FORMAT!r}")
    if not isinstance(state.get("active_branch_id"), str) or not state["active_branch_id"]:
        raise ValueError("branch state must contain active_branch_id")
    if not isinstance(state.get("branches"), list) or not state["branches"]:
        raise ValueError("branch state must contain branches")
    active = get_active_branch(state)
    active_statuses = [
        item
        for item in state["branches"]
        if isinstance(item, dict) and item.get("status") == "active"
    ]
    if len(active_statuses) != 1 or active_statuses[0] is not active:
        raise ValueError("branch state must contain exactly one active branch matching active_branch_id")


def get_active_branch(state: dict[str, Any]) -> dict[str, Any]:
    active_id = state.get("active_branch_id")
    matches = [item for item in state.get("branches", []) if isinstance(item, dict) and item.get("branch_id") == active_id]
    if len(matches) != 1:
        raise ValueError(f"active branch {active_id!r} matched {len(matches)} branches")
    return matches[0]


def get_branch(state: dict[str, Any], branch_id: str) -> dict[str, Any]:
    matches = [item for item in state.get("branches", []) if isinstance(item, dict) and item.get("branch_id") == branch_id]
    if len(matches) != 1:
        raise ValueError(f"branch {branch_id!r} matched {len(matches)} branches")
    return matches[0]


def parse_surface_args(items: tuple[str, ...]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        name, sep, raw_path = item.partition("=")
        if not sep or not name.strip() or not raw_path.strip():
            raise RuntimeError(f"surface must be name=path, got: {item!r}")
        name = name.strip()
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"surface path must be a regular file for {name!r}: {path}")
        if name in parsed:
            raise RuntimeError(f"duplicate surface name: {name!r}")
        parsed[name] = str(path)
    return parsed


def surface_args_from_branch(branch: dict[str, Any], *, key: str) -> tuple[str, ...]:
    surfaces = branch.get(key)
    if not isinstance(surfaces, dict):
        raise ValueError(f"branch {branch.get('branch_id')!r} {key} must be an object")
    return tuple(f"{name}={path}" for name, path in sorted((str(k), str(v)) for k, v in surfaces.items()))


def run_baseline_eval(*, eval_config: Path, output_dir: Path, reuse_existing: bool) -> None:
    if reuse_existing and (output_dir / "result.json").exists():
        return
    run_command(
        [
            sys.executable,
            str(ROOT / "eval" / "scripts" / "run_harbor_eval.py"),
            "--config",
            str(eval_config),
            "--output-dir",
            str(output_dir),
        ]
    )


def resolve_diagnosis(
    *,
    requested: Path | None,
    command_template: str | None,
    branch_paths: BranchPaths,
    baseline_result: Path,
    reuse_existing: bool,
) -> Path:
    diagnosis = requested.expanduser().resolve() if requested is not None else branch_paths.diagnosis
    if diagnosis.exists() and (reuse_existing or requested is not None):
        return diagnosis
    if diagnosis.exists() and not reuse_existing:
        diagnosis.unlink()
    if not command_template:
        raise RuntimeError("diagnosis artifact is missing; pass --diagnosis or --diagnosis-command")
    run_external_template(
        command_template,
        placeholders={
            "diagnosis": diagnosis,
            "work_dir": branch_paths.work_dir,
            "branch_dir": branch_paths.branch_dir,
            "branch_id": branch_paths.branch_id,
            "baseline_result": baseline_result,
        },
    )
    if not diagnosis.exists():
        raise RuntimeError(f"diagnosis command did not create expected artifact: {diagnosis}")
    return diagnosis


def run_build_proposer_prompt(
    *,
    diagnosis: Path,
    surfaces: tuple[str, ...],
    output_path: Path,
    route_count: int,
    reuse_existing: bool,
) -> None:
    if reuse_existing and output_path.exists():
        return
    argv = [
        sys.executable,
        str(ROOT / "proposer" / "scripts" / "run_multi_proposer.py"),
        "--diagnosis",
        str(diagnosis),
        "--route-count",
        str(route_count),
        "--output",
        str(output_path),
    ]
    for surface in surfaces:
        argv.extend(["--surface", surface])
    run_command(argv)


def resolve_proposer_response(
    *,
    requested: Path | None,
    command_template: str | None,
    branch_paths: BranchPaths,
    reuse_existing: bool,
) -> Path:
    response = requested.expanduser().resolve() if requested is not None else branch_paths.proposer_response
    if response.exists() and (reuse_existing or requested is not None):
        return response
    if response.exists() and not reuse_existing:
        response.unlink()
    if not command_template:
        raise RuntimeError("proposer response artifact is missing; pass --proposer-response or --proposer-command")
    run_external_template(
        command_template,
        placeholders={
            "prompt": branch_paths.proposer_prompt,
            "response": response,
            "work_dir": branch_paths.work_dir,
            "branch_dir": branch_paths.branch_dir,
            "branch_id": branch_paths.branch_id,
        },
    )
    if not response.exists():
        raise RuntimeError(f"proposer command did not create expected artifact: {response}")
    return response


def run_parse_proposer_response(
    *,
    diagnosis: Path,
    surfaces: tuple[str, ...],
    response: Path,
    output_path: Path,
    route_count: int,
    reuse_existing: bool,
) -> None:
    if reuse_existing and output_path.exists():
        return
    argv = [
        sys.executable,
        str(ROOT / "proposer" / "scripts" / "run_multi_proposer.py"),
        "--diagnosis",
        str(diagnosis),
        "--route-count",
        str(route_count),
        "--response",
        str(response),
        "--output",
        str(output_path),
    ]
    for surface in surfaces:
        argv.extend(["--surface", surface])
    run_command(argv)


def enqueue_candidates(
    *,
    queue: dict[str, Any],
    proposal_bundle: dict[str, Any],
    proposal_bundle_path: Path,
    parent_branch: dict[str, Any],
    candidates_dir: Path,
) -> dict[str, Any]:
    proposals = proposal_bundle.get("proposals")
    if not isinstance(proposals, list):
        raise ValueError("proposal bundle must contain a proposals list")
    parent_branch_id = str(parent_branch["branch_id"])
    existing = {
        (str(item.get("parent_branch_id")), str(item.get("proposal_id")))
        for item in queue["candidates"]
        if isinstance(item, dict)
    }
    now = int(time.time())
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        proposal_id = str(proposal.get("proposal_id") or "").strip()
        if not proposal_id:
            raise ValueError("proposal is missing proposal_id")
        key = (parent_branch_id, proposal_id)
        if key in existing:
            continue
        metadata = proposal.get("metadata") if isinstance(proposal.get("metadata"), dict) else {}
        if str(metadata.get("selection_decision") or "").strip().lower() == "decline":
            continue
        candidate_id = safe_slug(proposal_id)
        queue["candidates"].append(
            {
                "proposal_id": proposal_id,
                "candidate_id": candidate_id,
                "mechanism_family": str(metadata.get("mechanism_family") or ""),
                "parent_branch_id": parent_branch_id,
                "status": "pending_eval",
                "candidate_dir": str(candidates_dir / candidate_id),
                "proposal_bundle": str(proposal_bundle_path),
                "parent_baseline_result": str(parent_branch["baseline_result"]),
                "parent_eval_surfaces": dict(parent_branch["eval_surfaces"]),
                "parent_proposer_surfaces": dict(parent_branch["proposer_surfaces"]),
                "enqueued_at": now,
            }
        )
        existing.add(key)
    return queue


CLAIM_FIELDS = ("claim_id", "worker_id", "lease_expires_at", "claimed_at", "stage")


def clear_candidate_claim(item: dict[str, Any]) -> None:
    for field in CLAIM_FIELDS:
        item.pop(field, None)


def process_pending_candidates(
    *,
    queue: dict[str, Any],
    queue_path: Path,
    branch_state: dict[str, Any],
    branch_state_path: Path,
    finalization_path: Path,
    eval_config: Path,
    candidate_env_var: str,
    max_candidates: int,
    reuse_existing: bool,
) -> int:
    now = int(time.time())
    recovered = False
    for item in queue["candidates"]:
        if isinstance(item, dict) and item.get("status") == "evaluating":
            expires = int(item.get("lease_expires_at", 0) or 0)
            if expires and expires <= now:
                item["status"] = "pending_eval"
                item["recovery_reason"] = "expired candidate lease"
                clear_candidate_claim(item)
                recovered = True
    if recovered:
        write_json(queue_path, queue)
    pending = [item for item in queue["candidates"] if item.get("status") == "pending_eval"]
    if max_candidates:
        pending = pending[:max_candidates]
    processed = 0
    for item in pending:
        item["status"] = "evaluating"
        item["attempt_count"] = int(item.get("attempt_count", 0)) + 1
        item["claim_id"] = hashlib.sha256(f"{os.getpid()}:{time.time_ns()}".encode()).hexdigest()
        item["worker_id"] = f"{os.environ.get('COMPUTERNAME', 'host')}:{os.getpid()}"
        item["lease_expires_at"] = int(time.time()) + 172800 + 60
        item["stage"] = "materialize"
        item["claimed_at"] = int(time.time())
        write_json(queue_path, queue)
        try:
            parent_branch = get_branch(branch_state, str(item["parent_branch_id"]))
            candidate_dir = Path(str(item["candidate_dir"])).expanduser().resolve()
            proposal_id = str(item["proposal_id"])
            eval_dir = candidate_dir / "eval"
            acceptance_path = candidate_dir / "acceptance.json"
            materialize_candidate(
                proposal_bundle=Path(str(item["proposal_bundle"])),
                proposal_id=proposal_id,
                surfaces=surface_args_from_queue_item(item, key="parent_eval_surfaces"),
                output_dir=candidate_dir,
                reuse_existing=reuse_existing,
            )
            item["stage"] = "validate"
            validate_materialized_candidate(candidate_dir=candidate_dir, parent_surfaces=item["parent_eval_surfaces"])
            item["stage"] = "eval"
            run_candidate_eval(
                eval_config=eval_config,
                output_dir=eval_dir,
                candidate_dir=candidate_dir,
                candidate_env_var=candidate_env_var,
                reuse_existing=reuse_existing,
            )
            item["stage"] = "acceptance"
            run_acceptance_gate(
                baseline_result=Path(str(item["parent_baseline_result"])),
                candidate_result=eval_dir / "result.json",
                output_path=acceptance_path,
                reuse_existing=reuse_existing,
            )
            acceptance = read_json(acceptance_path)
            accepted = bool(acceptance.get("accepted"))
            item["status"] = "accepted_pending_merge" if accepted else "rejected"
            item["eval_result"] = str(eval_dir / "result.json")
            item["acceptance_result"] = str(acceptance_path)
            item["evaluated_at"] = int(time.time())
            item["decision_reason"] = acceptance.get("reason")
            item.pop("failure", None)
            processed += 1
        except ValueError as exc:
            item["status"] = "rejected"
            item["decision_reason"] = f"candidate rejected: {exc}"
            item["rejected_at"] = int(time.time())
        except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            item["status"] = "pipeline_failed"
            item["failure"] = {"stage": item.get("stage"), "type": type(exc).__name__, "message": str(exc)}
            item["failed_at"] = int(time.time())
        finally:
            if item.get("status") != "evaluating":
                clear_candidate_claim(item)
            write_json(queue_path, queue)
            write_json(branch_state_path, branch_state)
    finalize_merge_groups(
        queue=queue,
        queue_path=queue_path,
        branch_state=branch_state,
        branch_state_path=branch_state_path,
        finalization_path=finalization_path,
        eval_config=eval_config,
        candidate_env_var=candidate_env_var,
        reuse_existing=reuse_existing,
    )
    return processed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _queue_item_identity(item: Mapping[str, Any]) -> dict[str, str]:
    identity = {
        key: str(item.get(key) or "")
        for key in ("parent_branch_id", "proposal_bundle", "proposal_id", "candidate_id")
    }
    if any(not value for value in identity.values()):
        raise ValueError(f"queued candidate has incomplete identity: {identity}")
    return identity


def _identity_key(identity: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return tuple(
        str(identity.get(key) or "")
        for key in ("parent_branch_id", "proposal_bundle", "proposal_id", "candidate_id")
    )


def build_finalization_identity(
    *,
    parent_branch: Mapping[str, Any],
    accepted_items: list[dict[str, Any]],
    result_kind: str,
) -> tuple[str, dict[str, Any]]:
    if result_kind not in {"single", "merged"}:
        raise ValueError(f"unsupported finalization result kind: {result_kind!r}")
    if not accepted_items:
        raise ValueError("finalization requires at least one accepted candidate")
    bundle_paths = {
        str(Path(str(item["proposal_bundle"])).expanduser().resolve())
        for item in accepted_items
    }
    if len(bundle_paths) != 1:
        raise ValueError("finalization candidates must share one proposal bundle")
    bundle_path = Path(next(iter(bundle_paths)))
    if not bundle_path.is_file():
        raise ValueError(f"finalization proposal bundle is missing: {bundle_path}")
    candidates = []
    for item in accepted_items:
        candidate_dir = Path(str(item["candidate_dir"])).expanduser().resolve()
        manifest_path = candidate_dir / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"finalization candidate manifest is missing: {manifest_path}")
        candidates.append(
            {
                **_queue_item_identity(item),
                "candidate_dir": str(candidate_dir),
                "manifest_sha256": _sha256_file(manifest_path),
            }
        )
    candidates.sort(key=lambda item: _identity_key(item))
    identity = {
        "parent_branch_id": str(parent_branch["branch_id"]),
        "proposal_bundle": {
            "path": str(bundle_path),
            "sha256": _sha256_file(bundle_path),
        },
        "result_kind": result_kind,
        "candidates": candidates,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    finalization_id = "finalize-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return finalization_id, identity


def build_finalization_intent(
    *,
    finalization_id: str,
    identity: dict[str, Any],
    parent_branch: Mapping[str, Any],
    child_branch: dict[str, Any],
    accepted_items: list[dict[str, Any]],
    target_status: str,
    merged_candidate_id: str | None,
) -> dict[str, Any]:
    queue_updates = []
    for item in accepted_items:
        target = {
            "status": target_status,
            "accepted_branch_id": str(child_branch["branch_id"]),
            "finalization_id": finalization_id,
        }
        if merged_candidate_id is not None:
            target["merged_candidate_id"] = merged_candidate_id
        queue_updates.append(
            {
                "identity": _queue_item_identity(item),
                "source_status": "accepted_pending_merge",
                "target": target,
            }
        )
    queue_updates.sort(key=lambda update: _identity_key(update["identity"]))
    return {
        "format": FINALIZATION_FORMAT,
        "state": "prepared",
        "finalization_id": finalization_id,
        "identity": identity,
        "parent_branch_id": str(parent_branch["branch_id"]),
        "child_branch": child_branch,
        "queue_updates": queue_updates,
    }


def _validate_finalization_intent(intent: Mapping[str, Any]) -> None:
    if intent.get("format") != FINALIZATION_FORMAT:
        raise ValueError(f"finalization journal format must be {FINALIZATION_FORMAT!r}")
    if intent.get("state") not in {"prepared", "completed"}:
        raise ValueError("finalization journal state must be 'prepared' or 'completed'")
    finalization_id = intent.get("finalization_id")
    if not isinstance(finalization_id, str) or not finalization_id:
        raise ValueError("finalization journal is missing finalization_id")
    parent_branch_id = intent.get("parent_branch_id")
    if not isinstance(parent_branch_id, str) or not parent_branch_id:
        raise ValueError("finalization journal is missing parent_branch_id")
    child = intent.get("child_branch")
    if not isinstance(child, dict) or child.get("finalization_id") != finalization_id:
        raise ValueError("finalization child binding does not match finalization_id")
    if child.get("parent_branch_id") != parent_branch_id:
        raise ValueError("finalization child binding does not match parent branch")
    updates = intent.get("queue_updates")
    if not isinstance(updates, list) or not updates:
        raise ValueError("finalization journal must contain queue updates")
    keys: set[tuple[str, str, str, str]] = set()
    for update in updates:
        if not isinstance(update, dict) or not isinstance(update.get("identity"), dict):
            raise ValueError("finalization queue update is malformed")
        key = _identity_key(update["identity"])
        if any(not part for part in key) or key in keys:
            raise ValueError("finalization queue update identities must be complete and unique")
        keys.add(key)
        target = update.get("target")
        if update.get("source_status") != "accepted_pending_merge" or not isinstance(target, dict):
            raise ValueError("finalization queue transition is malformed")
        if target.get("finalization_id") != finalization_id:
            raise ValueError("finalization queue binding does not match finalization_id")
        if target.get("accepted_branch_id") != child.get("branch_id"):
            raise ValueError("finalization queue binding does not match child branch")


def _apply_finalization_intent(
    *,
    intent: Mapping[str, Any],
    queue: dict[str, Any],
    branch_state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    _validate_finalization_intent(intent)
    next_queue = copy.deepcopy(queue)
    next_branch_state = copy.deepcopy(branch_state)
    finalization_id = str(intent["finalization_id"])
    child = copy.deepcopy(intent["child_branch"])
    branches = next_branch_state.get("branches")
    if not isinstance(branches, list):
        raise ValueError("branch state must contain branches")

    finalization_matches = [
        branch
        for branch in branches
        if isinstance(branch, dict) and branch.get("finalization_id") == finalization_id
    ]
    if len(finalization_matches) > 1:
        raise ValueError(f"finalization {finalization_id!r} matched multiple branches")
    if finalization_matches:
        if finalization_matches[0] != child:
            raise ValueError(f"finalization {finalization_id!r} has a conflicting child binding")
    else:
        branch_id = str(child.get("branch_id") or "")
        if not branch_id:
            raise ValueError("planned child branch is missing branch_id")
        if any(isinstance(branch, dict) and str(branch.get("branch_id")) == branch_id for branch in branches):
            raise ValueError(f"planned branch id is already occupied: {branch_id!r}")
        parent_id = str(intent["parent_branch_id"])
        parents = [
            branch
            for branch in branches
            if isinstance(branch, dict) and str(branch.get("branch_id")) == parent_id
        ]
        if len(parents) != 1:
            raise ValueError(f"finalization parent {parent_id!r} matched {len(parents)} branches")
        if next_branch_state.get("active_branch_id") != parent_id or parents[0].get("status") != "active":
            raise ValueError(f"finalization parent is no longer active: {parent_id!r}")
        parents[0]["status"] = "superseded"
        branches.append(child)
        next_branch_state["active_branch_id"] = branch_id

    candidates = next_queue.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("queue candidates must be a list")
    used_indices: set[int] = set()
    for update in intent["queue_updates"]:
        expected_key = _identity_key(update["identity"])
        matches = [
            (index, item)
            for index, item in enumerate(candidates)
            if isinstance(item, dict) and _identity_key(item) == expected_key
        ]
        if len(matches) != 1:
            raise ValueError(f"finalization queue item {expected_key!r} matched {len(matches)} entries")
        index, item = matches[0]
        if index in used_indices:
            raise ValueError(f"finalization queue item {expected_key!r} is updated more than once")
        used_indices.add(index)
        target = update["target"]
        if item.get("status") == update["source_status"]:
            conflicting = [key for key, value in target.items() if key in item and item[key] != value and key != "status"]
            if conflicting:
                raise ValueError(f"finalization queue item has conflicting target fields: {conflicting}")
            item.update(target)
        elif item.get("status") == target.get("status"):
            if any(item.get(key) != value for key, value in target.items()):
                raise ValueError(f"finalization queue item {expected_key!r} has a conflicting terminal binding")
        else:
            raise ValueError(
                f"finalization queue item {expected_key!r} has status {item.get('status')!r}; "
                f"expected {update['source_status']!r} or {target.get('status')!r}"
            )

    validate_branch_state(next_branch_state)
    validate_workflow_consistency(next_queue, next_branch_state)
    return next_queue, next_branch_state


def commit_finalization(
    *,
    intent: dict[str, Any],
    journal_path: Path,
    queue: dict[str, Any],
    queue_path: Path,
    branch_state: dict[str, Any],
    branch_state_path: Path,
) -> None:
    if journal_path.exists():
        previous = read_json(journal_path)
        _validate_finalization_intent(previous)
        if previous.get("state") == "prepared":
            raise RuntimeError(f"unfinished finalization journal must be recovered first: {journal_path}")
    next_queue, next_branch_state = _apply_finalization_intent(
        intent=intent,
        queue=queue,
        branch_state=branch_state,
    )
    write_json(journal_path, intent)
    write_json(branch_state_path, next_branch_state)
    write_json(queue_path, next_queue)
    completed = copy.deepcopy(intent)
    completed["state"] = "completed"
    write_json(journal_path, completed)
    branch_state.clear()
    branch_state.update(next_branch_state)
    queue.clear()
    queue.update(next_queue)


def recover_finalization(
    *,
    journal_path: Path,
    queue: dict[str, Any],
    queue_path: Path,
    branch_state: dict[str, Any],
    branch_state_path: Path,
) -> bool:
    if not journal_path.exists():
        return False
    intent = read_json(journal_path)
    _validate_finalization_intent(intent)
    next_queue, next_branch_state = _apply_finalization_intent(
        intent=intent,
        queue=queue,
        branch_state=branch_state,
    )
    changed = next_queue != queue or next_branch_state != branch_state
    if changed:
        write_json(branch_state_path, next_branch_state)
        write_json(queue_path, next_queue)
    if intent["state"] != "completed":
        intent["state"] = "completed"
        write_json(journal_path, intent)
    branch_state.clear()
    branch_state.update(next_branch_state)
    queue.clear()
    queue.update(next_queue)
    return changed


def validate_workflow_consistency(queue: Mapping[str, Any], branch_state: Mapping[str, Any]) -> None:
    branches = branch_state.get("branches")
    if not isinstance(branches, list):
        raise ValueError("branch state must contain branches")
    by_id: dict[str, dict[str, Any]] = {}
    finalization_ids: set[str] = set()
    for branch in branches:
        if not isinstance(branch, dict):
            raise ValueError("every branch state entry must be an object")
        branch_id = str(branch.get("branch_id") or "")
        if not branch_id or branch_id in by_id:
            raise ValueError(f"branch ids must be nonempty and unique: {branch_id!r}")
        by_id[branch_id] = branch
        finalization_id = branch.get("finalization_id")
        if finalization_id is not None:
            if not isinstance(finalization_id, str) or not finalization_id or finalization_id in finalization_ids:
                raise ValueError(f"branch finalization ids must be nonempty and unique: {finalization_id!r}")
            finalization_ids.add(finalization_id)

    candidates = queue.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("queue candidates must be a list")
    for item in candidates:
        if not isinstance(item, dict):
            continue
        accepted_branch_id = item.get("accepted_branch_id")
        if accepted_branch_id is None:
            continue
        accepted_branch_id = str(accepted_branch_id)
        branch = by_id.get(accepted_branch_id)
        if branch is None:
            raise ValueError(
                f"queued candidate {item.get('candidate_id')!r} references missing accepted branch "
                f"{accepted_branch_id!r}; restore a consistent state or a prepared finalization journal"
            )
        item_finalization = item.get("finalization_id")
        branch_finalization = branch.get("finalization_id")
        if item_finalization is not None or branch_finalization is not None:
            if item_finalization != branch_finalization:
                raise ValueError(
                    f"queued candidate {item.get('candidate_id')!r} finalization binding does not match "
                    f"branch {accepted_branch_id!r}"
                )


def finalize_merge_groups(
    *,
    queue: dict[str, Any],
    queue_path: Path,
    branch_state: dict[str, Any],
    branch_state_path: Path,
    finalization_path: Path,
    eval_config: Path,
    candidate_env_var: str,
    reuse_existing: bool,
) -> int:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in queue["candidates"]:
        if not isinstance(item, dict):
            continue
        if item.get("status") not in {"pending_eval", "evaluating", "accepted_pending_merge", "rejected", "pipeline_failed"}:
            continue
        key = (str(item.get("parent_branch_id") or ""), str(item.get("proposal_bundle") or ""))
        if not key[0] or not key[1]:
            continue
        groups.setdefault(key, []).append(item)

    for (_parent_branch_id, _proposal_bundle), items in groups.items():
        if any(item.get("status") in {"pending_eval", "evaluating"} for item in items):
            continue
        accepted_items = [item for item in items if item.get("status") == "accepted_pending_merge"]
        if not accepted_items:
            continue
        parent_branch = get_branch(branch_state, str(accepted_items[0]["parent_branch_id"]))
        result_kind = "single" if len(accepted_items) == 1 else "merged"
        finalization_id, identity = build_finalization_identity(
            parent_branch=parent_branch,
            accepted_items=accepted_items,
            result_kind=result_kind,
        )
        if len(accepted_items) == 1:
            item = accepted_items[0]
            candidate_dir = Path(str(item["candidate_dir"])).expanduser().resolve()
            child_branch = create_child_branch(
                branch_state=branch_state,
                parent_branch=parent_branch,
                queue_item=item,
                candidate_id=str(item["candidate_id"]),
                candidate_dir=candidate_dir,
                eval_result=Path(str(item["eval_result"])),
                eval_dir=Path(str(item["eval_result"])).parent,
                finalization_id=finalization_id,
            )
            intent = build_finalization_intent(
                finalization_id=finalization_id,
                identity=identity,
                parent_branch=parent_branch,
                child_branch=child_branch,
                accepted_items=accepted_items,
                target_status="accepted",
                merged_candidate_id=None,
            )
            commit_finalization(
                intent=intent,
                journal_path=finalization_path,
                queue=queue,
                queue_path=queue_path,
                branch_state=branch_state,
                branch_state_path=branch_state_path,
            )
            return 1

        merged = create_merged_candidate(
            parent_branch=parent_branch,
            accepted_items=accepted_items,
            reuse_existing=reuse_existing,
        )
        run_candidate_eval(
            eval_config=eval_config,
            output_dir=merged["eval_dir"],
            candidate_dir=merged["candidate_dir"],
            candidate_env_var=candidate_env_var,
            reuse_existing=reuse_existing,
        )
        run_acceptance_gate(
            baseline_result=Path(str(parent_branch["baseline_result"])),
            candidate_result=merged["eval_dir"] / "result.json",
            output_path=merged["acceptance_path"],
            reuse_existing=reuse_existing,
        )
        acceptance = read_json(merged["acceptance_path"])
        if bool(acceptance.get("accepted")):
            child_branch = plan_branch_from_surfaces(
                branch_state=branch_state,
                parent_branch=parent_branch,
                branch_id_base=f"{parent_branch['branch_id']}+{merged['candidate_id']}",
                eval_surfaces=merged["eval_surfaces"],
                proposer_surfaces=merged["proposer_surfaces"],
                baseline_result=merged["eval_dir"] / "result.json",
                baseline_eval_dir=merged["eval_dir"],
                accepted_candidate_dir=merged["candidate_dir"],
                accepted_candidate_id=merged["candidate_id"],
                accepted_mechanism_family="merged",
                merged_candidate_ids=[str(item["candidate_id"]) for item in accepted_items],
                finalization_id=finalization_id,
            )
            intent = build_finalization_intent(
                finalization_id=finalization_id,
                identity=identity,
                parent_branch=parent_branch,
                child_branch=child_branch,
                accepted_items=accepted_items,
                target_status="accepted_merged",
                merged_candidate_id=str(merged["candidate_id"]),
            )
            commit_finalization(
                intent=intent,
                journal_path=finalization_path,
                queue=queue,
                queue_path=queue_path,
                branch_state=branch_state,
                branch_state_path=branch_state_path,
            )
        else:
            for item in accepted_items:
                item["status"] = "accepted_merge_rejected"
                item["merged_candidate_id"] = merged["candidate_id"]
                item["merge_rejection_reason"] = acceptance.get("reason")
            write_json(queue_path, queue)
        return 1

    return 0


def surface_args_from_queue_item(item: dict[str, Any], *, key: str) -> tuple[str, ...]:
    surfaces = item.get(key)
    if not isinstance(surfaces, dict):
        raise ValueError(f"queued candidate {item.get('candidate_id')!r} is missing {key}")
    return tuple(f"{name}={path}" for name, path in sorted((str(k), str(v)) for k, v in surfaces.items()))


def create_child_branch(
    *,
    branch_state: dict[str, Any],
    parent_branch: dict[str, Any],
    queue_item: dict[str, Any],
    candidate_id: str,
    candidate_dir: Path,
    eval_result: Path,
    eval_dir: Path,
    finalization_id: str,
) -> dict[str, Any]:
    candidate_eval_surfaces = surfaces_from_candidate_manifest(candidate_dir / "manifest.json", candidate_dir=candidate_dir)
    eval_surfaces = dict(require_surface_map(parent_branch, key="eval_surfaces"))
    eval_surfaces.update(candidate_eval_surfaces)
    if is_prompt_candidate(queue_item):
        proposer_surfaces = dict(parent_branch["proposer_surfaces"])
    else:
        proposer_surfaces = build_proposer_surfaces_for_non_prompt_items(
            parent_branch=parent_branch,
            accepted_items=[queue_item],
            accepted_surface_maps={candidate_id: candidate_eval_surfaces},
            output_dir=candidate_dir / "proposer_current",
        )
    return plan_branch_from_surfaces(
        branch_state=branch_state,
        parent_branch=parent_branch,
        branch_id_base=f"{parent_branch['branch_id']}+{candidate_id}",
        eval_surfaces=eval_surfaces,
        proposer_surfaces=proposer_surfaces,
        baseline_result=eval_result,
        baseline_eval_dir=eval_dir,
        accepted_candidate_dir=candidate_dir,
        accepted_candidate_id=candidate_id,
        accepted_mechanism_family=str(queue_item.get("mechanism_family") or ""),
        merged_candidate_ids=None,
        finalization_id=finalization_id,
    )


def create_merged_candidate(
    *,
    parent_branch: dict[str, Any],
    accepted_items: list[dict[str, Any]],
    reuse_existing: bool,
) -> dict[str, Any]:
    if not accepted_items:
        raise ValueError("create_merged_candidate requires at least one accepted item")

    candidate_ids = [str(item["candidate_id"]) for item in accepted_items]
    candidate_id = merged_candidate_id(candidate_ids)
    base_dir = Path(str(accepted_items[0]["candidate_dir"])).expanduser().resolve().parent
    candidate_dir = base_dir / candidate_id
    current_dir = candidate_dir / "current"
    current_dir.mkdir(parents=True, exist_ok=True)

    parent_eval_surfaces = require_surface_map(parent_branch, key="eval_surfaces")
    parent_proposer_surfaces = require_surface_map(parent_branch, key="proposer_surfaces")
    accepted_surface_maps = {
        str(item["candidate_id"]): surfaces_from_candidate_manifest(
            Path(str(item["candidate_dir"])).expanduser().resolve() / "manifest.json",
            candidate_dir=Path(str(item["candidate_dir"])).expanduser().resolve(),
        )
        for item in accepted_items
    }

    eval_surfaces: dict[str, str] = {}
    changed_surfaces: list[str] = []
    for name, parent_path in sorted(parent_eval_surfaces.items()):
        parent_text = Path(parent_path).read_text(encoding="utf-8")
        merged_text = parent_text
        for item in accepted_items:
            candidate_path = accepted_surface_maps[str(item["candidate_id"])].get(name)
            if candidate_path is None:
                continue
            candidate_text = Path(candidate_path).read_text(encoding="utf-8")
            if candidate_text == parent_text:
                continue
            merged_text = merge_candidate_changes(
                parent_source=parent_text,
                current_source=merged_text,
                candidate_source=candidate_text,
                candidate_id=str(item["candidate_id"]),
                surface_name=name,
            )
        relative = Path(parent_path).name
        target = current_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(merged_text, encoding="utf-8")
        eval_surfaces[name] = str(target.resolve())
        if merged_text != parent_text:
            changed_surfaces.append(name)
    if not changed_surfaces:
        raise RuntimeError(f"merged candidate {candidate_id!r} has no surface changes")

    proposer_surfaces = build_proposer_surfaces_for_non_prompt_items(
        parent_branch=parent_branch,
        accepted_items=accepted_items,
        accepted_surface_maps=accepted_surface_maps,
        output_dir=candidate_dir / "proposer_current",
    )

    manifest = {
        "candidate_id": candidate_id,
        "proposal_id": "merged",
        "changed_surfaces": changed_surfaces,
        "proposal_path": "merge.json",
        "variant_path": "merge.json",
        "surface_files": {name: str(Path("current") / Path(path).name) for name, path in sorted(eval_surfaces.items())},
        "merged_candidate_ids": candidate_ids,
        "merged_proposal_ids": [str(item["proposal_id"]) for item in accepted_items],
    }
    merge_payload = {
        "candidate_id": candidate_id,
        "parent_branch_id": parent_branch["branch_id"],
        "changed_surfaces": changed_surfaces,
        "merged_items": [
            {
                "candidate_id": str(item["candidate_id"]),
                "proposal_id": str(item["proposal_id"]),
                "mechanism_family": str(item.get("mechanism_family") or ""),
                "candidate_dir": str(Path(str(item["candidate_dir"])).expanduser().resolve()),
            }
            for item in accepted_items
        ],
        "eval_surfaces": eval_surfaces,
        "proposer_surfaces": proposer_surfaces,
    }
    write_json(candidate_dir / "manifest.json", manifest)
    write_json(candidate_dir / "merge.json", merge_payload)
    return {
        "candidate_id": candidate_id,
        "candidate_dir": candidate_dir,
        "eval_dir": candidate_dir / "eval",
        "acceptance_path": candidate_dir / "acceptance.json",
        "eval_surfaces": eval_surfaces,
        "proposer_surfaces": proposer_surfaces,
    }


def build_proposer_surfaces_for_non_prompt_items(
    *,
    parent_branch: dict[str, Any],
    accepted_items: list[dict[str, Any]],
    accepted_surface_maps: dict[str, dict[str, str]],
    output_dir: Path,
) -> dict[str, str]:
    non_prompt_items = [item for item in accepted_items if not is_prompt_candidate(item)]
    parent_eval_surfaces = require_surface_map(parent_branch, key="eval_surfaces")
    parent_proposer_surfaces = require_surface_map(parent_branch, key="proposer_surfaces")
    proposer_surfaces = dict(parent_proposer_surfaces)
    if not non_prompt_items:
        return proposer_surfaces

    output_dir.mkdir(parents=True, exist_ok=True)
    for name, proposer_parent_path in sorted(parent_proposer_surfaces.items()):
        if name not in parent_eval_surfaces:
            raise RuntimeError(f"proposer surface {name!r} is missing from parent eval surfaces")
        eval_parent_text = Path(parent_eval_surfaces[name]).read_text(encoding="utf-8")
        proposer_text = Path(proposer_parent_path).read_text(encoding="utf-8")
        merged_proposer_text = proposer_text
        for item in non_prompt_items:
            candidate_id = str(item["candidate_id"])
            candidate_path = accepted_surface_maps[candidate_id].get(name)
            if candidate_path is None:
                continue
            candidate_text = Path(candidate_path).read_text(encoding="utf-8")
            if candidate_text == eval_parent_text:
                continue
            merged_proposer_text = merge_candidate_changes(
                parent_source=eval_parent_text,
                current_source=merged_proposer_text,
                candidate_source=candidate_text,
                candidate_id=candidate_id,
                surface_name=name,
            )
        if merged_proposer_text != proposer_text:
            target = output_dir / Path(proposer_parent_path).name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(merged_proposer_text, encoding="utf-8")
            proposer_surfaces[name] = str(target.resolve())
    return proposer_surfaces


def merge_candidate_changes(
    *,
    parent_source: str,
    current_source: str,
    candidate_source: str,
    candidate_id: str,
    surface_name: str,
) -> str:
    changed_functions = changed_top_level_functions(
        parent_source=parent_source,
        candidate_source=candidate_source,
        candidate_id=candidate_id,
        surface_name=surface_name,
    )
    merged = current_source
    for function_name, candidate_definition in changed_functions:
        merged = apply_function_definition(
            parent_source=parent_source,
            current_source=merged,
            function_name=function_name,
            candidate_definition=candidate_definition,
            candidate_source=candidate_source,
            candidate_id=candidate_id,
            surface_name=surface_name,
        )
    return merged


def changed_top_level_functions(
    *,
    parent_source: str,
    candidate_source: str,
    candidate_id: str,
    surface_name: str,
) -> list[tuple[str, str]]:
    parent_spans = top_level_function_spans(parent_source, label=f"parent:{surface_name}")
    candidate_spans = top_level_function_spans(candidate_source, label=f"{candidate_id}:{surface_name}")
    deleted = sorted(set(parent_spans) - set(candidate_spans))
    if deleted:
        raise RuntimeError(
            f"candidate {candidate_id!r} deletes top-level functions on surface {surface_name!r}: "
            + ", ".join(deleted)
        )
    if normalize_non_function_source(
        strip_top_level_functions(parent_source, parent_spans)
    ) != normalize_non_function_source(strip_top_level_functions(candidate_source, candidate_spans)):
        raise RuntimeError(
            f"candidate {candidate_id!r} changes unsupported non-function top-level code on surface {surface_name!r}"
        )

    changed: list[tuple[str, str]] = []
    for function_name in function_order(candidate_source):
        candidate_definition = source_segment(candidate_source, candidate_spans[function_name])
        parent_span = parent_spans.get(function_name)
        parent_definition = source_segment(parent_source, parent_span) if parent_span else None
        if candidate_definition != parent_definition:
            changed.append((function_name, candidate_definition))
    if not changed and candidate_source != parent_source:
        raise RuntimeError(
            f"candidate {candidate_id!r} changes surface {surface_name!r}, but no mergeable top-level function changed"
        )
    return changed


def apply_function_definition(
    *,
    parent_source: str,
    current_source: str,
    function_name: str,
    candidate_definition: str,
    candidate_source: str,
    candidate_id: str,
    surface_name: str,
) -> str:
    parent_spans = top_level_function_spans(parent_source, label=f"parent:{surface_name}")
    current_spans = top_level_function_spans(current_source, label=f"current:{surface_name}")
    current_span = current_spans.get(function_name)
    if current_span is not None:
        current_definition = source_segment(current_source, current_span)
        parent_span = parent_spans.get(function_name)
        parent_definition = source_segment(parent_source, parent_span) if parent_span else None
        if parent_definition is not None and current_definition != parent_definition and current_definition != candidate_definition:
            raise RuntimeError(
                f"merge conflict on {surface_name!r}.{function_name}: candidate {candidate_id!r} "
                "modifies a function already changed by another accepted candidate"
            )
        if parent_definition is None and current_definition != candidate_definition:
            raise RuntimeError(
                f"merge conflict on {surface_name!r}.{function_name}: candidate {candidate_id!r} "
                "modifies a function already changed by another accepted candidate"
            )
        return replace_span(current_source, current_span, ensure_trailing_newline(candidate_definition))

    insertion_index = insertion_line_for_new_function(
        current_source=current_source,
        candidate_source=candidate_source,
        function_name=function_name,
    )
    lines = current_source.splitlines(keepends=True)
    insertion_text = ensure_blank_separation(current_source, insertion_index, candidate_definition)
    return "".join(lines[:insertion_index] + [insertion_text] + lines[insertion_index:])


def top_level_function_spans(source: str, *, label: str) -> dict[str, tuple[int, int]]:
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise RuntimeError(f"cannot parse Python surface {label}: {exc}") from exc
    spans: dict[str, tuple[int, int]] = {}
    for node in module.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = node.lineno
        if node.decorator_list:
            start = min(decorator.lineno for decorator in node.decorator_list)
        end = getattr(node, "end_lineno", None)
        if end is None:
            raise RuntimeError(f"Python parser did not provide end_lineno for {label}.{node.name}")
        spans[node.name] = (start - 1, end)
    return spans


def function_order(source: str) -> list[str]:
    module = ast.parse(source)
    return [node.name for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def strip_top_level_functions(source: str, spans: dict[str, tuple[int, int]]) -> str:
    lines = source.splitlines(keepends=True)
    removed: set[int] = set()
    for start, end in spans.values():
        removed.update(range(start, end))
    return "".join(line for index, line in enumerate(lines) if index not in removed)


def normalize_non_function_source(source: str) -> str:
    return "\n".join(line.rstrip() for line in source.splitlines() if line.strip())


def source_segment(source: str, span: tuple[int, int] | None) -> str | None:
    if span is None:
        return None
    start, end = span
    return "".join(source.splitlines(keepends=True)[start:end])


def replace_span(source: str, span: tuple[int, int], replacement: str) -> str:
    start, end = span
    lines = source.splitlines(keepends=True)
    return "".join(lines[:start] + [replacement] + lines[end:])


def insertion_line_for_new_function(*, current_source: str, candidate_source: str, function_name: str) -> int:
    current_spans = top_level_function_spans(current_source, label="current")
    candidate_order = function_order(candidate_source)
    if function_name not in candidate_order:
        raise RuntimeError(f"candidate function {function_name!r} is missing from candidate source")
    after_target = candidate_order[candidate_order.index(function_name) + 1 :]
    for next_function in after_target:
        span = current_spans.get(next_function)
        if span is not None:
            return span[0]
    return len(current_source.splitlines(keepends=True))


def ensure_trailing_newline(value: str) -> str:
    return value if value.endswith("\n") else value + "\n"


def ensure_blank_separation(source: str, insertion_index: int, value: str) -> str:
    lines = source.splitlines(keepends=True)
    prefix = ""
    suffix = ""
    if insertion_index > 0 and lines and lines[insertion_index - 1].strip():
        prefix = "\n"
    if insertion_index < len(lines) and lines[insertion_index].strip():
        suffix = "\n"
    return prefix + ensure_trailing_newline(value) + suffix


def merged_candidate_id(candidate_ids: list[str]) -> str:
    raw = "merged-" + "-".join(candidate_ids)
    slug = safe_slug(raw)
    if len(slug) <= 96:
        return slug
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:84].rstrip('-_.+')}-{digest}"


def require_surface_map(branch: dict[str, Any], *, key: str) -> dict[str, str]:
    surfaces = branch.get(key)
    if not isinstance(surfaces, dict) or not surfaces:
        raise ValueError(f"branch {branch.get('branch_id')!r} is missing {key}")
    return {str(name): str(path) for name, path in surfaces.items()}


def plan_branch_from_surfaces(
    *,
    branch_state: dict[str, Any],
    parent_branch: dict[str, Any],
    branch_id_base: str,
    eval_surfaces: dict[str, str],
    proposer_surfaces: dict[str, str],
    baseline_result: Path,
    baseline_eval_dir: Path,
    accepted_candidate_dir: Path,
    accepted_candidate_id: str,
    accepted_mechanism_family: str,
    merged_candidate_ids: list[str] | None,
    finalization_id: str,
) -> dict[str, Any]:
    branch_id = unique_branch_id(branch_state, branch_id_base)
    child = {
        "branch_id": branch_id,
        "parent_branch_id": parent_branch["branch_id"],
        "status": "active",
        "depth": int(parent_branch.get("depth", 0)) + 1,
        "eval_surfaces": eval_surfaces,
        "proposer_surfaces": proposer_surfaces,
        "baseline_eval_dir": str(baseline_eval_dir),
        "baseline_result": str(baseline_result),
        "accepted_candidate_dir": str(accepted_candidate_dir),
        "accepted_candidate_id": accepted_candidate_id,
        "accepted_mechanism_family": accepted_mechanism_family,
        "created_at": int(time.time()),
        "finalization_id": finalization_id,
    }
    if merged_candidate_ids:
        child["merged_candidate_ids"] = merged_candidate_ids
    return child


def is_prompt_candidate(queue_item: dict[str, Any]) -> bool:
    return str(queue_item.get("mechanism_family") or "").strip() == "prompt_instruction"


def surfaces_from_candidate_manifest(manifest_path: Path, *, candidate_dir: Path) -> dict[str, str]:
    manifest = read_json(manifest_path)
    surface_files = manifest.get("surface_files")
    if not isinstance(surface_files, dict) or not surface_files:
        raise ValueError(f"candidate manifest missing surface_files: {manifest_path}")
    surfaces: dict[str, str] = {}
    for name, relative_path in surface_files.items():
        path = candidate_dir / str(relative_path)
        if not path.exists():
            raise ValueError(f"candidate surface file is missing for {name!r}: {path}")
        surfaces[str(name)] = str(path.resolve())
    return surfaces


def unique_branch_id(branch_state: dict[str, Any], base: str) -> str:
    existing = {str(item.get("branch_id")) for item in branch_state.get("branches", []) if isinstance(item, dict)}
    candidate = safe_slug(base)
    if candidate not in existing:
        return candidate
    index = 2
    while f"{candidate}-{index}" in existing:
        index += 1
    return f"{candidate}-{index}"


def materialize_candidate(
    *,
    proposal_bundle: Path,
    proposal_id: str,
    surfaces: tuple[str, ...],
    output_dir: Path,
    reuse_existing: bool,
) -> None:
    if reuse_existing and (output_dir / "manifest.json").exists():
        return
    argv = [
        sys.executable,
        str(ROOT / "proposer" / "scripts" / "materialize_candidate.py"),
        "--bundle",
        str(proposal_bundle),
        "--proposal-id",
        proposal_id,
        "--candidate-id",
        safe_slug(proposal_id),
        "--output-dir",
        str(output_dir),
    ]
    for surface in surfaces:
        argv.extend(["--surface", surface])
    run_command(argv)


def validate_materialized_candidate(*, candidate_dir: Path, parent_surfaces: Mapping[str, Any]) -> dict[str, Any]:
    manifest = read_json(candidate_dir / "manifest.json")
    files = manifest.get("surface_files")
    if not isinstance(files, dict) or len(files) != 1:
        raise ValueError("candidate must materialize exactly one declared surface")
    changed = manifest.get("changed_surfaces")
    if not isinstance(changed, list) or len(changed) != 1:
        raise ValueError("candidate must change exactly one declared surface")
    surface = str(changed[0])
    if surface not in parent_surfaces:
        raise ValueError(f"candidate changed undeclared surface: {surface}")
    current = candidate_dir / str(files[surface])
    parent = Path(str(parent_surfaces[surface])).resolve()
    if not current.is_file() or not parent.is_file():
        raise ValueError("candidate surface files must be regular files")
    metrics = validate_candidate_edit(
        before=parent.read_text(encoding="utf-8"),
        after=current.read_text(encoding="utf-8"),
        changed_files=1,
        changed_surfaces=1,
    )
    return metrics


def run_candidate_eval(
    *,
    eval_config: Path,
    output_dir: Path,
    candidate_dir: Path,
    candidate_env_var: str,
    reuse_existing: bool,
) -> None:
    if reuse_existing and (output_dir / "result.json").exists():
        return
    argv = [
        sys.executable,
        str(ROOT / "eval" / "scripts" / "run_harbor_eval.py"),
        "--config",
        str(eval_config),
        "--output-dir",
        str(output_dir),
    ]
    if reuse_existing:
        argv.append("--reuse-existing")
    run_command(
        argv,
        extra_env={candidate_env_var: str(candidate_dir)},
    )


def run_acceptance_gate(*, baseline_result: Path, candidate_result: Path, output_path: Path, reuse_existing: bool) -> None:
    if reuse_existing and output_path.exists():
        return
    run_command(
        [
            sys.executable,
            str(ROOT / "acceptance" / "scripts" / "run_acceptance_gate.py"),
            "--baseline-result",
            str(baseline_result),
            "--candidate-result",
            str(candidate_result),
            "--output",
            str(output_path),
        ]
    )


def load_queue(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"format": QUEUE_FORMAT, "candidates": []}
    queue = read_json(path)
    if queue.get("format") != QUEUE_FORMAT:
        raise ValueError(f"queue format must be {QUEUE_FORMAT!r}")
    if not isinstance(queue.get("candidates"), list):
        raise ValueError("queue candidates must be a list")
    return queue


def pending_candidates(queue: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in queue["candidates"] if isinstance(item, dict) and item.get("status") == "pending_eval"]


def run_external_template(template: str, *, placeholders: dict[str, Any]) -> None:
    formatted = template.format(**{key: str(value) for key, value in placeholders.items()})
    run_command(split_external_command(formatted))


def split_external_command(command: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(command)
    tokens = shlex.split(command, posix=False)
    return [
        token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"} else token
        for token in tokens
    ]


class StageTimeoutError(RuntimeError):
    def __init__(self, argv: list[str], timeout_s: float) -> None:
        self.argv = argv
        self.timeout_s = timeout_s
        super().__init__(f"command timed out after {timeout_s}s: {shlex.join(argv)}")


def _remaining_timeout(deadline: float) -> float:
    return max(0.001, deadline - time.monotonic())


def _terminate_process_tree(process: subprocess.Popen[Any], grace_s: float) -> None:
    """Best-effort tree termination with bounded direct-child reaping."""
    errors: list[Exception] = []
    graceful_deadline = time.monotonic() + grace_s
    force_deadline = graceful_deadline + FORCE_CLEANUP_TIMEOUT_S

    poll = getattr(process, "poll", None)
    if callable(poll) and poll() is not None:
        process.wait(timeout=_remaining_timeout(force_deadline))
        return

    tree_failed = False
    if os.name == "nt":
        if grace_s > 0:
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_remaining_timeout(graceful_deadline),
                )
                if result.returncode:
                    tree_failed = True
                    errors.append(RuntimeError(f"taskkill /T exited with status {result.returncode}"))
            except (OSError, subprocess.SubprocessError) as exc:
                tree_failed = True
                errors.append(exc)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            tree_failed = True
            errors.append(exc)

    if tree_failed:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        except OSError as exc:
            errors.append(exc)

    if grace_s > 0:
        try:
            process.wait(timeout=_remaining_timeout(graceful_deadline))
        except subprocess.TimeoutExpired:
            pass
        except OSError as exc:
            errors.append(exc)
        else:
            if errors:
                raise ExceptionGroup("process cleanup encountered errors", errors)
            return

    force_tree_failed = False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_remaining_timeout(force_deadline),
            )
            if result.returncode:
                force_tree_failed = True
                errors.append(RuntimeError(f"taskkill /T /F exited with status {result.returncode}"))
        except (OSError, subprocess.SubprocessError) as exc:
            force_tree_failed = True
            errors.append(exc)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            force_tree_failed = True
            errors.append(exc)

    if force_tree_failed or not (callable(poll) and poll() is not None):
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            errors.append(exc)

    try:
        process.wait(timeout=_remaining_timeout(force_deadline))
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(exc)
    if errors:
        raise ExceptionGroup("process cleanup encountered errors", errors)


def _cleanup_after_exception(process: subprocess.Popen[Any], grace_s: float, exc: BaseException) -> None:
    try:
        _terminate_process_tree(process, grace_s)
    except BaseException as cleanup_exc:
        exc.add_note(f"process cleanup failed: {cleanup_exc!r}")


def run_command(
    argv: list[str],
    *,
    extra_env: dict[str, str] | None = None,
    timeout_s: float | None = DEFAULT_COMMAND_TIMEOUT_S,
    termination_grace_s: float = 10.0,
) -> None:
    if timeout_s is not None and (not math.isfinite(timeout_s) or timeout_s <= 0):
        raise ValueError("timeout_s must be finite and positive")
    if not math.isfinite(termination_grace_s) or termination_grace_s < 0:
        raise ValueError("termination_grace_s must be finite and nonnegative")
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    print("+ " + shlex.join(argv))
    kwargs: dict[str, Any] = {"cwd": ROOT, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(argv, **kwargs)
    try:
        returncode = process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        error = StageTimeoutError(argv, float(timeout_s))
        _cleanup_after_exception(process, termination_grace_s, error)
        raise error from exc
    except BaseException as exc:
        _cleanup_after_exception(process, termination_grace_s, exc)
        raise
    if returncode:
        raise subprocess.CalledProcessError(returncode, argv)


def read_json(path: Path) -> dict[str, Any]:
    return load_json_object(path)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def safe_slug(value: str) -> str:
    allowed = []
    for char in value.strip():
        if char.isalnum() or char in "_.-+":
            allowed.append(char)
        else:
            allowed.append("-")
    return "".join(allowed).strip("-_.+") or "candidate"


if __name__ == "__main__":
    raise SystemExit(main())
