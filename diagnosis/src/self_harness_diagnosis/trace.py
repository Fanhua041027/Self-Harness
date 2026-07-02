from __future__ import annotations

import json
import re
import signal
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TRACE_CAUSAL_DIAGNOSIS_FORMAT = "self_harness.trace_causal_diagnosis.v0"

_CHANGE_TOOL_RE = re.compile(
    r"(write|edit|patch|replace|update|create|delete|remove|append|insert|"
    r"submit|save|commit|apply)",
    re.IGNORECASE,
)
_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_SIGNATURE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{1,119}$")
_CRITICALITY_VALUES = {
    "root_cause",
    "contributor",
    "non_terminal_friction",
    "recovered_friction",
    "unknown",
}


@dataclass(frozen=True)
class DiagnosisConfig:
    """Runtime configuration for trace diagnosis.

    Runtime choices are explicit and caller supplied.
    """

    model_reference: str | None = None
    timeout_s: float | None = 180.0
    retries: int = 1
    strict: bool = True


@dataclass(frozen=True)
class ToolCallRecord:
    name: str
    args: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "args": self.args}


@dataclass(frozen=True)
class ToolResultRecord:
    name: str
    status: str | None
    content_excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "content_excerpt": self.content_excerpt,
        }


@dataclass(frozen=True)
class NormalizedStep:
    step_id: int
    kind: str
    assistant_summary: str
    tool_calls: tuple[ToolCallRecord, ...]
    tool_results: tuple[ToolResultRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "kind": self.kind,
            "assistant_summary": self.assistant_summary,
            "tool_calls": [item.to_dict() for item in self.tool_calls],
            "tool_results": [item.to_dict() for item in self.tool_results],
        }


@dataclass(frozen=True)
class StageRecord:
    stage_id: int
    step_ids: tuple[int, ...]
    boundary_step_id: int
    boundary_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "step_ids": list(self.step_ids),
            "boundary_step_id": self.boundary_step_id,
            "boundary_type": self.boundary_type,
        }


def build_causal_trace_diagnosis(
    payload: dict[str, Any],
    *,
    llm: Any,
    config: DiagnosisConfig | None = None,
    source_trace_path: Path | None = None,
) -> dict[str, Any] | None:
    """Build a terminal-cause-first trace diagnosis.

    `llm` must expose `invoke(messages)` and may optionally expose
    `bind(response_format=...)`, matching common chat-model clients. No provider
    construction is performed here.
    """

    config = config or DiagnosisConfig()
    if not _is_failed_trace(payload):
        return None
    if llm is None:
        raise RuntimeError("build_causal_trace_diagnosis requires an explicit llm client")

    steps = normalize_trace_steps(payload)
    if config.strict and not steps:
        raise RuntimeError("strict trace diagnosis requires normalized trace steps")
    stages = build_stage_records(steps)
    analysis = _run_llm_analysis(
        llm=llm,
        payload=payload,
        steps=steps,
        stages=stages,
        config=config,
    )
    terminal_kind = terminal_failure_kind(payload)
    terminal_summary = terminal_verifier_summary(payload)
    analysis = _annotate_analysis_causality(
        steps=steps,
        analysis=analysis,
        terminal_failure_kind=terminal_kind,
    )
    return {
        "format": TRACE_CAUSAL_DIAGNOSIS_FORMAT,
        "trace_id": str(payload.get("id", "")),
        "source_trace_path": str(source_trace_path) if source_trace_path is not None else None,
        "status": payload.get("status"),
        "failure_message": payload.get("failure_message"),
        "runtime_failure": payload.get("runtime_failure"),
        "terminal_failure_kind": terminal_kind,
        "terminal_failure_summary": terminal_summary,
        "verifier_evidence": verifier_evidence_from_payload(payload),
        "causal_summary": _build_causal_summary(
            terminal_failure_kind=terminal_kind,
            terminal_failure_summary=terminal_summary,
            analysis=analysis,
        ),
        "task_description": extract_task_description(payload),
        "step_count": len(steps),
        "steps": [step.to_dict() for step in steps],
        "stages": [stage.to_dict() for stage in stages],
        "analysis": analysis,
        "analysis_model": {
            "requested_model": config.model_reference,
            "used_model": config.model_reference,
            "analysis_mode": "llm",
        },
    }


def normalize_trace_steps(payload: dict[str, Any]) -> list[NormalizedStep]:
    messages = _extract_output_messages(payload)
    steps: list[NormalizedStep] = []
    index = 0
    cursor = 0
    while cursor < len(messages):
        message = messages[cursor]
        if not _is_ai_message(message):
            cursor += 1
            continue
        tool_calls = _tool_calls_from_message(message)
        tool_results: list[ToolResultRecord] = []
        lookahead = cursor + 1
        while lookahead < len(messages) and _is_tool_message(messages[lookahead]):
            tool_results.append(_tool_result_from_message(messages[lookahead]))
            lookahead += 1
        index += 1
        steps.append(
            NormalizedStep(
                step_id=index,
                kind=_step_kind(tool_calls),
                assistant_summary=_message_text(message),
                tool_calls=tuple(tool_calls),
                tool_results=tuple(tool_results),
            )
        )
        cursor = lookahead
    return steps


