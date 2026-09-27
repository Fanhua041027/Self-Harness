from __future__ import annotations

from paper import audit_confirmation_design


def test_canonical_sealed21_resolution_is_auditable() -> None:
    audit = audit_confirmation_design.build_audit(tasks=21, bootstrap_samples=2_000)

    assert audit["outcome_blind"] is True
    assert audit["smallest_exact_two_sided_p"] == 2 / (2**21)
    assert "Hyndman-Fan type 7" in audit["bootstrap_percentile_method"]
    assert audit["minimum_canonical_success"]["favorable_task_clusters"] == 6
    assert audit["minimum_canonical_success"]["attempt_level_delta"] == 6 / 42
    assert audit["primary_endpoint_alpha"] == 0.05
    assert audit["bootstrap_seed"] == 20260812


def test_design_audit_uses_preregistered_alpha() -> None:
    prereg = {
        "primary_endpoint": {"alpha": 0.10, "success_rule": "custom"},
        "statistical_design": {"bootstrap": {"seed": 20260812}},
    }
    audit = audit_confirmation_design.build_audit(
        tasks=21, bootstrap_samples=2_000, preregistration=prereg
    )
    assert audit["primary_endpoint_alpha"] == 0.10
    assert audit["primary_success_rule"] == "custom"
