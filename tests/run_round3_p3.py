"""P3 离线验收；仅临时 SQLite / 框架替身，不访问真实库、不发送消息。"""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_congling.tests import run_diagnosis_regressions
from astrbot_plugin_congling.tests import test_round3_p3

if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(test_round3_p3))
    raise SystemExit(not result.wasSuccessful())
