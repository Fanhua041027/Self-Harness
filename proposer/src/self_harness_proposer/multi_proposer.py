from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from .hooks import HOOKS_BY_MECHANISM_FAMILY, apply_candidate_values, canonical_hook_name

_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


@dataclass(frozen=True)
class EditableSurface:
    name: str
    kind: str
    target: str
    current_value: str
    filename: str | None = None

    def to_prompt_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "kind": self.kind,
            "target": self.target,
            "filename": self.filename,
            "current_value": self.current_value,
        }


@dataclass(frozen=True)
class ProposalBundle:
    proposal_id: str
    title: str
    selected_cluster: str
    selected_surface: str
    mechanism: str
    why_distinct: str
    net_gain_hypothesis: str
    regression_guard: str
    summary: str
    final_message: str | None
    values: dict[str, str]
    route_prompt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MultiProposerRequest:
    diagnosis: str
    surfaces: tuple[EditableSurface, ...]
    route_count: int = 4
    harness_overview: str = ""
    surface_hints: str = ""
    current_eval_observations: str = ""
    strict_noop: bool = True
    max_slot_attempts: int = 1
    baseline_surface_name: str = "baseline"


def build_multi_proposer_prompt(
    *,
    request: MultiProposerRequest,
    slot: int | None = None,
    total_slots: int | None = None,
    existing_bundles: Sequence[ProposalBundle] = (),
    retry_errors: Sequence[str] = (),
) -> str:
    existing_payload = [
        {
            "proposal_id": bundle.proposal_id,
            "selected_cluster": bundle.selected_cluster,
            "selected_surface": bundle.selected_surface,
            "mechanism": bundle.mechanism,
            "mechanism_family": bundle.metadata.get("mechanism_family"),
            "exact_hook": bundle.metadata.get("exact_hook"),
            "summary": bundle.summary[:1000],
        }
        for bundle in existing_bundles
    ]
    slot_note = ""
    if slot is not None and total_slots is not None:
        slot_note = (
            f"\nThis call generates proposal slot {slot} of {total_slots}. "
            "Return exactly one proposal in the proposals array. It must be materially distinct "
            "from the already generated proposals below.\n"
        )
    retry_note = ""
    if retry_errors:
        retry_note = "\nPrevious invalid attempts for this slot:\n" + "\n".join(
            f"- {error}" for error in retry_errors
        )
    no_op_requirement = (
        "- No-op, unchanged, placeholder, or fallback proposals are invalid. Use an evidence-backed decline "
        "only when `selection_decision` is `decline`, `candidate_values` is empty, and `decline_reason` plus "
        "`skipped_clusters` explains why no safe reusable harness mechanism should be evaluated."
        if request.strict_noop
        else "- If no credible change exists, emit an evidence-backed decline rather than duplicating another proposal."
    )
    return f"""
You are a mechanism-diverse multi-proposer for a self-harness experiment.
Generate exactly {request.route_count if slot is None else 1} mutually distinct candidate proposals from the shared train-side diagnosis.
Use only train-side evidence. Do not use or mention holdout/private/test-set evidence.
{slot_note}

Return compact strict JSON only with this shape:
{{
  "proposals": [
    {{
      "proposal_id": "short_slug",
      "title": "short title",
      "selection_decision": "eval_candidate|decline",
      "selected_cluster_id": "literal diagnosis cluster/evidence id, or none for decline",
      "selected_cluster": "diagnosis cluster or evidence family",
      "selected_surface": "surface name or virtual surface family",
      "mechanism": "targeted reusable agent/harness mechanism",
      "mechanism_family": "prompt_instruction|subagent|skill_procedure|tool_configuration|middleware|runtime_control|permission_interrupt",
      "exact_hook": "one exact hook from the virtual alias menu, or none for decline",
      "mechanism_to_hook_rationale": "why this mechanism family maps to this exact hook",
      "why_not_noop": "why this slot has enough evidence to evaluate, or empty for decline",
      "decline_reason": "",
      "skipped_clusters": ["larger or riskier diagnosis clusters skipped and why"],
      "expected_affected_cases": ["train case ids expected to improve"],
      "protected_passing_cases": ["passing train cases or behavior to preserve"],
      "why_distinct": "why this candidate is materially different from every other proposal",
      "net_gain_hypothesis": "how this should gain passes",
      "regression_guard": "what passing behavior must be preserved",
      "summary": "proposal markdown with evidence, hook, hypothesis, and regression risk",
      "final_message": "brief implementation summary",
      "candidate_values": {{"<exact_hook>": "JSON-compatible hook value"}}
    }}
  ]
}}

Hard requirements:
- Return JSON only. Do not append candidate_value fenced blocks, diffs, logs, copied diagnosis, or copied surface content.
- Use `candidate_values` with exactly one known virtual hook alias for eval candidates. Declines must omit or empty `candidate_values`.
- `exact_hook` must match the single `candidate_values` key and must belong to the chosen `mechanism_family`.
- Mechanism families and hooks are first-class editable mechanisms, not comments inside a broad prompt rewrite.
{no_op_requirement}
{retry_note}

# Already Generated Proposals In This Round
{json.dumps(existing_payload, indent=2)}

# Harness Overview
{request.harness_overview}

# Surface Hints
{request.surface_hints}

# Current Eval Observations
{request.current_eval_observations}

# Editable Surfaces
{json.dumps([surface.to_prompt_dict() for surface in request.surfaces], indent=2)}

# Canonical Train Diagnosis
{request.diagnosis}
""".strip()


