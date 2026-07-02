#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
QUEUE_FORMAT = "self_harness.candidate_queue.v0"
BRANCH_STATE_FORMAT = "self_harness.branch_state.v0"
DEFAULT_CANDIDATE_ENV_VAR = "SELF_HARNESS_CANDIDATE_WORKSPACE"
BASELINE_BRANCH_ID = "baseline"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the public Self-Harness eval/propose/eval/accept loop.")
    parser.add_argument("--eval-config", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--surface", action="append", default=[], help="Initial surface spec as name=path. Repeatable.")
    parser.add_argument("--route-count", type=int, default=4)
    parser.add_argument("--diagnosis", type=Path, help="Ready-made TB2 diagnosis brief. If omitted, use --diagnosis-command.")
    parser.add_argument("--diagnosis-command", help="Command template that writes {diagnosis}.")
    parser.add_argument("--proposer-response", type=Path, help="Ready-made proposer response JSON.")
    parser.add_argument("--proposer-command", help="Command template that writes {response}.")
    parser.add_argument("--candidate-env-var", default=DEFAULT_CANDIDATE_ENV_VAR)
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=1,
        help="Number of pending candidates to evaluate this run. Use 0 for all pending candidates.",
    )
    parser.add_argument("--reuse-existing", action="store_true", help="Reuse existing stage artifacts when present.")
    args = parser.parse_args(argv)

    if args.max_candidates < 0:
        raise RuntimeError("--max-candidates must be >= 0")

    work_dir = args.work_dir.expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
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
    finalized = finalize_merge_groups(
        queue=queue,
        queue_path=paths.queue,
        branch_state=branch_state,
        branch_state_path=paths.branch_state,
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
    get_active_branch(state)


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
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise RuntimeError(f"surface path does not exist for {name.strip()!r}: {path}")
        parsed[name.strip()] = str(path)
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
    reuse_existing: bool,
) -> Path:
    diagnosis = requested.expanduser().resolve() if requested is not None else branch_paths.diagnosis
    if diagnosis.exists():
        return diagnosis
    if reuse_existing:
        raise RuntimeError(f"diagnosis artifact is missing: {diagnosis}")
    if not command_template:
        raise RuntimeError("diagnosis artifact is missing; pass --diagnosis or --diagnosis-command")
    run_external_template(
        command_template,
        placeholders={
            "diagnosis": diagnosis,
            "work_dir": branch_paths.work_dir,
            "branch_dir": branch_paths.branch_dir,
            "branch_id": branch_paths.branch_id,
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
    if response.exists():
        return response
    if reuse_existing:
        raise RuntimeError(f"proposer response artifact is missing: {response}")
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


def process_pending_candidates(
    *,
    queue: dict[str, Any],
    queue_path: Path,
    branch_state: dict[str, Any],
    branch_state_path: Path,
    eval_config: Path,
    candidate_env_var: str,
    max_candidates: int,
    reuse_existing: bool,
) -> int:
    pending = [item for item in queue["candidates"] if item.get("status") == "pending_eval"]
    if max_candidates:
        pending = pending[:max_candidates]
    processed = 0
    for item in pending:
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
        run_candidate_eval(
            eval_config=eval_config,
            output_dir=eval_dir,
            candidate_dir=candidate_dir,
            candidate_env_var=candidate_env_var,
            reuse_existing=reuse_existing,
        )
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
        write_json(queue_path, queue)
        write_json(branch_state_path, branch_state)
        processed += 1
    finalize_merge_groups(
        queue=queue,
        queue_path=queue_path,
        branch_state=branch_state,
        branch_state_path=branch_state_path,
        eval_config=eval_config,
        candidate_env_var=candidate_env_var,
        reuse_existing=reuse_existing,
    )
    return processed


def finalize_merge_groups(
    *,
    queue: dict[str, Any],
    queue_path: Path,
    branch_state: dict[str, Any],
    branch_state_path: Path,
    eval_config: Path,
    candidate_env_var: str,
    reuse_existing: bool,
) -> int:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in queue["candidates"]:
        if not isinstance(item, dict):
            continue
        if item.get("status") not in {"pending_eval", "accepted_pending_merge", "rejected"}:
            continue
        key = (str(item.get("parent_branch_id") or ""), str(item.get("proposal_bundle") or ""))
        if not key[0] or not key[1]:
            continue
        groups.setdefault(key, []).append(item)

    finalized = 0
    for (_parent_branch_id, _proposal_bundle), items in groups.items():
        if any(item.get("status") == "pending_eval" for item in items):
            continue
        accepted_items = [item for item in items if item.get("status") == "accepted_pending_merge"]
        if not accepted_items:
            continue
        parent_branch = get_branch(branch_state, str(accepted_items[0]["parent_branch_id"]))
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
            )
            item["status"] = "accepted"
            item["accepted_branch_id"] = child_branch["branch_id"]
            finalized += 1
            continue

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
            child_branch = create_branch_from_surfaces(
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
            )
            for item in accepted_items:
                item["status"] = "accepted_merged"
                item["merged_candidate_id"] = merged["candidate_id"]
                item["accepted_branch_id"] = child_branch["branch_id"]
        else:
            for item in accepted_items:
                item["status"] = "accepted_merge_rejected"
                item["merged_candidate_id"] = merged["candidate_id"]
                item["merge_rejection_reason"] = acceptance.get("reason")
        finalized += 1

    if finalized:
        write_json(queue_path, queue)
        write_json(branch_state_path, branch_state)
    return finalized


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
    return create_branch_from_surfaces(
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


def create_branch_from_surfaces(
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
) -> dict[str, Any]:
    branch_id = unique_branch_id(branch_state, branch_id_base)
    for branch in branch_state["branches"]:
        if branch.get("branch_id") == branch_state["active_branch_id"]:
            branch["status"] = "superseded"
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
    }
    if merged_candidate_ids:
        child["merged_candidate_ids"] = merged_candidate_ids
    branch_state["branches"].append(child)
    branch_state["active_branch_id"] = branch_id
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
    run_command(
        [
            sys.executable,
            str(ROOT / "eval" / "scripts" / "run_harbor_eval.py"),
            "--config",
            str(eval_config),
            "--output-dir",
            str(output_dir),
        ],
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
    run_command(shlex.split(formatted))


def run_command(argv: list[str], *, extra_env: dict[str, str] | None = None) -> None:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    print("+ " + shlex.join(argv))
    subprocess.run(argv, cwd=ROOT, env=env, check=True)


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
