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
    EditableSurface,
    MultiProposerRequest,
    build_multi_proposer_prompt,
    parse_multi_proposer_response,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or parse a mechanism-diverse multi-proposer prompt.")
    parser.add_argument("--diagnosis", required=True, help="Canonical train diagnosis text file.")
    parser.add_argument("--surface", action="append", default=[], help="Surface spec as name=path. Repeatable.")
    parser.add_argument("--route-count", type=int, default=4)
    parser.add_argument("--harness-overview", default="")
    parser.add_argument("--surface-hints", default="")
    parser.add_argument("--current-eval-observations", default="")
    parser.add_argument("--response", help="Optional proposer response JSON to parse and materialize.")
    parser.add_argument("--output", required=True, help="Prompt or parsed bundle output JSON path.")
    args = parser.parse_args()

    surfaces = []
    for item in args.surface:
        name, sep, raw_path = item.partition("=")
        if not sep or not name.strip() or not raw_path.strip():
            raise RuntimeError(f"surface must be name=path, got: {item!r}")
        path = Path(raw_path).expanduser().resolve()
        surfaces.append(
            EditableSurface(
                name=name.strip(),
                kind="workspace_file",
                target=path.name,
                filename=path.name,
                current_value=path.read_text(encoding="utf-8"),
            )
        )
    if not surfaces:
        raise RuntimeError("at least one --surface name=path is required")

    request = MultiProposerRequest(
        diagnosis=Path(args.diagnosis).read_text(encoding="utf-8"),
        surfaces=tuple(surfaces),
        route_count=args.route_count,
        harness_overview=_read_optional_text(args.harness_overview),
        surface_hints=_read_optional_text(args.surface_hints),
        current_eval_observations=_read_optional_text(args.current_eval_observations),
    )
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    if args.response:
        bundles = parse_multi_proposer_response(
            response_text=Path(args.response).read_text(encoding="utf-8"),
            current_values={surface.name: surface.current_value for surface in surfaces},
            require_one=False,
        )
        output.write_text(json.dumps({"proposals": [bundle.to_dict() for bundle in bundles]}, indent=2) + "\n")
    else:
        prompt = build_multi_proposer_prompt(request=request)
        output.write_text(prompt + "\n", encoding="utf-8")
    return 0


def _read_optional_text(value: str) -> str:
    if not value:
        return ""
    path = Path(value).expanduser()
    if path.exists():
        return path.read_text(encoding="utf-8")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
