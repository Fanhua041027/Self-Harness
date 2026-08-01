#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workflow" / "src"))
sys.path.insert(0, str(ROOT / "acceptance" / "src"))

from self_harness_workflow.mechanism import *  # noqa: F401,F403
from self_harness_workflow.mechanism import main


if __name__ == "__main__":
    raise SystemExit(main())
