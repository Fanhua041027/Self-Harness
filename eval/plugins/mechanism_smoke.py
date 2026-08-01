from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "eval" / "fixtures" / "mechanism_smoke_cases.json"


def evaluate(stage: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Evaluate only non-Sealed smoke cases; Regression details stay plugin-private."""
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    if any(case["split"] == "sealed" for case in cases):
        raise ValueError("mechanism smoke evaluator must not load Sealed cases")
    if stage == "baseline":
        execution = str(payload.get("execution") or "")
        results = [_score_case(case, execution) for case in cases]
        evolution_failures = [
            {"failure_type": "missing_required_terms", "mechanism": "textual_harness_instruction",
             "required_terms": result["missing_terms"]}
            for result in results if result["split"] == "evolution" and not result["passed"]
        ]
        clusters = [] if not evolution_failures else [{"signature": ["missing_required_terms", "stops_early", "textual_harness_instruction"],
                                                        "support": len(evolution_failures)}]
        return {"model_view": {"failures": evolution_failures, "clusters": clusters},
                "private_metrics": {"splits": _aggregates(results)}}
    if stage == "candidates":
        proposals = payload.get("proposals")
        if not isinstance(proposals, list):
            raise ValueError("candidate evaluation requires a proposals list")
        candidates = []
        for proposal in proposals:
            if not isinstance(proposal, Mapping):
                raise ValueError("proposal must be an object")
            harness_text = proposal.get("harness_text")
            surface = proposal.get("surface")
            if not isinstance(harness_text, str) or not harness_text.strip() or not isinstance(surface, str) or not surface:
                raise ValueError("proposal must declare surface and concrete harness_text")
            results = [_score_case(case, harness_text) for case in cases]
            candidates.append({
                "proposal_id": str(proposal.get("proposal_id") or ""),
                "surface": surface,
                "candidate_hash": hashlib.sha256(harness_text.encode("utf-8")).hexdigest(),
                "materialized": True,
                "executed": True,
                "metrics": {"splits": _aggregates(results)},
            })
        return {"candidates": candidates}
    raise ValueError(f"unsupported evaluation stage: {stage}")


def _score_case(case: Mapping[str, Any], artifact: str) -> dict[str, Any]:
    required = [str(item).lower() for item in case["required_terms"]]
    normalized = artifact.lower()
    return {"split": case["split"], "passed": all(term in normalized for term in required),
            "missing_terms": [term for term in required if term not in normalized]}


def _aggregates(results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    by_split: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = by_split.setdefault(str(result["split"]), {"passed": 0, "total": 0})
        bucket["total"] += 1
        bucket["passed"] += int(result["passed"])
    return by_split