def build_stage_records(steps: list[NormalizedStep]) -> list[StageRecord]:
    records: list[StageRecord] = []
    stage_steps: list[int] = []
    stage_id = 1
    for step in steps:
        stage_steps.append(step.step_id)
        if step.kind == "change":
            records.append(
                StageRecord(
                    stage_id=stage_id,
                    step_ids=tuple(stage_steps),
                    boundary_step_id=step.step_id,
                    boundary_type=step.kind,
                )
            )
            stage_id += 1
            stage_steps = []
    if stage_steps:
        records.append(
            StageRecord(
                stage_id=stage_id,
                step_ids=tuple(stage_steps),
                boundary_step_id=stage_steps[-1],
                boundary_type="explore",
            )
        )
    return records


def extract_task_description(payload: dict[str, Any]) -> str:
    for message in _extract_output_messages(payload):
        if str(message.get("type", "")).lower() in {"human", "user"}:
            return _message_text(message)
    return str(payload.get("task_description", "") or "")


def verifier_evidence_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = payload.get("verifier_evidence")
    return dict(evidence) if isinstance(evidence, dict) else {}


def terminal_failure_kind(payload: dict[str, Any]) -> str:
    evidence = verifier_evidence_from_payload(payload)
    text = " ".join(
        str(item or "").lower()
        for item in (
            evidence.get("terminal_summary"),
            evidence.get("reward_text"),
            payload.get("failure_message"),
        )
    )
    for snippet in evidence.get("failure_snippets", []) if isinstance(evidence.get("failure_snippets"), list) else []:
        text += " " + str(snippet).lower()
    if _has_missing_artifact(evidence, text):
        return "missing_required_artifact"
    if "modulenotfounderror:" in text or "importerror:" in text or "no module named" in text:
        return "missing_dependency"
    if "timeout" in text or evidence.get("agent", {}).get("timed_out") is True:
        return "agent_timeout"
    if "runtimeerror:" in text or "valueerror:" in text or "typeerror:" in text:
        return "verifier_runtime_error"
    if "assertionerror:" in text or "expected" in text or "mismatch" in text:
        return "verifier_assertion"
    rewards = evidence.get("rewards")
    if isinstance(rewards, dict) and float(rewards.get("reward", 0.0) or 0.0) <= 0.0:
        return "reward_zero"
    return "unknown"


def terminal_verifier_summary(payload: dict[str, Any]) -> str:
    evidence = verifier_evidence_from_payload(payload)
    explicit = str(evidence.get("terminal_summary", "") or "").strip()
    if explicit:
        return explicit
    snippets = evidence.get("failure_snippets")
    if isinstance(snippets, list):
        for snippet in snippets:
            if str(snippet).strip():
                return str(snippet).strip()
    return str(payload.get("failure_message", "") or "").strip()


def _run_llm_analysis(
    *,
    llm: Any,
    payload: dict[str, Any],
    steps: list[NormalizedStep],
    stages: list[StageRecord],
    config: DiagnosisConfig,
) -> list[dict[str, Any]]:
    prompt = _build_stage_analysis_prompt(
        task_description=extract_task_description(payload),
        failure_message=str(payload.get("failure_message", "") or ""),
        verifier_evidence=verifier_evidence_from_payload(payload),
        steps=steps,
        stages=stages,
    )
    response_llm = _json_object_response_llm(llm)
    last_error: Exception | None = None
    for _attempt in range(max(1, config.retries)):
        try:
            with _timeout(config.timeout_s):
                response = response_llm.invoke(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Analyze a failed agent trace. Return only JSON with key `analysis`. "
                                "Each item must contain stage_id, incorrect_step_ids, unuseful_step_ids, "
                                "reasoning, terminal_link, causal_weight, selected_step_recovered, "
                                "terminal_cause, criticality, agent_mechanism, missed_oracle."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ]
                )
            analysis = _validate_analysis(_extract_analysis_items(_llm_response_text(response)), stages=stages)
            if analysis:
                return analysis
        except Exception as exc:
            last_error = exc
    if config.strict:
        raise RuntimeError(f"trace diagnosis LLM failed: {last_error}") from last_error
    return []


