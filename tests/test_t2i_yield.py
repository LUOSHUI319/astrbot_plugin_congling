"""验证「长文本让位给 t2i」：超阈值不接管发送，短文本与 t2i 关闭时照旧。

背景：result_decorate 先跑 on_decorating_result、再判 t2i。本插件只替换
event.send、不改 result.chain，所以 t2i 看到的是未拆分的原文；超阈值时整条
转图，随后 event.send 又把这张图当 extras 挂到最后一条气泡，文字和图片各发一遍。
"""

import importlib.util
import os
import sys
import types

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)

import night_selftest as st  # noqa: E402

st._prepare()

# compose 额外依赖 message_components 与 humanize 包标记
_mc = types.ModuleType("astrbot.api.message_components")


class Plain:
    def __init__(self, text=""):
        self.text = text


class Reply:
    def __init__(self, id=""):
        self.id = id


_mc.Plain = Plain
_mc.Reply = Reply
sys.modules["astrbot.api.message_components"] = _mc
sys.modules["astrbot.api"].message_components = _mc

for _name, _path in (("cg.humanize", os.path.join(ROOT, "humanize")),):
    _mod = types.ModuleType(_name)
    _mod.__path__ = [_path]
    _mod.__package__ = _name
    sys.modules[_name] = _mod

_spec = importlib.util.spec_from_file_location(
    "cg.humanize.compose", os.path.join(ROOT, "humanize", "compose.py")
)
_compose = importlib.util.module_from_spec(_spec)
sys.modules["cg.humanize.compose"] = _compose
_spec.loader.exec_module(_compose)

Composer = _compose.Composer


class _Ctx:
    def __init__(self, cfg):
        self.cfg = cfg

    def get_config(self, *args, **kwargs):
        return self.cfg


class _Plugin:
    def __init__(self, cfg):
        self.context = _Ctx(cfg)


def _composer(cfg):
    # 只测纯判定，跳过 __init__ 对 cfg/guard/rhythm 的依赖
    c = Composer.__new__(Composer)
    c.plugin = _Plugin(cfg)
    return c


def main():
    on = {"t2i": True, "t2i_word_threshold": 150}
    assert _composer(on)._should_yield_to_t2i("字" * 149), "149+2>150 应让位"
    assert not _composer(on)._should_yield_to_t2i("字" * 148), "148+2=150 不该让位"

    off = {"t2i": False, "t2i_word_threshold": 150}
    assert not _composer(off)._should_yield_to_t2i("字" * 500), "t2i 关闭不该让位"

    default = {"t2i": True}
    assert _composer(default)._should_yield_to_t2i("字" * 149), "缺阈值按 150"
    assert not _composer(default)._should_yield_to_t2i("字" * 148), "缺阈值按 150"

    custom = {"t2i": True, "t2i_word_threshold": 300}
    assert not _composer(custom)._should_yield_to_t2i("字" * 250), "250+2<300 不该让位"
    assert _composer(custom)._should_yield_to_t2i("字" * 299), "299+2>300 应让位"

    class _Boom:
        def get_config(self, *args, **kwargs):
            raise RuntimeError("boom")

    c = Composer.__new__(Composer)
    c.plugin = types.SimpleNamespace(context=_Boom())
    assert not c._should_yield_to_t2i("字" * 500), "取配置失败时不该让位"

    print("PASS")


if __name__ == "__main__":
    main()
