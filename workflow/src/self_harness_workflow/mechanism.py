from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from self_harness_acceptance.pareto import decide
from self_harness_workflow.model import FingerprintGate, ModelSpec, OpenAICompatibleAdapter
from self_harness_workflow.policy import PhaseState, canonical_hash, combination_states, validate_split_manifest
from self_harness_workflow.records import FailureRecord, evidence_bundle, exact_signature_clusters

UPSTREAM_COMMIT = "2720dbb3f52283684f4b85a1065d642df1779dd8"
EvalCallback = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


def probe_model(adapter: OpenAICompatibleAdapter) -> dict[str, Any]:
    response = adapter.invoke([{"role": "user", "content": "Reply with exactly: OK"}])
    return {"model": response.model, "model_fingerprint": adapter.fingerprint, "api_style": response.api_style,
            "request_id": response.request_id, "usage": dict(response.usage), "response_hash": response.response_hash}


def parse_structured_response(content: str, *, required: tuple[str, ...]) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("model response must be strict JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("model response JSON must be an object")
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"model response missing fields: {missing}")
    return payload


def run_online_orchestration(
    *, adapter: OpenAICompatibleAdapter, eval_callback: EvalCallback, execution_input: Mapping[str, Any]
) -> dict[str, Any]:
    """Run the formal Evolution/Regression loop. Sealed remains a separate locked mechanism."""
    gate = FingerprintGate()
    fingerprint = adapter.fingerprint
    gate.check("execution", fingerprint)
    execution = adapter.invoke([{"role": "user", "content": json.dumps({"stage": "execution", "input": execution_input})}])
    baseline_eval = dict(eval_callback("baseline", {"execution": execution.content, "model_fingerprint": fingerprint}))
    model_view, baseline_metrics = _baseline_views(baseline_eval)

    gate.check("diagnosis", fingerprint)
    diagnosis_response = adapter.invoke([
        {"role": "system", "content": "Return strict JSON with keys failures and clusters."},
        {"role": "user", "content": json.dumps({"stage": "diagnosis", "evaluation": model_view})},
    ])
    diagnosis = parse_structured_response(diagnosis_response.content, required=("failures", "clusters"))
    if not diagnosis["failures"] and not diagnosis["clusters"]:
        gate.check("proposal", fingerprint)
        gate.assert_complete()
        return {"model_fingerprint": fingerprint, "baseline_metrics": baseline_metrics, "diagnosis": diagnosis,
                "proposals": {"proposals": []}, "validation": {"candidates": []}, "decisions": {},
                "stop_reason": "no_actionable_evolution_failures",
                "request_ids": [execution.request_id, diagnosis_response.request_id]}

    gate.check("proposal", fingerprint)
    proposal_response = adapter.invoke([
        {"role": "system", "content": "Return JSON only in exactly this shape: {\"proposals\":[{\"proposal_id\":\"...\",\"component\":\"...\",\"file\":\"...\",\"surface\":\"...\",\"harness_text\":\"...\"}]}. The harness_text must be a non-empty complete candidate instruction, directly include every required term from the diagnosed Evolution failure cluster, and preserve verification behavior. Do not decline or return an empty proposals list when the diagnosis contains a cluster."},
        {"role": "user", "content": json.dumps({"stage": "proposal", "diagnosis": diagnosis})},
    ])
    proposals = parse_structured_response(proposal_response.content, required=("proposals",))
    gate.assert_complete()
    validation = dict(eval_callback("candidates", {"proposals": proposals["proposals"], "model_fingerprint": fingerprint}))
    decisions = _candidate_decisions(validation, baseline_metrics)
    return {"model_fingerprint": fingerprint, "baseline_metrics": baseline_metrics, "diagnosis": diagnosis,
            "proposals": proposals, "validation": validation, "decisions": decisions,
            "request_ids": [execution.request_id, diagnosis_response.request_id, proposal_response.request_id]}