def parse_multi_proposer_response(
    *,
    response_text: str,
    current_values: Mapping[str, str],
    baseline_surface_name: str = "baseline",
    require_one: bool = True,
) -> list[ProposalBundle]:
    payload = _extract_json_object(response_text)
    raw_proposals = payload.get("proposals") if isinstance(payload, Mapping) else None
    if not isinstance(raw_proposals, list):
        raise ValueError("proposer response must contain a proposals list")
    if require_one and len(raw_proposals) != 1:
        raise ValueError("slot proposer response must contain exactly one proposal")
    return [
        _parse_one_proposal(
            item=item,
            current_values=current_values,
            baseline_surface_name=baseline_surface_name,
            fallback_index=index + 1,
        )
        for index, item in enumerate(raw_proposals)
        if isinstance(item, Mapping)
    ]


def generate_multi_proposals(*, llm: Any, request: MultiProposerRequest) -> list[ProposalBundle]:
    bundles: list[ProposalBundle] = []
    seen: set[tuple[str, str, str]] = set()
    for slot in range(1, request.route_count + 1):
        errors: list[str] = []
        for attempt in range(1, max(1, request.max_slot_attempts) + 1):
            prompt = build_multi_proposer_prompt(
                request=request,
                slot=slot,
                total_slots=request.route_count,
                existing_bundles=bundles,
                retry_errors=errors,
            )
            started = time.monotonic()
            try:
                response = llm.invoke([{"role": "user", "content": prompt}])
                response_text = _message_text(response)
                parsed = parse_multi_proposer_response(
                    response_text=response_text,
                    current_values={surface.name: surface.current_value for surface in request.surfaces},
                    baseline_surface_name=request.baseline_surface_name,
                    require_one=True,
                )
                bundle = parsed[0]
                signature = _proposal_signature(bundle)
                if bundle.proposal_id in {existing.proposal_id for existing in bundles}:
                    raise ValueError(f"duplicate proposal_id: {bundle.proposal_id}")
                if signature in seen:
                    raise ValueError(f"duplicate proposal route: {signature}")
                seen.add(signature)
                bundles.append(bundle)
                break
            except Exception as exc:
                duration = time.monotonic() - started
                errors.append(f"attempt {attempt} ({duration:.1f}s): {exc}")
        else:
            raise RuntimeError(f"could not produce valid proposal slot {slot}: {'; '.join(errors)}")
    return bundles


