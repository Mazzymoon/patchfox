#!/usr/bin/env python3
"""Run a reproducible PatchFox SWE-bench Verified batch experiment."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _main() -> int:
    from patchfox.evaluation.swebench_experiment import main

    return main()


if __name__ == "__main__":
    raise SystemExit(_main())
