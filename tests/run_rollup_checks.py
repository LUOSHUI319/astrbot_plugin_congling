"""离线专项：python tests/run_rollup_checks.py --guard-root /path/to/astrbot_plugin_at_guard。"""
import argparse
import contextlib
import importlib
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="摘要保真与未读爬楼离线回归，不调用真实模型或发送平台消息")
    parser.add_argument("--guard-root", type=Path,
                        default=Path(__file__).resolve().parents[2] / "astrbot_plugin_at_guard")
    parser.add_argument("--result-dir", type=Path, help="可选：保存日志与结果 JSON")
    args = parser.parse_args()
    if not (args.guard_root / "main.py").is_file():
        parser.error("--guard-root 应指向配套 at_guard 插件源码目录")

    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root / "tests"), str(root)]
    import test_wake_persona_flow
    # 仅装配待测 reset 模块，阻止导入完整记忆服务或真实数据目录。
    package = types.ModuleType("cg.memory")
    package.__path__ = [str(root / "memory")]
    sys.modules[package.__name__] = package
    star = types.ModuleType("astrbot.api.star")
    def deny_data_dir(*args, **kwargs):
        raise AssertionError("离线测试不可访问生产数据库")
    star.StarTools = types.SimpleNamespace(get_data_dir=deny_data_dir)
    sys.modules[star.__name__] = star

    names = (
        "test_rollup_cache", "test_rollup_evidence", "test_wake_persona_flow",
        "test_unread_crawl_flow", "test_active_crawl_announcement",
        "test_rollup_mentions", "test_modelhub_max_tokens",
    )
    # skipUnless 在导入时判定源码是否存在，须先设置路径再装配测试。
    os.environ["CONGLING_TEST_GUARD_ROOT"] = str(args.guard_root.resolve())
    modules = [importlib.import_module(name) for name in names]
    modules[3].GUARD_ROOT = args.guard_root.resolve()
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromModule(module)
                               for module in modules)
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    record = {"run": result.testsRun, "failures": len(result.failures),
              "errors": len(result.errors), "skipped": len(result.skipped),
              "real_model_calls": 0, "real_platform_sends": 0}
    if args.result_dir:
        args.result_dir.mkdir(parents=True, exist_ok=True)
        (args.result_dir / "github-tests.log").write_text(stream.getvalue(), encoding="utf-8")
        (args.result_dir / "github-tests.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False))
    if not result.wasSuccessful():
        print(stream.getvalue())
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
