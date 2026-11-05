#!/usr/bin/env python3
"""一键跑全部单测：python tests/run_tests.py"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    suite = unittest.TestLoader().discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print()
    print("共 %d 个用例，失败 %d，错误 %d" % (result.testsRun, len(result.failures), len(result.errors)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