def _baseline_views(payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    model_view, private_metrics = payload.get("model_view"), payload.get("private_metrics")
    if not isinstance(model_view, dict) or not isinstance(private_metrics, dict):
        raise ValueError("baseline evaluator must return model_view and private_metrics objects")
    if set(model_view) - {"failures", "clusters"}:
        raise ValueError("baseline model_view may contain only failures and clusters")
    if not isinstance(model_view.get("failures"), list) or not isinstance(model_view.get("clusters"), list):
        raise ValueError("baseline model_view must contain failures and clusters lists")
    _split_rates(private_metrics)
    return dict(model_view), dict(private_metrics)


def _candidate_decisions(payload: Mapping[str, Any], baseline_metrics: Mapping[str, Any]) -> dict[str, Any]:
    candidates = payload.get("candidates")
    if candidates == []:
        return {}
    if not isinstance(candidates, list):
        raise ValueError("candidate evaluator must return a candidates list")
    if not candidates:
        raise ValueError("candidate evaluator must return a non-empty candidates list when proposals were emitted")
    baseline = _split_rates(baseline_metrics)
    decisions: dict[str, Any] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("candidate result must be an object")
        required = ("proposal_id", "candidate_hash", "materialized", "executed", "metrics")
        if any(key not in candidate for key in required):
            raise ValueError("candidate result is missing materialization/execution evidence")
        if candidate["materialized"] is not True or candidate["executed"] is not True:
            raise ValueError("candidate must be materialized and executed before acceptance")
        candidate_hash = candidate["candidate_hash"]
        if not isinstance(candidate_hash, str) or len(candidate_hash) != 64:
            raise ValueError("candidate_hash must be a sha256 digest")
        rates = _split_rates(candidate["metrics"])
        decision = decide(rates["evolution"] - baseline["evolution"], rates["regression"] - baseline["regression"])
        decisions[str(candidate["proposal_id"])] = decision.__dict__
    return decisions


def _split_rates(metrics: Mapping[str, Any]) -> dict[str, float]:
    splits = metrics.get("splits") if isinstance(metrics, Mapping) else None
    if not isinstance(splits, Mapping):
        raise ValueError("private metrics must contain split aggregates")
    rates: dict[str, float] = {}
    for split in ("evolution", "regression"):
        aggregate = splits.get(split)
        if not isinstance(aggregate, Mapping):
            raise ValueError(f"private metrics missing {split} aggregate")
        passed, total = aggregate.get("passed"), aggregate.get("total")
        if not isinstance(passed, int) or not isinstance(total, int) or total <= 0 or not 0 <= passed <= total:
            raise ValueError(f"invalid {split} aggregate")
        rates[split] = passed / total
    return rates


def load_eval_callback(path: Path) -> EvalCallback:
    spec = importlib.util.spec_from_file_location("self_harness_eval_plugin", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load eval plugin: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    callback = getattr(module, "evaluate", None)
    if not callable(callback):
        raise ValueError("eval plugin must define evaluate(stage, payload)")
    return callback


def run_offline_demo(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in ("baseline", "weakness_mining", "proposals", "candidates", "validation", "sealed", "logs"):
        (output_dir / name).mkdir(exist_ok=True)
    state, spec, requests = PhaseState(), ModelSpec(), []
    fingerprint = spec.fingerprint("https://offline.invalid/v1")
    gate = FingerprintGate()
    state.complete("baseline")
    for role in ("execution", "diagnosis", "proposal"):
        gate.check(role, fingerprint)
        requests.append({"role": role, "model_fingerprint": fingerprint, "split": "evolution"})
    gate.assert_complete()
    records = [
        FailureRecord("repeat-invalid-command-evo", 1, "evolution", {"steps": 3}, None, {"passed": False},
                      "command_repeated", "retries_without_adaptation", "error_recovery_policy"),
        FailureRecord("artifact-recovery-evo", 1, "evolution", {"steps": 4}, None, {"passed": False},
                      "command_repeated", "retries_without_adaptation", "error_recovery_policy"),
    ]
    clusters = exact_signature_clusters(records, min_support=2)
    state.complete("weakness-mining")
    write_jsonl(output_dir / "weakness_mining/failures.jsonl", [item.to_dict() for item in records])
    write_json(output_dir / "weakness_mining/clusters.json", {"schema_version": "1.0", "clusters": clusters})
    write_json(output_dir / "weakness_mining/evidence_bundle.json", evidence_bundle(clusters[0], records, {"upstream_commit": UPSTREAM_COMMIT}))
    state.complete("propose")
    proposals = [{"proposal_id": "d1", "component": "failure_recovery", "files": ["repo_baseline.py"], "surface": "failure_recovery_instruction"},
                 {"proposal_id": "d2", "component": "runtime_control", "files": ["runtime_policy.py"], "surface": "runtime_control_policy"}]
    write_json(output_dir / "proposals/proposals.json", {"schema_version": "1.0", "proposals": proposals})
    state.complete("validate")
    write_json(output_dir / "validation/single_candidates.json", {"d1": decide(.5, 0).__dict__, "d2": decide(0, .5).__dict__})
    states = combination_states("H", "d1", "d2")
    state.complete("merge-retest")
    write_json(output_dir / "validation/merge_retest.json", {name: decide(.5, .5).__dict__ for name in states})
    state.complete("freeze")
    artifact = {"selected": states[-1], "model_fingerprint": fingerprint, "requests_hash": canonical_hash(requests)}
    write_json(output_dir / "validation/frozen.json", artifact)
    state.complete("sealed-once")
    result = {"schema_version": "1.0", "sealed_status": state.sealed_status,
              "sealed_model_requests_before_freeze": len([r for r in requests if r["split"] == "sealed"]),
              "states_retested": list(states), "frozen_artifact_hash": canonical_hash(artifact)}
    write_json(output_dir / "sealed/result.json", result)
    return result


def load_split_manifest(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return validate_split_manifest(payload["tasks"])


def validate_online_input(payload: Any, assignments: Mapping[str, str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("online workflow input must be a JSON object")
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or task_id not in assignments:
        raise ValueError("online workflow input must name a task_id from the fixed split manifest")
    if assignments[task_id] != "evolution":
        raise ValueError("online workflow permits Evolution tasks only")
    result = dict(payload)
    result.pop("split", None)
    result["task_id"] = task_id
    return result


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in items), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the auditable Self-Harness mechanism state machine")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline-demo", action="store_true")
    mode.add_argument("--probe-model", action="store_true")
    mode.add_argument("--online-workflow", action="store_true", help="Formal Evolution/Regression loop; Sealed is separate")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--eval-plugin", type=Path)
    parser.add_argument("--execution-input", type=Path)
    parser.add_argument("--split-manifest", type=Path)
    args = parser.parse_args(argv)
    if args.offline_demo:
        if args.output_dir is None:
            parser.error("--offline-demo requires --output-dir")
        result = run_offline_demo(args.output_dir.resolve())
    else:
        adapter = OpenAICompatibleAdapter(ModelSpec.from_env())
        if args.probe_model:
            result = probe_model(adapter)
        else:
            if args.eval_plugin is None or args.execution_input is None or args.split_manifest is None:
                parser.error("--online-workflow requires --eval-plugin, --execution-input, and --split-manifest")
            execution_input = validate_online_input(json.loads(args.execution_input.read_text(encoding="utf-8")),
                                                    load_split_manifest(args.split_manifest))
            result = run_online_orchestration(adapter=adapter, eval_callback=load_eval_callback(args.eval_plugin),
                                              execution_input=execution_input)
            if args.output_dir is not None:
                write_json(args.output_dir.resolve() / "online_workflow.json", result)
    print(json.dumps(result, sort_keys=True))
    return 0
