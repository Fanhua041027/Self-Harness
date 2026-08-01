from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for source in ("workflow/src", "acceptance/src", "diagnosis/src", "proposer/src"):
    sys.path.insert(0, str(ROOT / source))
