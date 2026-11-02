"""Offline P0/P1 entrypoint: original 161 + focused new tests + cache guard.

Only temporary SQLite and loopback HTTP test fixtures are used.
Example: E:/BotLauncher/python/python.exe -B tests/run_round1_p0p1.py
"""
from __future__ import annotations

import importlib
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.dont_write_bytecode = True
from astrbot_plugin_congling.tests import run_diagnosis_regressions as baseline

BASELINE_MODULES = (
    "test_sleep_stages", "test_unread_gate", "test_diary", "test_proactive",
    "test_memory_level", "test_night_wake_dream", "test_dream_hint",
    "test_core", "test_night_wake_text", "test_qzone", "test_qzone_http",
)


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(baseline.Regression)
    for name in BASELINE_MODULES:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(
            importlib.import_module("astrbot_plugin_congling.tests." + name)))
    assert suite.countTestCases() >= 161, suite.countTestCases()
    original = unittest.TextTestRunner(verbosity=1).run(suite)
    added = unittest.defaultTestLoader.loadTestsFromModule(
        importlib.import_module("astrbot_plugin_congling.tests.test_round1_p0p1"))
    focused = unittest.TextTestRunner(verbosity=2).run(added)
    cache = importlib.import_module("astrbot_plugin_congling.tests.check_prompt_cache")
    guard_code = cache.main()
    print(f"original={original.testsRun}, added={focused.testsRun}, cache_checks={len(cache.CHECKS)}")
    return int(not original.wasSuccessful() or not focused.wasSuccessful() or guard_code != 0)


if __name__ == "__main__":
    raise SystemExit(main())
