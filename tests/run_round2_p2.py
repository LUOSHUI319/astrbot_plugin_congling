"""Offline P2 suite; all writes are temporary SQLite fixtures."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_congling.tests import run_diagnosis_regressions  # framework substitutes
from astrbot_plugin_congling.tests import test_round2_p2

if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(test_round2_p2))
    raise SystemExit(not result.wasSuccessful())
