"""Mechanism-diverse multi-proposer utilities."""

from .hooks import (
    HOOKS_BY_MECHANISM_FAMILY,
    apply_candidate_values,
    hook_alias_menu,
)
from .multi_proposer import (
    EditableSurface,
    MultiProposerRequest,
    ProposalBundle,
    build_multi_proposer_prompt,
    generate_multi_proposals,
    parse_multi_proposer_response,
)
from .materialize import (
    CandidateManifest,
    SurfaceSpec,
    load_proposal_bundle,
    materialize_candidate,
    proposal_bundle_from_dict,
    surface_spec_from_path,
)

__all__ = [
    "CandidateManifest",
    "EditableSurface",
    "HOOKS_BY_MECHANISM_FAMILY",
    "MultiProposerRequest",
    "ProposalBundle",
    "SurfaceSpec",
    "apply_candidate_values",
    "build_multi_proposer_prompt",
    "generate_multi_proposals",
    "hook_alias_menu",
    "load_proposal_bundle",
    "materialize_candidate",
    "parse_multi_proposer_response",
    "proposal_bundle_from_dict",
    "surface_spec_from_path",
]