def _build_stage_analysis_prompt(
    *,
    task_description: str,
    failure_message: str,
    verifier_evidence: dict[str, Any],
    steps: list[NormalizedStep],
    stages: list[StageRecord],
) -> str:
    return (
        "Task description:\n"
        f"{task_description or '(missing)'}\n\n"
        "Failure message:\n"
        f"{_truncate(failure_message, 4000) or '(missing)'}\n\n"
        "Terminal verifier evidence, highest priority:\n"
        f"{_truncate(json.dumps(verifier_evidence, ensure_ascii=False, sort_keys=True), 6000)}\n\n"
        "Normalized steps:\n"
        + "\n".join(json.dumps(_step_prompt_payload(step), ensure_ascii=False) for step in steps)
        + "\n\nStages:\n"
        + "\n".join(json.dumps(stage.to_dict(), ensure_ascii=False) for stage in stages)
        + "\n\n"
        "Instructions:\n"
        "- Root cause means the first unrecoverable critical failure, not the first visible recovered tool error.\n"
        "- Terminal verifier evidence describes what actually made the task fail, but the diagnosis must still "
        "name the causal terminal signature from the combined verifier evidence and trace behavior.\n"
        "- Use trace steps to explain how the run caused the verifier outcome.\n"
        "- Mark recovered tool friction as causal_weight=friction and selected_step_recovered=true.\n"
        "- For every stage item, set terminal_cause to a concise snake_case causal terminal signature. "
        "Do not merely restate a broad verifier bucket if the trace reveals a more specific terminal cause.\n"
        "- For every stage item, set criticality to exactly one of: root_cause, contributor, "
        "non_terminal_friction, recovered_friction, unknown.\n"
        "- For every stage item, set agent_mechanism to a concise snake_case reusable behavior pattern "
        "observed in the agent trace.\n"
        "- If no concrete unrecovered wrong step is visible, leave incorrect_step_ids empty and explain uncertainty.\n"
    )


def _step_prompt_payload(step: NormalizedStep) -> dict[str, Any]:
    return {
        "step_id": step.step_id,
        "kind": step.kind,
        "assistant_summary": _truncate(step.assistant_summary, 220),
        "tool_calls": [
            {"name": call.name, "args": _truncate(json.dumps(call.args, ensure_ascii=False), 240)}
            for call in step.tool_calls
        ],
        "tool_results": [
            {"name": result.name, "status": result.status, "content_excerpt": result.content_excerpt}
            for result in step.tool_results
        ],
    }


def _annotate_analysis_causality(
    *,
    steps: list[NormalizedStep],
    analysis: list[dict[str, Any]],
    terminal_failure_kind: str,
) -> list[dict[str, Any]]:
    valid_step_ids = {step.step_id for step in steps}
    annotated: list[dict[str, Any]] = []
    for item in analysis:
        incorrect = [int(step_id) for step_id in item.get("incorrect_step_ids", []) if step_id in valid_step_ids]
        unuseful = [int(step_id) for step_id in item.get("unuseful_step_ids", []) if step_id in valid_step_ids]
        copied = dict(item)
        copied["incorrect_step_ids"] = incorrect
        copied["unuseful_step_ids"] = unuseful
        copied.setdefault("terminal_failure_kind", terminal_failure_kind)
        copied.setdefault("terminal_link", "unknown")
        copied.setdefault("causal_weight", "unknown")
        copied.setdefault("selected_step_recovered", "unknown")
        annotated.append(copied)
    return annotated


def _build_causal_summary(
    *,
    terminal_failure_kind: str,
    terminal_failure_summary: str,
    analysis: list[dict[str, Any]],
) -> str:
    selected = _primary_analysis_item(analysis)
    if selected is None:
        return f"Terminal failure `{terminal_failure_kind}`: {terminal_failure_summary or 'no verifier summary'}"
    return (
        f"Primary failure candidate is stage {selected.get('stage_id')} "
        f"({selected.get('causal_weight', 'unknown')}, terminal_link={selected.get('terminal_link', 'unknown')}): "
        f"{selected.get('reasoning', '').strip()} Verifier evidence: {terminal_failure_summary}"
    ).strip()


def _primary_analysis_item(analysis: list[dict[str, Any]]) -> dict[str, Any] | None:
    items = [item for item in analysis if isinstance(item, dict)]
    if not items:
        return None
    weight_rank = {"root_cause": 0, "contributor": 1, "unknown": 2, "friction": 3, "noise": 4}
    link_rank = {"direct": 0, "indirect": 1, "unknown": 2, "weak": 3, "none": 4}
    return sorted(
        items,
        key=lambda item: (
            0 if item.get("incorrect_step_ids") else 1,
            weight_rank.get(str(item.get("causal_weight", "unknown")), 2),
            link_rank.get(str(item.get("terminal_link", "unknown")), 2),
            int(item.get("stage_id", 999999) or 999999),
        ),
    )[0]


