#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from self_harness_proposer import (  # noqa: E402
    load_proposal_bundle,
    materialize_candidate,
    surface_spec_from_path,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize a proposer bundle into a candidate workspace.")
    parser.add_argument("--bundle", required=True, type=Path, help="Parsed proposer bundle JSON.")
    parser.add_argument("--proposal-id", help="Proposal id to select when the bundle contains multiple proposals.")
    parser.add_argument("--surface", action="append", default=[], help="Surface spec as name=path. Repeatable.")
    parser.add_argument(
        "--surface-filename",
        action="append",
        default=[],
        help="Optional output filename override as name=relative/path.py. Repeatable.",
    )
    parser.add_argument("--candidate-id", help="Candidate label. Defaults to proposal_id.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Candidate workspace output directory.")
    args = parser.parse_args()

    filename_overrides = parse_name_map(args.surface_filename, field="surface-filename")
    surface_paths = parse_name_map(args.surface, field="surface")
    if not surface_paths:
        raise RuntimeError("at least one --surface name=path is required")

    surface_specs = {}
    for name, raw_path in surface_paths.items():
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise RuntimeError(f"surface path does not exist for {name!r}: {path}")
        surface_specs[name] = surface_spec_from_path(
            name=name,
            path=path,
            filename=filename_overrides.get(name),
        )

    bundle = load_proposal_bundle(args.bundle.expanduser().resolve(), proposal_id=args.proposal_id)
    manifest = materialize_candidate(
        bundle=bundle,
        output_dir=args.output_dir.expanduser().resolve(),
        surface_specs=surface_specs,
        candidate_id=args.candidate_id,
    )
    print(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    return 0


def parse_name_map(items: list[str], *, field: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not name.strip() or not value.strip():
            raise RuntimeError(f"{field} must be name=value, got: {item!r}")
        parsed[name.strip()] = value.strip()
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
