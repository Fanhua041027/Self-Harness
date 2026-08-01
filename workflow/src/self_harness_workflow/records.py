from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class FailureRecord:
    task_id: str
    repeat: int
    split: str
    trace: Mapping[str, Any]
    output: Any
    verdict: Mapping[str, Any]
    terminal_cause: str
    agent_behavior: str
    mechanism: str

    def __post_init__(self) -> None:
        if self.split != "evolution":
            raise ValueError("only Evolution failures may enter weakness mining")

    @property
    def signature(self) -> tuple[str, str, str]:
        return self.terminal_cause, self.agent_behavior, self.mechanism

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "x": self.task_id, "repeat": self.repeat, "split": self.split,
                "trace": self.trace, "output": self.output, "verdict": self.verdict,
                "signature": {"terminal_verifier_cause": self.terminal_cause,
                              "causal_agent_behavior": self.agent_behavior, "abstract_mechanism": self.mechanism}}


def exact_signature_clusters(records: Iterable[FailureRecord], *, min_support: int = 2) -> list[dict[str, Any]]:
    if min_support < 2:
        raise ValueError("min_support must be at least 2; single-failure proposals are forbidden")
    grouped: dict[tuple[str, str, str], list[FailureRecord]] = defaultdict(list)
    for record in records:
        grouped[record.signature].append(record)
    clusters = []
    for signature, members in sorted(grouped.items()):
        task_support = len({item.task_id for item in members})
        repeat_support = len({(item.task_id, item.repeat) for item in members})
        actionable = repeat_support >= min_support and task_support >= 2
        clusters.append({"schema_version": SCHEMA_VERSION, "signature": list(signature), "support": repeat_support,
                         "task_support": task_support, "actionable": actionable,
                         "ineligible_reason": None if actionable else "insufficient_cross_task_support",
                         "representative_tasks": sorted({item.task_id for item in members})[:5]})
    return sorted(clusters, key=lambda item: (-item["support"], item["signature"]))


def evidence_bundle(cluster: Mapping[str, Any], records: Iterable[FailureRecord], provenance: Mapping[str, Any]) -> dict[str, Any]:
    if not cluster.get("actionable"):
        raise ValueError("cannot propose from a low-support cluster")
    signature = tuple(cluster["signature"])
    selected = [record for record in records if record.signature == signature]
    return {"schema_version": SCHEMA_VERSION, "cluster_signature": list(signature),
            "chain": {"failure_phenomenon": [item.verdict for item in selected], "agent_behavior": signature[1],
                      "root_mechanism": signature[2], "harness_defect": "to_be_selected_by_proposer"},
            "provenance": dict(provenance)}
