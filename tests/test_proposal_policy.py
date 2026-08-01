from __future__ import annotations

import pytest

from self_harness_proposer.proposal_policy import validate_proposal
from self_harness_workflow.policy import EditBudget


BASE = "def build_failure_recovery_instruction():\n    return 'old'\n"


def payload():
    return {"proposal_id": "recover", "target_cluster": ["missing", "stops", "recovery"],
            "target_failure_type": "missing_artifact", "component": "failure_recovery", "files": ["repo_baseline.py"],
            "surface": "failure_recovery_instruction", "expected_behavior": "retry with changed action",
            "preserved_behavior": "successful commands unchanged", "regression_risk": "extra command"}


def test_proposal_rejects_undeclared_surface():
    with pytest.raises(ValueError, match="undeclared"):
        validate_proposal(payload(), before=BASE, after=BASE.replace("old", "new"),
                          actionable_clusters={("missing", "stops", "recovery")}, allowed_surfaces=set())


def test_proposal_rejects_non_actionable_cluster():
    with pytest.raises(ValueError, match="actionable"):
        validate_proposal(payload(), before=BASE, after=BASE.replace("old", "new"),
                          actionable_clusters=set(), allowed_surfaces={"failure_recovery_instruction"})


def test_proposal_enforces_diff_budget():
    with pytest.raises(ValueError, match="budget"):
        validate_proposal(payload(), before=BASE, after=BASE + "x = 1\n",
                          actionable_clusters={("missing", "stops", "recovery")},
                          allowed_surfaces={"failure_recovery_instruction"}, budget=EditBudget(max_added_chars=1))
