from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from paper import audit_split_coverage


def fixture_metadata() -> dict[str, dict[str, str]]:
    return {
        "train-a": {"category": "a", "difficulty": "easy"},
        "train-b": {"category": "b", "difficulty": "hard"},
        "held-a": {"category": "a", "difficulty": "hard"},
        "held-b": {"category": "b", "difficulty": "easy"},
        "sealed-a": {"category": "a", "difficulty": "easy"},
        "sealed-c": {"category": "c", "difficulty": "hard"},
        "media": {"category": "b", "difficulty": "hard"},
    }


def test_build_audit_accounts_for_tasks_and_quantifies_shift() -> None:
    metadata = fixture_metadata()
    audit = audit_split_coverage.build_audit(
        clean={"train": {"train-a", "train-b"}, "heldout": {"held-a", "held-b"}},
        sealed={"sealed-a", "sealed-c"},
        excluded={"media"},
        universe=set(metadata),
        metadata=metadata,
    )

    assert audit["task_accounting"]["universe"] == 7
    assert audit["task_accounting"]["clean_sealed_overlap"] == 0
    assert audit["clean64_vs_sealed21"]["category"]["total_variation"] == 0.5
    assert audit["clean64_vs_sealed21"]["categories_missing_from_sealed"] == ["b"]
    assert audit["clean64_vs_sealed21"]["categories_novel_in_sealed"] == ["c"]


def test_build_audit_rejects_unaccounted_task() -> None:
    metadata = fixture_metadata()
    with pytest.raises(ValueError, match="task accounting mismatch"):
        audit_split_coverage.build_audit(
            clean={"train": {"train-a"}, "heldout": set()},
            sealed={"sealed-a"},
            excluded=set(),
            universe=set(metadata),
            metadata=metadata,
        )


def test_svg_geometry_stays_inside_canvas(tmp_path) -> None:
    metadata = fixture_metadata()
    audit = audit_split_coverage.build_audit(
        clean={"train": {"train-a", "train-b"}, "heldout": {"held-a", "held-b"}},
        sealed={"sealed-a", "sealed-c"},
        excluded={"media"},
        universe=set(metadata),
        metadata=metadata,
    )
    output = tmp_path / "coverage.svg"
    audit_split_coverage.write_svg(output, audit)
    root = ET.parse(output).getroot()

    assert root.attrib["width"] == "980"
    for rect in root.findall("{http://www.w3.org/2000/svg}rect"):
        if rect.attrib.get("width", "").endswith("%"):
            continue
        x = float(rect.attrib.get("x", 0))
        width = float(rect.attrib.get("width", 0))
        assert x >= 0
        assert width >= 0
        assert x + width <= 980


def test_manifest_loader_accepts_bom_and_rejects_nested_duplicate_key(tmp_path: Path) -> None:
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf{\"ok\": true}")
    assert audit_split_coverage.load_json(bom)["ok"] is True

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"outer": {"value": 1, "value": 2}}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        audit_split_coverage.load_json(duplicate)
