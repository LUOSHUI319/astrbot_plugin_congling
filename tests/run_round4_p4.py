"""P4 可执行验收：真实临时 SQLite + 可控时钟；不访问线上数据/模型。"""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_congling.tests import run_diagnosis_regressions
from astrbot_plugin_congling.tests import test_round4_p4

if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(test_round4_p4))
    raise SystemExit(not result.wasSuccessful())
