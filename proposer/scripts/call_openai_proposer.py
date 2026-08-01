#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path

from langchain_openai import ChatOpenAI


def main() -> int:
    parser = argparse.ArgumentParser(description="Call an OpenAI-compatible model for a proposer prompt.")
    parser.add_argument("--prompt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default=os.environ.get("SELF_HARNESS_MODEL", ""))
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    if not args.model:
        raise RuntimeError("missing model; pass --model or set SELF_HARNESS_MODEL")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required")

    llm = ChatOpenAI(
        model=args.model.removeprefix("openai:"),
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
        api_key=os.environ["OPENAI_API_KEY"],
        timeout=args.timeout,
        max_retries=2,
        temperature=0,
        use_responses_api=False,
    )
    response = llm.invoke(args.prompt.read_text(encoding="utf-8"))
    text = response.content if isinstance(response.content, str) else str(response.content)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text.rstrip() + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
