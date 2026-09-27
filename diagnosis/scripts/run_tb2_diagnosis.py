#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from langchain_openai import ChatOpenAI

from self_harness_diagnosis.integrated import DiagnosisOutcome, write_verifier_causal_brief
from self_harness_diagnosis.tb2 import load_tb2_verifier_evidence
from self_harness_diagnosis.trace import DiagnosisConfig, build_causal_trace_diagnosis


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a verifier-causal diagnosis from a Harbor eval result.")
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default=os.environ.get("SELF_HARNESS_MODEL"))
    parser.add_argument("--timeout-s", type=float, default=180.0)
    parser.add_argument("--concurrency", type=int, default=4, help="parallel diagnosis calls (default 4)")
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        raise RuntimeError("--concurrency must be >= 1")

    if not args.model:
        raise RuntimeError("--model or SELF_HARNESS_MODEL is required")
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError("OPENAI_API_KEY and OPENAI_BASE_URL are required")

    result = read_json(args.result)
    outcomes = load_train_outcomes(result)
    model_name = args.model.removeprefix("openai:")
    llm = ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        use_responses_api=False,
        timeout=args.timeout_s,
        max_retries=2,
    )
    diagnosis_dir = args.output.resolve().parent / "diagnoses"
    diagnoses: dict[str, dict[str, Any]] = {}

    def diagnose_one(outcome: DiagnosisOutcome) -> tuple[str, DiagnosisOutcome, dict[str, Any]] | None:
        if outcome.passed or outcome.status == "invalid":
            # Infrastructure timeout, not an Evolution failure; excluded from failure mining.
            return None
        if not outcome.messages_path:
            print(f"WARNING: skip diagnosis for {outcome.case_id}: no messages_path", file=sys.stderr)
            return None
        messages_path = Path(outcome.messages_path).resolve()
        messages = read_json_list(messages_path)
        payload = {
            "id": outcome.case_id,
            "status": "failed",
            "failure_message": outcome.failure_message,
            "outputs": {"messages": messages},
            "verifier_evidence": load_tb2_verifier_evidence(messages_path=messages_path),
        }
        try:
            diagnosis = build_causal_trace_diagnosis(
                payload,
                llm=llm,
                config=DiagnosisConfig(model_reference=args.model, timeout_s=args.timeout_s, retries=2, strict=True),
                source_trace_path=messages_path,
            )
        except Exception as exc:
            print(f"WARNING: skip diagnosis for {outcome.case_id}: {exc!r}", file=sys.stderr)
            return None
        if diagnosis is None:
            print(f"WARNING: skip diagnosis for {outcome.case_id}: empty analysis", file=sys.stderr)
            return None
        return str(messages_path), outcome, diagnosis

    targets = [outcome for outcome in outcomes if not outcome.passed and outcome.status != "invalid"]
    if args.concurrency > 1 and len(targets) > 1:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [executor.submit(diagnose_one, outcome) for outcome in targets]
            for future in as_completed(futures):
                item = future.result()  # propagate errors from any worker
                if item is None:
                    continue
                messages_key, outcome, diagnosis = item
                diagnoses[messages_key] = diagnosis
                write_json(diagnosis_dir / f"{safe_slug(outcome.case_id)}.json", diagnosis)
    else:
        for outcome in targets:
            item = diagnose_one(outcome)
            if item is None:
                continue
            messages_key, outcome, diagnosis = item
            diagnoses[messages_key] = diagnosis
            write_json(diagnosis_dir / f"{safe_slug(outcome.case_id)}.json", diagnosis)

    write_verifier_causal_brief(
        outcomes=outcomes,
        load_diagnosis=lambda outcome: diagnoses.get(str(Path(outcome.messages_path).resolve()))
        if outcome.messages_path
        else None,
        output_path=args.output.resolve(),
    )
    print(f"wrote {args.output.resolve()} ({len(diagnoses)} failed train trace(s) diagnosed)")
    return 0


def load_train_outcomes(result: dict[str, Any]) -> list[DiagnosisOutcome]:
    split_runs = result.get("splits", {}).get("train", [])
    outcomes: list[DiagnosisOutcome] = []
    for split_run in split_runs:
        for case in split_run.get("case_results", []):
            repeat = int(case.get("repeat", split_run.get("repeat", 1)))
            outcomes.append(
                DiagnosisOutcome(
                    case_id=f"{case['case_id']}#repeat-{repeat:02d}",
                    split="train",
                    stratum=str(case.get("stratum", "default")),
                    status=str(case.get("status", "failed")),
                    failure_message=case.get("failure_message"),
                    artifacts_dir=case.get("artifacts_dir"),
                    messages_path=case.get("messages_path"),
                )
            )
    if not outcomes:
        raise RuntimeError("eval result contains no train outcomes")
    return outcomes


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.resolve().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def read_json_list(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"expected JSON array: {path}")
    return [item for item in payload if isinstance(item, dict)]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "case"


if __name__ == "__main__":
    raise SystemExit(main())