def _parse_one_proposal(
    *,
    item: Mapping[str, Any],
    current_values: Mapping[str, str],
    baseline_surface_name: str,
    fallback_index: int,
) -> ProposalBundle:
    metadata = _metadata_from_item(item)
    decision = str(metadata.get("selection_decision") or "").strip().lower()
    candidate_values = item.get("candidate_values")
    if candidate_values is None:
        candidate_values = {}
    if not isinstance(candidate_values, Mapping):
        raise ValueError("candidate_values must be an object")

    if decision == "decline":
        if candidate_values:
            raise ValueError("decline proposals must not change candidate_values")
        values = dict(current_values)
    else:
        _validate_mechanism_contract(metadata=metadata, candidate_values=candidate_values)
        values = apply_candidate_values(
            current_values=current_values,
            candidate_values=candidate_values,
            baseline_surface_name=baseline_surface_name,
        )
        if values == dict(current_values):
            raise ValueError("eval candidate did not change any surface value")

    proposal_id = _normalize_identifier(str(item.get("proposal_id") or f"proposal_{fallback_index:02d}"))
    return ProposalBundle(
        proposal_id=proposal_id,
        title=str(item.get("title") or proposal_id),
        selected_cluster=str(item.get("selected_cluster") or metadata.get("selected_cluster_id") or "unknown"),
        selected_surface=str(item.get("selected_surface") or metadata.get("exact_hook") or "unknown"),
        mechanism=str(item.get("mechanism") or "unknown"),
        why_distinct=str(item.get("why_distinct") or ""),
        net_gain_hypothesis=str(item.get("net_gain_hypothesis") or ""),
        regression_guard=str(item.get("regression_guard") or ""),
        summary=str(item.get("summary") or ""),
        final_message=str(item.get("final_message")) if item.get("final_message") is not None else None,
        values=values,
        route_prompt=str(item.get("route_prompt") or ""),
        metadata=metadata,
    )


def _metadata_from_item(item: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), Mapping) else {}
    for key in (
        "selection_decision",
        "selected_cluster_id",
        "mechanism_family",
        "exact_hook",
        "mechanism_to_hook_rationale",
        "why_not_noop",
        "decline_reason",
        "skipped_clusters",
        "expected_affected_cases",
        "protected_passing_cases",
    ):
        if key in item:
            metadata[key] = item[key]
    return metadata


def _validate_mechanism_contract(*, metadata: Mapping[str, Any], candidate_values: Mapping[str, Any]) -> None:
    if len(candidate_values) != 1:
        raise ValueError("eval candidates must include exactly one candidate_values hook")
    raw_hook = next(iter(candidate_values))
    exact_hook = canonical_hook_name(str(metadata.get("exact_hook") or raw_hook))
    changed_hook = canonical_hook_name(str(raw_hook))
    if exact_hook is None:
        raise ValueError("exact_hook must name a known virtual hook")
    if changed_hook != exact_hook:
        raise ValueError(f"exact_hook {exact_hook!r} must match candidate_values key {raw_hook!r}")
    mechanism_family = str(metadata.get("mechanism_family") or "").strip()
    if mechanism_family not in HOOKS_BY_MECHANISM_FAMILY:
        raise ValueError(f"unknown mechanism_family: {mechanism_family!r}")
    if exact_hook not in HOOKS_BY_MECHANISM_FAMILY[mechanism_family]:
        raise ValueError(f"hook {exact_hook!r} is not allowed for mechanism_family {mechanism_family!r}")
    for required in ("mechanism_to_hook_rationale", "why_not_noop"):
        if not str(metadata.get(required) or "").strip():
            raise ValueError(f"{required} must be non-empty")


def _proposal_signature(bundle: ProposalBundle) -> tuple[str, str, str]:
    return (
        _normalize_identifier(str(bundle.metadata.get("selected_cluster_id") or bundle.selected_cluster)),
        _normalize_identifier(str(bundle.metadata.get("mechanism_family") or "")),
        _normalize_identifier(str(bundle.metadata.get("exact_hook") or bundle.selected_surface)),
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(text)
        if match is None:
            raise ValueError("response did not contain a JSON object") from None
        payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("response JSON must be an object")
    return payload


def _message_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    return str(response)


def _normalize_identifier(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return cleaned or "proposal"
