from __future__ import annotations

import importlib.util
import errno
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "workflow" / "scripts" / "run_self_harness_loop.py"
spec = importlib.util.spec_from_file_location("self_harness_loop", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def owner_process(work_dir: Path, *, crash: bool = False) -> subprocess.Popen[str]:
    action = "os._exit(0)" if crash else "sys.stdin.readline(); lock.release()"
    code = f"""
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('lock_owner', Path(sys.argv[1]))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
lock = module.OrchestratorLock(Path(sys.argv[2])).acquire()
print('ready', flush=True)
{action}
"""
    return subprocess.Popen(
        [sys.executable, "-c", code, str(SCRIPT), str(work_dir)],
        cwd=SCRIPT.parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_write_json_replaces_atomically(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    module.write_json(path, {"version": 1})
    module.write_json(path, {"version": 2, "items": [1, 2]})
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2
    assert list(tmp_path.glob(".*.tmp")) == []


def test_write_all_retries_interrupts_and_short_writes(monkeypatch) -> None:
    written = bytearray()
    calls = 0

    def short_write(_fd, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise InterruptedError
        chunk = bytes(payload[:2])
        written.extend(chunk)
        return len(chunk)

    monkeypatch.setattr(module.os, "write", short_write)
    module._write_all(7, b"abcdef")
    assert written == b"abcdef"
    assert calls == 4


def test_write_all_rejects_zero_progress(monkeypatch) -> None:
    monkeypatch.setattr(module.os, "write", lambda _fd, _payload: 0)
    with pytest.raises(OSError, match="no progress"):
        module._write_all(7, b"x")


@pytest.mark.parametrize("err", [errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EWOULDBLOCK])
def test_lock_contention_errno_is_normalized(err: int) -> None:
    assert module._is_lock_contention_error(OSError(err, "busy"))


@pytest.mark.parametrize("winerror", [32, 33])
def test_lock_contention_winerror_is_normalized(winerror: int) -> None:
    error = OSError(errno.EINVAL, "busy")
    error.winerror = winerror
    assert module._is_lock_contention_error(error)
    assert not module._is_lock_contention_error(OSError(errno.EINVAL, "not contention"))


def test_run_command_timeout_is_bounded() -> None:
    started = time.monotonic()
    with pytest.raises(module.StageTimeoutError) as captured:
        module.run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_s=0.1,
            termination_grace_s=0.2,
        )
    assert captured.value.timeout_s == 0.1
    assert time.monotonic() - started < 5


def test_run_command_rejects_invalid_timeouts(monkeypatch) -> None:
    spawned = False

    def unexpected_spawn(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("Popen must not run")

    monkeypatch.setattr(module.subprocess, "Popen", unexpected_spawn)
    for timeout in (0, -1, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite and positive"):
            module.run_command([sys.executable, "-c", "pass"], timeout_s=timeout)
    for grace in (-1, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite and nonnegative"):
            module.run_command([sys.executable, "-c", "pass"], termination_grace_s=grace)
    assert not spawned


def test_run_command_cleans_up_on_keyboard_interrupt(monkeypatch) -> None:
    class FakeProcess:
        pid = 123

        def wait(self, timeout=None):
            raise KeyboardInterrupt

    process = FakeProcess()
    cleaned: list[object] = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(module, "_terminate_process_tree", lambda target, grace: cleaned.append((target, grace)))
    with pytest.raises(KeyboardInterrupt):
        module.run_command(["fake"], timeout_s=1, termination_grace_s=0.25)
    assert cleaned == [(process, 0.25)]


def test_windows_cleanup_bounds_taskkill_and_uses_direct_fallback(monkeypatch) -> None:
    class FakeProcess:
        pid = 123

        def __init__(self):
            self.waits: list[float] = []
            self.terminated = False
            self.killed = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            assert timeout is not None and timeout > 0
            self.waits.append(timeout)
            if len(self.waits) == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            return 0

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode

    process = FakeProcess()
    taskkill_timeouts: list[float] = []
    results = iter((Result(1), Result(0)))

    def fake_run(*_args, timeout=None, **_kwargs):
        assert timeout is not None and timeout > 0
        taskkill_timeouts.append(timeout)
        return next(results)

    monkeypatch.setattr(module.os, "name", "nt")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(ExceptionGroup, match="cleanup encountered errors"):
        module._terminate_process_tree(process, 0.1)
    assert process.terminated
    assert process.killed
    assert len(taskkill_timeouts) == 2
    assert len(process.waits) == 2


def test_posix_cleanup_falls_back_and_bounds_final_reap(monkeypatch) -> None:
    class FakeProcess:
        pid = 456

        def __init__(self):
            self.waits: list[float] = []
            self.terminated = False
            self.killed = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            assert timeout is not None and timeout > 0
            self.waits.append(timeout)
            if len(self.waits) == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            return 0

    process = FakeProcess()
    monkeypatch.setattr(module.os, "name", "posix")
    monkeypatch.setattr(module.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(module.os, "killpg", lambda *_args: (_ for _ in ()).throw(PermissionError("denied")), raising=False)
    with pytest.raises(ExceptionGroup, match="cleanup encountered errors"):
        module._terminate_process_tree(process, 0.1)
    assert process.terminated
    assert process.killed
    assert len(process.waits) == 2


def test_cleanup_failure_is_not_primary_exception(monkeypatch) -> None:
    class FakeProcess:
        pid = 789

        def wait(self, timeout=None):
            raise KeyboardInterrupt

    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(module, "_terminate_process_tree", lambda *_args: (_ for _ in ()).throw(OSError("cleanup")))
    with pytest.raises(KeyboardInterrupt) as captured:
        module.run_command(["fake"], timeout_s=1, termination_grace_s=0.1)
    assert any("process cleanup failed" in note for note in captured.value.__notes__)


def test_run_candidate_eval_only_reuses_when_requested(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(module, "run_command", lambda argv, **kwargs: calls.append(argv))
    module.run_candidate_eval(eval_config=tmp_path / "eval.toml", output_dir=tmp_path / "eval", candidate_dir=tmp_path / "candidate", candidate_env_var="CANDIDATE_DIR", reuse_existing=False)
    assert "--reuse-existing" not in calls[0]
    module.run_candidate_eval(eval_config=tmp_path / "eval.toml", output_dir=tmp_path / "eval2", candidate_dir=tmp_path / "candidate", candidate_env_var="CANDIDATE_DIR", reuse_existing=True)
    assert "--reuse-existing" in calls[1]


def test_invalid_cli_does_not_create_or_lock_workdir(tmp_path: Path) -> None:
    work_dir = tmp_path / "should-not-exist"
    with pytest.raises(SystemExit):
        module.main(["--eval-config", "eval.toml", "--work-dir", str(work_dir), "--unknown"])
    assert not work_dir.exists()


def test_negative_candidate_limit_fails_before_workdir_creation(tmp_path: Path) -> None:
    work_dir = tmp_path / "should-not-exist"
    with pytest.raises(RuntimeError, match="max-candidates"):
        module.main(["--eval-config", "eval.toml", "--work-dir", str(work_dir), "--max-candidates", "-1"])
    assert not work_dir.exists()


def preflight_args(tmp_path: Path) -> tuple[list[str], Path, Path, Path]:
    work_dir = tmp_path / "new-run"
    config = tmp_path / "eval.toml"
    surface = tmp_path / "solver.py"
    config.write_text("[eval]\n", encoding="utf-8")
    surface.write_text("pass\n", encoding="utf-8")
    argv = [
        "--eval-config",
        str(config),
        "--work-dir",
        str(work_dir),
        "--surface",
        f"solver={surface}",
    ]
    return argv, work_dir, config, surface


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--route-count", "0"], "route-count"),
        (["--route-count", "-1"], "route-count"),
        (["--candidate-env-var", ""], "candidate-env-var"),
        (["--candidate-env-var", "BAD=NAME"], "candidate-env-var"),
    ],
)
def test_static_cli_errors_do_not_create_workdir(tmp_path: Path, extra: list[str], message: str) -> None:
    argv, work_dir, _config, _surface = preflight_args(tmp_path)
    with pytest.raises(RuntimeError, match=message):
        module.main([*argv, *extra])
    assert not work_dir.exists()


def test_missing_eval_config_does_not_create_workdir(tmp_path: Path) -> None:
    argv, work_dir, _config, _surface = preflight_args(tmp_path)
    argv[1] = str(tmp_path / "missing.toml")
    with pytest.raises(RuntimeError, match="eval-config"):
        module.main(argv)
    assert not work_dir.exists()


def test_new_run_without_surfaces_does_not_create_workdir(tmp_path: Path) -> None:
    argv, work_dir, _config, _surface = preflight_args(tmp_path)
    argv = argv[:4]
    with pytest.raises(RuntimeError, match="first run"):
        module.main(argv)
    assert not work_dir.exists()


def test_invalid_or_duplicate_surfaces_do_not_create_workdir(tmp_path: Path) -> None:
    argv, work_dir, _config, surface = preflight_args(tmp_path)
    argv[-1] = "malformed"
    with pytest.raises(RuntimeError, match="name=path"):
        module.main(argv)
    assert not work_dir.exists()

    argv, work_dir, _config, surface = preflight_args(tmp_path)
    with pytest.raises(RuntimeError, match="duplicate surface"):
        module.main([*argv, "--surface", f"solver={surface}"])
    assert not work_dir.exists()

    argv, work_dir, _config, _surface = preflight_args(tmp_path)
    argv[-1] = f"solver={tmp_path}"
    with pytest.raises(RuntimeError, match="regular file"):
        module.main(argv)
    assert not work_dir.exists()


def test_missing_explicit_artifacts_do_not_create_workdir(tmp_path: Path) -> None:
    for option in ("--diagnosis", "--proposer-response"):
        argv, work_dir, _config, _surface = preflight_args(tmp_path)
        with pytest.raises(RuntimeError, match=option.removeprefix("--")):
            module.main([*argv, option, str(tmp_path / "missing.json")])
        assert not work_dir.exists()


def test_artifact_and_command_options_are_mutually_exclusive(tmp_path: Path) -> None:
    argv, work_dir, _config, _surface = preflight_args(tmp_path)
    with pytest.raises(SystemExit):
        module.main([*argv, "--diagnosis", str(tmp_path / "x"), "--diagnosis-command", "cmd"])
    assert not work_dir.exists()


def test_orchestrator_lock_is_exclusive_and_reports_owner(tmp_path: Path) -> None:
    first = module.OrchestratorLock(tmp_path).acquire()
    try:
        lock_payload = module._read_lock_metadata(tmp_path / module.LOCK_FILENAME)
        assert lock_payload["pid"]
        assert lock_payload["format"] == module.LOCK_FORMAT
        assert "token" in lock_payload
        with pytest.raises(module.OrchestratorLockError, match="already locked") as captured:
            module.OrchestratorLock(tmp_path).acquire()
        assert str(lock_payload["pid"]) in str(captured.value)
    finally:
        first.release()
    assert (tmp_path / module.LOCK_FILENAME).exists()
    with module.orchestrator_lock(tmp_path):
        pass


def test_orchestrator_lock_is_exclusive_across_processes(tmp_path: Path) -> None:
    owner = owner_process(tmp_path)
    try:
        assert owner.stdout and owner.stdout.readline().strip() == "ready"
        with pytest.raises(module.OrchestratorLockError, match="already locked"):
            module.OrchestratorLock(tmp_path).acquire()
    finally:
        if owner.stdin:
            owner.stdin.write("release\n")
            owner.stdin.flush()
        stdout, stderr = owner.communicate(timeout=10)
        assert owner.returncode == 0, (stdout, stderr)
    with module.orchestrator_lock(tmp_path):
        pass


def test_orchestrator_lock_recovers_after_owner_crash(tmp_path: Path) -> None:
    owner = owner_process(tmp_path, crash=True)
    assert owner.stdout and owner.stdout.readline().strip() == "ready"
    stdout, stderr = owner.communicate(timeout=10)
    assert owner.returncode == 0, (stdout, stderr)
    with module.orchestrator_lock(tmp_path):
        pass
    assert (tmp_path / module.LOCK_FILENAME).exists()


def test_orchestrator_lock_releases_after_exception(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with module.orchestrator_lock(tmp_path):
            raise RuntimeError("boom")
    with module.orchestrator_lock(tmp_path):
        pass


def test_orchestrator_lock_preserves_primary_exception(monkeypatch, tmp_path: Path) -> None:
    lock = module.OrchestratorLock(tmp_path)
    monkeypatch.setattr(lock, "release", lambda: (_ for _ in ()).throw(OSError("cleanup")))
    monkeypatch.setattr(module, "OrchestratorLock", lambda _path: lock)
    with pytest.raises(RuntimeError, match="primary") as captured:
        with module.orchestrator_lock(tmp_path):
            raise RuntimeError("primary")
    assert any("cleanup" in note for note in captured.value.__notes__)
    if lock._fd is not None:
        module._unlock_fd(lock._fd)
        os.close(lock._fd)
        lock._fd = None


def test_orchestrator_lock_releases_after_keyboard_interrupt(tmp_path: Path) -> None:
    with pytest.raises(KeyboardInterrupt):
        with module.orchestrator_lock(tmp_path):
            raise KeyboardInterrupt
    assert (tmp_path / module.LOCK_FILENAME).exists()
    with module.orchestrator_lock(tmp_path):
        pass


def test_orchestrator_locks_do_not_interfere_across_workdirs(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    with module.orchestrator_lock(first_dir), module.orchestrator_lock(second_dir):
        assert (first_dir / module.LOCK_FILENAME).exists()
        assert (second_dir / module.LOCK_FILENAME).exists()


def test_orchestrator_lock_persists_without_unlink(tmp_path: Path) -> None:
    first = module.OrchestratorLock(tmp_path).acquire()
    path = tmp_path / module.LOCK_FILENAME
    first.release()
    first.release()
    assert path.exists()
    second = module.OrchestratorLock(tmp_path).acquire()
    second.release()


def test_orchestrator_lock_replaces_malformed_unlocked_metadata(tmp_path: Path) -> None:
    path = tmp_path / module.LOCK_FILENAME
    path.write_bytes(b"\0not-json")
    lock = module.OrchestratorLock(tmp_path).acquire()
    lock.release()
    assert module._read_lock_metadata(path)["format"] == module.LOCK_FORMAT


def test_expired_candidate_recovery_clears_claim_fields(monkeypatch, tmp_path: Path) -> None:
    item = {
        "status": "evaluating",
        "lease_expires_at": 1,
        "claim_id": "claim",
        "worker_id": "worker",
        "claimed_at": 1,
        "stage": "eval",
        "attempt_count": 2,
        "parent_branch_id": "baseline",
        "proposal_bundle": "bundle.json",
    }
    queue = {"candidates": [item]}
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(module, "write_json", lambda _path, payload: writes.append(json.loads(json.dumps(payload))))
    module.process_pending_candidates(
        queue=queue,
        queue_path=tmp_path / "queue.json",
        branch_state={"branches": []},
        branch_state_path=tmp_path / "state.json",
        finalization_path=tmp_path / "finalization.intent.json",
        eval_config=tmp_path / "eval.toml",
        candidate_env_var="CANDIDATE_DIR",
        max_candidates=-1,
        reuse_existing=False,
    )
    assert item["status"] == "pending_eval"
    assert item["attempt_count"] == 2
    assert item["recovery_reason"] == "expired candidate lease"
    assert not set(module.CLAIM_FIELDS).intersection(item)
    assert writes


def finalization_fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    parent = {
        "branch_id": "baseline",
        "parent_branch_id": None,
        "status": "active",
        "depth": 0,
        "eval_surfaces": {"solver": "solver.py"},
        "proposer_surfaces": {"solver": "solver.py"},
        "baseline_eval_dir": "baseline_eval",
        "baseline_result": "baseline_eval/result.json",
        "created_at": 1,
    }
    state: dict[str, object] = {
        "format": module.BRANCH_STATE_FORMAT,
        "active_branch_id": "baseline",
        "branches": [parent],
    }
    item = {
        "parent_branch_id": "baseline",
        "proposal_bundle": "bundle.json",
        "proposal_id": "proposal-1",
        "candidate_id": "candidate-1",
        "status": "accepted_pending_merge",
    }
    queue: dict[str, object] = {"format": module.QUEUE_FORMAT, "candidates": [item]}
    finalization_id = "finalize-test"
    child = {
        "branch_id": "baseline+candidate-1",
        "parent_branch_id": "baseline",
        "status": "active",
        "depth": 1,
        "eval_surfaces": {"solver": "candidate/current/solver.py"},
        "proposer_surfaces": {"solver": "candidate/current/solver.py"},
        "baseline_eval_dir": "candidate/eval",
        "baseline_result": "candidate/eval/result.json",
        "accepted_candidate_dir": "candidate",
        "accepted_candidate_id": "candidate-1",
        "accepted_mechanism_family": "code",
        "created_at": 10,
        "finalization_id": finalization_id,
    }
    intent = module.build_finalization_intent(
        finalization_id=finalization_id,
        identity={"test": True},
        parent_branch=parent,
        child_branch=child,
        accepted_items=[item],
        target_status="accepted",
        merged_candidate_id=None,
    )
    return queue, state, intent


def test_finalization_recovers_after_branch_write(monkeypatch, tmp_path: Path) -> None:
    queue, state, intent = finalization_fixture(tmp_path)
    queue_path = tmp_path / "candidate_queue.json"
    state_path = tmp_path / "branch_state.json"
    journal_path = tmp_path / module.FINALIZATION_FILENAME
    module.write_json(queue_path, queue)
    module.write_json(state_path, state)
    real_write_json = module.write_json
    failed = False

    def fail_queue_once(path, payload):
        nonlocal failed
        if Path(path) == queue_path and not failed:
            failed = True
            raise OSError("injected queue write failure")
        real_write_json(path, payload)

    monkeypatch.setattr(module, "write_json", fail_queue_once)
    with pytest.raises(OSError, match="injected"):
        module.commit_finalization(
            intent=intent,
            journal_path=journal_path,
            queue=queue,
            queue_path=queue_path,
            branch_state=state,
            branch_state_path=state_path,
        )
    assert module.read_json(journal_path)["state"] == "prepared"
    assert module.read_json(state_path)["active_branch_id"] == "baseline+candidate-1"
    assert module.read_json(queue_path)["candidates"][0]["status"] == "accepted_pending_merge"

    monkeypatch.setattr(module, "write_json", real_write_json)
    recovered_queue = module.read_json(queue_path)
    recovered_state = module.read_json(state_path)
    assert module.recover_finalization(
        journal_path=journal_path,
        queue=recovered_queue,
        queue_path=queue_path,
        branch_state=recovered_state,
        branch_state_path=state_path,
    )
    assert recovered_queue["candidates"][0]["status"] == "accepted"
    assert recovered_queue["candidates"][0]["accepted_branch_id"] == "baseline+candidate-1"
    assert len(recovered_state["branches"]) == 2
    assert recovered_state["branches"][1]["created_at"] == 10
    assert module.read_json(journal_path)["state"] == "completed"
    assert not module.recover_finalization(
        journal_path=journal_path,
        queue=recovered_queue,
        queue_path=queue_path,
        branch_state=recovered_state,
        branch_state_path=state_path,
    )
    assert len(recovered_state["branches"]) == 2


def test_finalization_recovers_after_both_state_writes(monkeypatch, tmp_path: Path) -> None:
    queue, state, intent = finalization_fixture(tmp_path)
    queue_path = tmp_path / "candidate_queue.json"
    state_path = tmp_path / "branch_state.json"
    journal_path = tmp_path / module.FINALIZATION_FILENAME
    real_write_json = module.write_json
    journal_writes = 0

    def fail_completion(path, payload):
        nonlocal journal_writes
        if Path(path) == journal_path:
            journal_writes += 1
            if journal_writes == 2:
                raise OSError("injected completion failure")
        real_write_json(path, payload)

    monkeypatch.setattr(module, "write_json", fail_completion)
    with pytest.raises(OSError, match="completion"):
        module.commit_finalization(
            intent=intent,
            journal_path=journal_path,
            queue=queue,
            queue_path=queue_path,
            branch_state=state,
            branch_state_path=state_path,
        )
    assert module.read_json(journal_path)["state"] == "prepared"
    monkeypatch.setattr(module, "write_json", real_write_json)
    recovered_queue = module.read_json(queue_path)
    recovered_state = module.read_json(state_path)
    assert not module.recover_finalization(
        journal_path=journal_path,
        queue=recovered_queue,
        queue_path=queue_path,
        branch_state=recovered_state,
        branch_state_path=state_path,
    )
    assert module.read_json(journal_path)["state"] == "completed"
    assert len(recovered_state["branches"]) == 2


def test_finalization_can_roll_forward_queue_first_state(tmp_path: Path) -> None:
    queue, state, intent = finalization_fixture(tmp_path)
    applied_queue, _ = module._apply_finalization_intent(intent=intent, queue=queue, branch_state=state)
    queue_path = tmp_path / "candidate_queue.json"
    state_path = tmp_path / "branch_state.json"
    journal_path = tmp_path / module.FINALIZATION_FILENAME
    module.write_json(queue_path, applied_queue)
    module.write_json(state_path, state)
    module.write_json(journal_path, intent)
    assert module.recover_finalization(
        journal_path=journal_path,
        queue=applied_queue,
        queue_path=queue_path,
        branch_state=state,
        branch_state_path=state_path,
    )
    assert state["active_branch_id"] == "baseline+candidate-1"
    assert len(state["branches"]) == 2


def test_finalization_rejects_stale_parent_without_mutation(tmp_path: Path) -> None:
    queue, state, intent = finalization_fixture(tmp_path)
    state["branches"][0]["status"] = "superseded"
    state["branches"].append({"branch_id": "other", "status": "active"})
    state["active_branch_id"] = "other"
    before_queue = json.loads(json.dumps(queue))
    before_state = json.loads(json.dumps(state))
    with pytest.raises(ValueError, match="no longer active"):
        module._apply_finalization_intent(intent=intent, queue=queue, branch_state=state)
    assert queue == before_queue
    assert state == before_state


def test_finalization_rejects_conflicting_or_duplicate_bindings(tmp_path: Path) -> None:
    queue, state, intent = finalization_fixture(tmp_path)
    conflicting = dict(intent["child_branch"])
    conflicting["accepted_candidate_id"] = "other"
    state["branches"].append(conflicting)
    state["branches"][0]["status"] = "superseded"
    state["active_branch_id"] = conflicting["branch_id"]
    with pytest.raises(ValueError, match="conflicting child binding"):
        module._apply_finalization_intent(intent=intent, queue=queue, branch_state=state)

    state["branches"].append(dict(conflicting))
    with pytest.raises(ValueError, match="multiple branches"):
        module._apply_finalization_intent(intent=intent, queue=queue, branch_state=state)


def test_finalization_rejects_invalid_queue_transition_without_mutation(tmp_path: Path) -> None:
    queue, state, intent = finalization_fixture(tmp_path)
    queue["candidates"][0]["status"] = "pipeline_failed"
    before_queue = json.loads(json.dumps(queue))
    before_state = json.loads(json.dumps(state))
    with pytest.raises(ValueError, match="expected 'accepted_pending_merge'"):
        module._apply_finalization_intent(intent=intent, queue=queue, branch_state=state)
    assert queue == before_queue
    assert state == before_state


def test_finalization_rejects_occupied_planned_branch_id(tmp_path: Path) -> None:
    queue, state, intent = finalization_fixture(tmp_path)
    state["branches"].append(
        {"branch_id": "baseline+candidate-1", "status": "superseded"}
    )
    before_state = json.loads(json.dumps(state))
    with pytest.raises(ValueError, match="already occupied"):
        module._apply_finalization_intent(intent=intent, queue=queue, branch_state=state)
    assert state == before_state


def test_invalid_finalization_is_not_journaled(tmp_path: Path) -> None:
    queue, state, intent = finalization_fixture(tmp_path)
    state["branches"][0]["status"] = "superseded"
    state["branches"].append({"branch_id": "other", "status": "active"})
    state["active_branch_id"] = "other"
    journal_path = tmp_path / module.FINALIZATION_FILENAME
    with pytest.raises(ValueError, match="no longer active"):
        module.commit_finalization(
            intent=intent,
            journal_path=journal_path,
            queue=queue,
            queue_path=tmp_path / "candidate_queue.json",
            branch_state=state,
            branch_state_path=tmp_path / "branch_state.json",
        )
    assert not journal_path.exists()


def test_workflow_consistency_rejects_dangling_accepted_branch(tmp_path: Path) -> None:
    queue, state, _intent = finalization_fixture(tmp_path)
    queue["candidates"][0].update(
        {"status": "accepted", "accepted_branch_id": "missing"}
    )
    with pytest.raises(ValueError, match="references missing accepted branch"):
        module.validate_workflow_consistency(queue, state)
