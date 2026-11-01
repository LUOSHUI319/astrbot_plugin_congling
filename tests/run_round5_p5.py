"""P5 离线验收入口：只用临时数据库，不接触生产数据。"""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_congling.tests import run_diagnosis_regressions
from astrbot_plugin_congling.tests import test_round5_p5

if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(test_round5_p5))
    raise SystemExit(not result.wasSuccessful())
