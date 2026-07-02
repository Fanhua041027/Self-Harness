from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class DiagnosisOutcome:
    case_id: str
    split: str
    stratum: str
    status: str
    failure_message: str | None = None
    artifacts_dir: str | None = None
    messages_path: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"


DiagnosisLoader = Callable[[DiagnosisOutcome], dict[str, Any] | None]
VerifierCausalSignature = tuple[str, str, str]


def write_verifier_causal_brief(
    *,
    outcomes: list[DiagnosisOutcome],
    load_diagnosis: DiagnosisLoader,
    output_path: Path,
) -> Path:
    clusters = build_verifier_causal_clusters(outcomes=outcomes, load_diagnosis=load_diagnosis)
    lines = render_verifier_causal_brief(outcomes=outcomes, clusters=clusters)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def build_verifier_causal_clusters(
    *,
    outcomes: list[DiagnosisOutcome],
    load_diagnosis: DiagnosisLoader,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for outcome in outcomes:
        if outcome.passed:
            continue
        diagnosis = load_diagnosis(outcome)
        if not isinstance(diagnosis, dict):
            raise RuntimeError(f"missing diagnosis for failed case: {outcome.case_id}")
        signature = _verifier_causal_signature(outcome=outcome, diagnosis=diagnosis)
        records.append({"outcome": outcome, "diagnosis": diagnosis, "signature": signature})
    grouped: dict[VerifierCausalSignature, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["signature"]].append(record)
    clusters = [
        {"signature": signature, "records": grouped_records}
        for signature, grouped_records in grouped.items()
    ]
    return sorted(clusters, key=_cluster_sort_key)


def render_verifier_causal_brief(
    *,
    outcomes: list[DiagnosisOutcome],
    clusters: list[dict[str, Any]],
) -> list[str]:
    passing = [outcome.case_id for outcome in outcomes if outcome.passed]
    failing = [outcome.case_id for outcome in outcomes if not outcome.passed]
    lines = [
        "# Verifier-Causal Integrated Trace Diagnosis Brief",
        "",
        "Use this file as the canonical diagnosis brief for the proposer.",
        "This diagnosis uses LLM-generated terminal cause, criticality, and agent-side mechanism fields.",
        "Proposal surfaces, hook names, patch text, and implementation plans are selected by the proposer.",
        "",
        "## Current Harness Contract",
        "",
        "- Passing cases are regression tests.",
        "- Select one high-confidence terminal-cause cluster unless multiple clusters clearly share one causal mechanism.",
        "- Do not treat a large terminal bucket as actionable by itself; inspect agent mechanism and evidence.",
        "- If no safe, reusable harness change follows from the evidence, prefer no-op.",
        "",
        "## Passing Cases To Preserve",
        "",
    ]
    lines.extend([f"- `{case_id}`" for case_id in passing] or ["- None"])
    lines.extend(
        [
            "",
            "## Cross-Case Failure Clusters",
            "",
            f"- Failed cases analyzed: `{len(failing)}`",
            f"- Cluster count: `{len(clusters)}`",
            "- Signature format: `terminal_cause / criticality / agent_mechanism`.",
            "",
        ]
    )
    lines.extend(_diagnosis_focus_lines(clusters))
    for index, cluster in enumerate(clusters, start=1):
        lines.extend(_format_cluster(index=index, cluster=cluster))
    return lines


def _verifier_causal_signature(*, outcome: DiagnosisOutcome, diagnosis: dict[str, Any]) -> VerifierCausalSignature:
    primary = _primary_analysis_item(diagnosis)
    if primary is None:
        raise RuntimeError(f"diagnosis has no primary LLM causal analysis item: {outcome.case_id}")
    return (
        _required_signature_field(primary, "terminal_cause", outcome.case_id),
        _required_signature_field(primary, "criticality", outcome.case_id),
        _required_signature_field(primary, "agent_mechanism", outcome.case_id),
    )


def _required_signature_field(item: dict[str, Any], key: str, case_id: str) -> str:
    value = str(item.get(key, "") or "").strip()
    if not value:
        raise RuntimeError(f"diagnosis primary analysis item missing {key}: {case_id}")
    return value


def _format_cluster(*, index: int, cluster: dict[str, Any]) -> list[str]:
    records = cluster["records"]
    terminal_cause, criticality, mechanism = cluster["signature"]
    strata = sorted({record["outcome"].stratum for record in records})
    lines = [
        f"### Cluster {index}: {terminal_cause.replace('_', ' ')} / {mechanism.replace('_', ' ')}",
        "",
        f"- Cases: `{len(records)}`",
        f"- Signature: `{terminal_cause} / {criticality} / {mechanism}`",
        f"- Strata: {', '.join(f'`{item}`' for item in strata)}",
        f"- Terminal cause: `{terminal_cause}`",
        f"- Criticality: `{criticality}`",
        f"- Agent mechanism: `{mechanism}`",
    ]
    shared = _representative_reasoning(records)
    if shared:
        lines.append(f"- Shared diagnosis: {shared}")
    lines.append("- Representative cases:")
    for record in records[:5]:
        outcome = record["outcome"]
        lines.append(f"  - `{outcome.case_id}`")
        evidence = _representative_evidence(record.get("diagnosis") or {})
        if evidence:
            lines.append("    - Boundary evidence:")
            lines.extend(f"      - {item}" for item in evidence)
    if len(records) > 5:
        lines.append(f"  - ... {len(records) - 5} more case(s) in this cluster")
    lines.append(f"- Proposer focus: {_cluster_guidance(cluster['signature'])}")
    lines.append("")
    return lines


def _diagnosis_focus_lines(clusters: list[dict[str, Any]]) -> list[str]:
    if not clusters:
        return []
    counts = Counter(signature[0] for signature in (cluster["signature"] for cluster in clusters))
    lines = ["## Diagnosis Focus", ""]
    lines.append(
        "- Terminal cause distribution: "
        + ", ".join(f"`{name}`={count}" for name, count in counts.most_common())
    )
    actionable = [
        cluster
        for cluster in clusters
        if cluster["signature"][1] in {"root_cause", "contributor", "unknown"}
    ]
    if actionable:
        sig = actionable[0]["signature"]
        lines.append(f"- Highest-priority cluster: `{sig[0]} / {sig[1]} / {sig[2]}`")
    lines.append("")
    return lines


def _cluster_sort_key(cluster: dict[str, Any]) -> tuple[int, int, str]:
    criticality_rank = {"root_cause": 0, "contributor": 1, "unknown": 2, "non_terminal_friction": 3, "recovered_friction": 4}
    terminal_cause, criticality, mechanism = cluster["signature"]
    return (criticality_rank.get(criticality, 2), -len(cluster["records"]), f"{terminal_cause}/{mechanism}")


def _cluster_guidance(signature: VerifierCausalSignature) -> str:
    _terminal_cause, criticality, _mechanism = signature
    if criticality == "recovered_friction":
        return "Recovered friction is evidence, not a repair target unless representative cases show terminal linkage."
    return "Choose a harness-level change only if this LLM-diagnosed mechanism is reusable across the listed cases."


def _primary_analysis_item(diagnosis: dict[str, Any]) -> dict[str, Any] | None:
    items = [
        item
        for item in diagnosis.get("analysis", [])
        if isinstance(item, dict)
    ]
    if not items:
        return None
    rank = {"root_cause": 0, "contributor": 1, "unknown": 2, "friction": 3, "noise": 4}
    link = {"direct": 0, "indirect": 1, "unknown": 2, "weak": 3, "none": 4}
    return sorted(
        items,
        key=lambda item: (
            0 if item.get("incorrect_step_ids") else 1,
            rank.get(str(item.get("causal_weight", "unknown")), 2),
            link.get(str(item.get("terminal_link", "unknown")), 2),
            int(item.get("stage_id", 999999) or 999999),
        ),
    )[0]


def _representative_reasoning(records: list[dict[str, Any]]) -> str:
    for record in records:
        diagnosis = record.get("diagnosis")
        if not isinstance(diagnosis, dict):
            continue
        primary = _primary_analysis_item(diagnosis)
        if primary is not None and str(primary.get("reasoning", "")).strip():
            return str(primary["reasoning"]).strip()
        summary = str(diagnosis.get("causal_summary", "") or "").strip()
        if summary:
            return summary
    return ""


def _representative_evidence(diagnosis: dict[str, Any]) -> list[str]:
    evidence = diagnosis.get("verifier_evidence")
    lines: list[str] = []
    if isinstance(evidence, dict):
        for key in ("terminal_summary", "reward_text"):
            value = str(evidence.get(key, "") or "").strip()
            if value:
                lines.append(f"{key}: {value}")
        failed_tests = evidence.get("failed_tests")
        if isinstance(failed_tests, list):
            for item in failed_tests[:3]:
                if isinstance(item, dict):
                    excerpt = str(item.get("trace_excerpt", "") or "").strip()
                    if excerpt:
                        lines.append(excerpt)
        snippets = evidence.get("failure_snippets")
        if isinstance(snippets, list):
            lines.extend(str(item) for item in snippets[:3] if str(item).strip())
    return _dedupe(lines)[:6]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
