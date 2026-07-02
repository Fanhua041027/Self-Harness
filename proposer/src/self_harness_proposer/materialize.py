from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .multi_proposer import ProposalBundle


@dataclass(frozen=True)
class SurfaceSpec:
    name: str
    filename: str
    base_value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateManifest:
    candidate_id: str
    proposal_id: str
    changed_surfaces: tuple[str, ...]
    proposal_path: str
    variant_path: str
    surface_files: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "proposal_id": self.proposal_id,
            "changed_surfaces": list(self.changed_surfaces),
            "proposal_path": self.proposal_path,
            "variant_path": self.variant_path,
            "surface_files": dict(self.surface_files),
        }


def load_proposal_bundle(path: Path, *, proposal_id: str | None = None) -> ProposalBundle:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping) and isinstance(payload.get("proposals"), list):
        proposals = payload["proposals"]
        if proposal_id is None:
            if len(proposals) != 1:
                raise ValueError("proposal bundle contains multiple proposals; pass --proposal-id")
            raw = proposals[0]
        else:
            matches = [item for item in proposals if isinstance(item, Mapping) and item.get("proposal_id") == proposal_id]
            if len(matches) != 1:
                raise ValueError(f"proposal_id {proposal_id!r} matched {len(matches)} proposals")
            raw = matches[0]
    else:
        raw = payload
    if not isinstance(raw, Mapping):
        raise ValueError("proposal bundle must be an object")
    return proposal_bundle_from_dict(raw)


def proposal_bundle_from_dict(payload: Mapping[str, Any]) -> ProposalBundle:
    values = payload.get("values")
    if not isinstance(values, Mapping):
        raise ValueError("proposal bundle must contain a values object")
    metadata = payload.get("metadata")
    return ProposalBundle(
        proposal_id=str(payload.get("proposal_id") or "proposal"),
        title=str(payload.get("title") or payload.get("proposal_id") or "proposal"),
        selected_cluster=str(payload.get("selected_cluster") or "unknown"),
        selected_surface=str(payload.get("selected_surface") or "unknown"),
        mechanism=str(payload.get("mechanism") or "unknown"),
        why_distinct=str(payload.get("why_distinct") or ""),
        net_gain_hypothesis=str(payload.get("net_gain_hypothesis") or ""),
        regression_guard=str(payload.get("regression_guard") or ""),
        summary=str(payload.get("summary") or ""),
        final_message=str(payload.get("final_message")) if payload.get("final_message") is not None else None,
        values={str(name): str(value) for name, value in values.items()},
        route_prompt=str(payload.get("route_prompt") or ""),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def materialize_candidate(
    *,
    bundle: ProposalBundle,
    output_dir: Path,
    surface_specs: Mapping[str, SurfaceSpec],
    candidate_id: str | None = None,
) -> CandidateManifest:
    candidate_id = safe_slug(candidate_id or bundle.proposal_id)
    if not candidate_id:
        raise ValueError("candidate_id must be non-empty")
    missing = sorted(set(bundle.values) - set(surface_specs))
    if missing:
        raise ValueError(f"missing surface specs for: {', '.join(missing)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    current_dir = output_dir / "current"
    current_dir.mkdir(parents=True, exist_ok=True)

    changed_surfaces: list[str] = []
    surface_files: dict[str, str] = {}
    for name in sorted(bundle.values):
        spec = surface_specs[name]
        relative = Path(spec.filename)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe surface filename for {name!r}: {spec.filename!r}")
        target = current_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        value = bundle.values[name]
        target.write_text(value, encoding="utf-8")
        surface_files[name] = str(Path("current") / relative)
        if spec.base_value is None or value != spec.base_value:
            changed_surfaces.append(name)

    proposal_payload = {
        "proposal_id": bundle.proposal_id,
        "candidate_id": candidate_id,
        "title": bundle.title,
        "selected_cluster": bundle.selected_cluster,
        "selected_surface": bundle.selected_surface,
        "mechanism": bundle.mechanism,
        "why_distinct": bundle.why_distinct,
        "net_gain_hypothesis": bundle.net_gain_hypothesis,
        "regression_guard": bundle.regression_guard,
        "summary": bundle.summary,
        "final_message": bundle.final_message,
        "metadata": dict(bundle.metadata),
    }
    variant_payload = {
        "label": candidate_id,
        "proposal_id": bundle.proposal_id,
        "changed_surfaces": changed_surfaces,
        "surfaces": {name: spec.to_dict() for name, spec in sorted(surface_specs.items()) if name in bundle.values},
        "values": dict(sorted(bundle.values.items())),
        "surface_files": dict(sorted(surface_files.items())),
    }

    write_json(output_dir / "proposal.json", proposal_payload)
    (output_dir / "proposal.md").write_text(bundle.summary.rstrip() + "\n", encoding="utf-8")
    write_json(output_dir / "candidate_variant.json", variant_payload)
    write_json(output_dir / "proposal_bundle.json", bundle.to_dict())

    manifest = CandidateManifest(
        candidate_id=candidate_id,
        proposal_id=bundle.proposal_id,
        changed_surfaces=tuple(changed_surfaces),
        proposal_path="proposal.json",
        variant_path="candidate_variant.json",
        surface_files=dict(sorted(surface_files.items())),
    )
    write_json(output_dir / "manifest.json", manifest.to_dict())
    return manifest


def surface_spec_from_path(*, name: str, path: Path, filename: str | None = None) -> SurfaceSpec:
    return SurfaceSpec(
        name=name,
        filename=filename or path.name,
        base_value=path.read_text(encoding="utf-8"),
    )


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-_.")
