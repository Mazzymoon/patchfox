"""Generate one SWE-bench prediction with PatchFox.

The delegated runner CLI supports host Bubblewrap execution and an official
SWE-bench image mode that uses Docker as the explicit outer sandbox.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from patchfox.evaluation.swebench_runner import main

if __name__ == "__main__":
    raise SystemExit(main())