def _validate_analysis(items: list[Any], *, stages: list[StageRecord]) -> list[dict[str, Any]]:
    stage_ids = {stage.stage_id for stage in stages}
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        stage_id = item.get("stage_id")
        if stage_id not in stage_ids:
            continue
        result.append(
            {
                "stage_id": stage_id,
                "incorrect_step_ids": _int_list(item.get("incorrect_step_ids")),
                "unuseful_step_ids": _int_list(item.get("unuseful_step_ids")),
                "reasoning": str(item.get("reasoning", "") or ""),
                "terminal_link": _choice(item.get("terminal_link"), {"direct", "indirect", "weak", "none", "unknown"}),
                "causal_weight": _choice(
                    item.get("causal_weight"),
                    {"root_cause", "contributor", "friction", "noise", "unknown"},
                ),
                "selected_step_recovered": item.get("selected_step_recovered", "unknown"),
                "terminal_cause": _signature_token(item, "terminal_cause"),
                "criticality": _criticality_value(item),
                "agent_mechanism": _signature_token(item, "agent_mechanism"),
                "missed_oracle": str(item.get("missed_oracle", "") or ""),
            }
        )
    return result


def _extract_analysis_items(text: str) -> list[Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            return []
        payload = json.loads(match.group(0))
    items = payload.get("analysis") if isinstance(payload, dict) else payload
    return items if isinstance(items, list) else []


def _extract_output_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    outputs = payload.get("outputs")
    if isinstance(outputs, dict) and isinstance(outputs.get("messages"), list):
        return [item for item in outputs["messages"] if isinstance(item, dict)]
    if isinstance(payload.get("messages"), list):
        return [item for item in payload["messages"] if isinstance(item, dict)]
    return []


def _is_failed_trace(payload: dict[str, Any]) -> bool:
    return str(payload.get("status", "") or "").lower() in {"failed", "error"} or bool(payload.get("failure_message"))


def _is_ai_message(message: dict[str, Any]) -> bool:
    return str(message.get("type", "") or "").lower() in {"ai", "assistant"}


def _is_tool_message(message: dict[str, Any]) -> bool:
    return str(message.get("type", "") or "").lower() == "tool"


def _tool_calls_from_message(message: dict[str, Any]) -> list[ToolCallRecord]:
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    calls: list[ToolCallRecord] = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "") or "").strip()
        args = raw.get("args")
        calls.append(ToolCallRecord(name=name, args=args if isinstance(args, dict) else {}))
    return calls


def _tool_result_from_message(message: dict[str, Any]) -> ToolResultRecord:
    return ToolResultRecord(
        name=str(message.get("name", "") or ""),
        status=str(message.get("status")) if message.get("status") is not None else None,
        content_excerpt=_truncate(str(message.get("content", "") or ""), 500),
    )


def _step_kind(tool_calls: list[ToolCallRecord]) -> str:
    if any(_CHANGE_TOOL_RE.search(call.name) for call in tool_calls):
        return "change"
    return "explore"


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item) for item in content)
    return str(content or "")


def _json_object_response_llm(llm: Any) -> Any:
    bind = getattr(llm, "bind", None)
    if callable(bind):
        try:
            return bind(response_format={"type": "json_object"})
        except TypeError:
            return llm
    return llm


def _llm_response_text(response: Any) -> str:
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    return str(response)


def _has_missing_artifact(evidence: dict[str, Any], text: str) -> bool:
    if evidence.get("missing_artifact_paths"):
        return True
    return any(
        needle in text
        for needle in (
            "filenotfounderror:",
            "no such file or directory",
            "could not find the file",
            "missing output",
            "file not found",
            "does not exist",
        )
    )


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return [int(item) for item in value if isinstance(item, int)]


def _choice(value: Any, allowed: set[str]) -> str:
    text = str(value or "").strip()
    return text if text in allowed else "unknown"


def _signature_token(item: dict[str, Any], key: str) -> str:
    text = str(item.get(key, "") or "").strip().lower()
    if not _SIGNATURE_TOKEN_RE.match(text):
        raise ValueError(f"LLM analysis item has invalid {key}: {text!r}")
    return text


def _criticality_value(item: dict[str, Any]) -> str:
    text = str(item.get("criticality", "") or "").strip().lower()
    if text not in _CRITICALITY_VALUES:
        raise ValueError(f"LLM analysis item has invalid criticality: {text!r}")
    return text


def _truncate(value: str, max_chars: int) -> str:
    return value if len(value) <= max_chars else value[: max_chars - 3] + "..."


@contextmanager
def _timeout(timeout_s: float | None):
    if not timeout_s or timeout_s <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return

    def handle_timeout(_signum: int, _frame: Any) -> None:
        raise TimeoutError(f"trace diagnosis LLM call exceeded {timeout_s:.0f}s")

    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
