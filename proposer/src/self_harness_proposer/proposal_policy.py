from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from self_harness_workflow.policy import EditBudget, validate_candidate_edit


@dataclass(frozen=True)
class ValidatedProposal:
    proposal_id: str
    component: str
    file: str
    surface: str
    edit_metrics: Mapping[str, Any]


def validate_proposal(
    payload: Mapping[str, Any], *, before: str, after: str, actionable_clusters: set[tuple[str, str, str]],
    allowed_surfaces: set[str], budget: EditBudget = EditBudget(),
) -> ValidatedProposal:
    required = ("proposal_id", "target_cluster", "target_failure_type", "component", "files", "surface",
                "expected_behavior", "preserved_behavior", "regression_risk")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise ValueError(f"proposal missing required fields: {missing}")
    cluster = tuple(str(item) for item in payload["target_cluster"])
    if len(cluster) != 3 or cluster not in actionable_clusters:
        raise ValueError("proposal target must be an actionable exact-signature cluster")
    files = payload["files"]
    if not isinstance(files, list) or len(files) != 1:
        raise ValueError("proposal must change exactly one file")
    surface = str(payload["surface"])
    if surface not in allowed_surfaces:
        raise ValueError(f"undeclared harness surface: {surface}")
    metrics = validate_candidate_edit(before=before, after=after, changed_files=1, changed_surfaces=1, budget=budget)
    return ValidatedProposal(str(payload["proposal_id"]), str(payload["component"]), str(files[0]), surface, metrics)
