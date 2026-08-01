from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from self_harness_acceptance.pareto import decide
from self_harness_workflow.policy import EditBudget, PhaseState, combination_states, snapshot_files, verify_snapshot
from self_harness_workflow.records import FailureRecord, evidence_bundle, exact_signature_clusters

ROOT = Path(__file__).resolve().parents[1]


def failure(task: str, repeat: int = 1) -> FailureRecord:
    return FailureRecord(task, repeat, "evolution", {"step": 1}, "", {"passed": False}, "missing_artifact", "stops_after_error", "recovery")


def test_single_failure_is_not_actionable():
    cluster = exact_signature_clusters([failure("one")])[0]
    assert not cluster["actionable"]
    with pytest.raises(ValueError, match="low-support"):
        evidence_bundle(cluster, [failure("one")], {})


def test_exact_cluster_requires_cross_task_support():
    records = [failure("one", 1), failure("one", 2), failure("two", 1)]
    cluster = exact_signature_clusters(records)[0]
    assert cluster["actionable"] and cluster["support"] == 3 and cluster["task_support"] == 2


def test_non_evolution_failure_is_rejected():
    with pytest.raises(ValueError, match="Evolution"):
        FailureRecord("x", 1, "regression", {}, None, {}, "a", "b", "c")


@pytest.mark.parametrize("evo,reg,accepted", [(0, 0, False), (1, 0, True), (0, 1, True), (1, 1, True), (-1, 1, False), (1, -1, False)])
def test_pareto_boundaries(evo, reg, accepted):
    assert decide(evo, reg).accepted is accepted


def test_readonly_tamper_detected(tmp_path):
    (tmp_path / "verifier.py").write_text("fixed", encoding="utf-8")
    snapshot = snapshot_files(tmp_path, ["verifier.py"])
    (tmp_path / "verifier.py").write_text("tampered", encoding="utf-8")
    with pytest.raises(RuntimeError, match="readonly"):
        verify_snapshot(tmp_path, snapshot)


def test_sealed_once_and_phase_order():
    state = PhaseState()
    for phase in state.ORDER:
        state.complete(phase)
    with pytest.raises(RuntimeError, match="sealed"):
        state.complete("sealed-once")


def test_combination_orchestration_names_all_states():
    assert combination_states("H", "d1", "d2") == ("H+d1", "H+d2", "H+d1+d2")


def test_online_orchestration_uses_three_roles_and_plugin():
    script = ROOT / "workflow/scripts/run_mechanism_reproduction.py"
    spec = importlib.util.spec_from_file_location("mechanism_online", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)

    class Response:
        def __init__(self, content, request_id):
            self.content, self.request_id = content, request_id

    class Adapter:
        fingerprint = "f" * 64

        def __init__(self):
            self.calls = []

        def invoke(self, messages):
            self.calls.append(messages)
            index = len(self.calls)
            contents = ["execution output", '{"failures": [{"failure_type": "missing_rule"}], "clusters": [{"signature": ["missing_rule", "stops_early", "runtime_control"], "support": 2}]}', '{"proposals": [{"proposal_id": "p1", "surface": "text", "harness_text": "recover artifact retry adapt verify"}]}']
            return Response(contents[index - 1], f"r{index}")

    stages = []
    def evaluate(stage, payload):
        stages.append(stage)
        if stage == "baseline":
            return {"model_view": {"failures": [{"failure_type": "missing_rule"}],
                                   "clusters": [{"signature": ["missing_rule", "stops_early", "runtime_control"], "support": 2}]},
                    "private_metrics": {"splits": {"evolution": {"passed": 0, "total": 2}, "regression": {"passed": 0, "total": 1}}}}
        return {"candidates": [{"proposal_id": "p1", "candidate_hash": "a" * 64, "materialized": True,
                                "executed": True, "metrics": {"splits": {"evolution": {"passed": 2, "total": 2},
                                                                            "regression": {"passed": 1, "total": 1}}}}]}

    adapter = Adapter()
    result = module.run_online_orchestration(adapter=adapter, eval_callback=evaluate,
                                              execution_input={"split": "evolution", "task": "smoke"})
    assert len(adapter.calls) == 3
    assert stages == ["baseline", "candidates"]
    assert result["diagnosis"]["failures"] == [{"failure_type": "missing_rule"}]
    assert result["diagnosis"]["clusters"][0]["support"] == 2
    assert result["decisions"]["p1"]["accepted"] is True
    assert result["request_ids"] == ["r1", "r2", "r3"]


def test_online_orchestration_stops_cleanly_without_evolution_failures():
    script = ROOT / "workflow/scripts/run_mechanism_reproduction.py"
    spec = importlib.util.spec_from_file_location("mechanism_empty", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)

    class Response:
        def __init__(self, content, request_id):
            self.content, self.request_id = content, request_id

    class Adapter:
        fingerprint = "f" * 64
        def __init__(self):
            self.calls = []
        def invoke(self, messages):
            self.calls.append(messages)
            return Response("execution output" if len(self.calls) == 1 else '{"failures": [], "clusters": []}', "r")

    def evaluate(stage, payload):
        assert stage == "baseline"
        return {"model_view": {"failures": [], "clusters": []},
                "private_metrics": {"splits": {"evolution": {"passed": 2, "total": 2},
                                                   "regression": {"passed": 0, "total": 1}}}}

    adapter = Adapter()
    result = module.run_online_orchestration(adapter=adapter, eval_callback=evaluate,
                                              execution_input={"task_id": "artifact-recovery-evo"})
    assert result["stop_reason"] == "no_actionable_evolution_failures"
    assert result["proposals"] == {"proposals": []}
    assert len(adapter.calls) == 2



def test_offline_demo_is_end_to_end_and_sealed_isolated(tmp_path):
    script = ROOT / "workflow/scripts/run_mechanism_reproduction.py"
    spec = importlib.util.spec_from_file_location("mechanism", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    result = module.run_offline_demo(tmp_path)
    assert result["sealed_status"] == "completed"
    assert result["sealed_model_requests_before_freeze"] == 0
    assert result["states_retested"] == ["H+d1", "H+d2", "H+d1+d2"]
    assert (tmp_path / "weakness_mining/failures.jsonl").exists()
