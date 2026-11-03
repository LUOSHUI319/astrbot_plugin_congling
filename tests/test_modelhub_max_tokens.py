"""输出限制的请求级回归；不访问模型、不读取真实配置。"""
import asyncio
import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


def load_modelhub():
    try:
        from astrbot.api import logger
    except ImportError:
        api = types.ModuleType("astrbot.api")
        api.logger = logging.getLogger("max-tokens-test")
        previous = sys.modules.get("astrbot.api")
        sys.modules["astrbot.api"] = api
    else:
        api = None
    try:
        path = Path(__file__).resolve().parents[1] / "core/modelhub.py"
        spec = importlib.util.spec_from_file_location("modelhub_limit_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if api is not None:
            if previous is None:
                sys.modules.pop("astrbot.api", None)
            else:
                sys.modules["astrbot.api"] = previous


modelhub = load_modelhub()


class Resource:
    def __init__(self):
        self.calls = []
        self.failure = None

    async def create(self, **kwargs):
        await asyncio.sleep(0)
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        return SimpleNamespace(completion_text="结果")


class Provider:
    """模拟原生适配器丢弃 text_chat kwargs，并合入全局 extra_body。"""
    def __init__(self):
        self.resource = Resource()
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=self.resource))
        self.body = {"max_tokens": 8192, "thinking": {"type": "enabled"}}
        self.provider_options = []

    async def text_chat(self, **kwargs):
        self.provider_options.append(kwargs)
        return await self.resource.create(prompt=kwargs["prompt"], extra_body=self.body)


def hub(provider):
    plugin = SimpleNamespace(cfg=object(), context=SimpleNamespace(get_using_provider=lambda: provider))
    return modelhub.ModelHub(plugin)


class MaxTokensTests(unittest.IsolatedAsyncioTestCase):
    async def test_limit_reaches_provider_and_final_sdk_request(self):
        provider = Provider()
        self.assertEqual(await hub(provider).chat("摘要", max_tokens=2000), "结果")
        self.assertEqual(provider.provider_options[0]["max_tokens"], 2000)
        call = provider.resource.calls[0]
        self.assertEqual(call["max_tokens"], 2000)
        self.assertNotIn("max_tokens", call["extra_body"])
        self.assertEqual(provider.body["max_tokens"], 8192)
        self.assertEqual(call["extra_body"]["thinking"], {"type": "enabled"})

    async def test_concurrent_limits_and_unrelated_call_are_isolated(self):
        provider = Provider()
        model = hub(provider)
        await asyncio.gather(model.chat("first", max_tokens=2000),
                             model.chat("second", max_tokens=900),
                             provider.resource.create(prompt="ordinary", max_tokens=123))
        calls = {r["prompt"]: r for r in provider.resource.calls}
        self.assertEqual(calls["first"]["max_tokens"], 2000)
        self.assertEqual(calls["second"]["max_tokens"], 900)
        self.assertEqual(calls["ordinary"]["max_tokens"], 123)
        await provider.resource.create(prompt="after")
        self.assertNotIn("max_tokens", provider.resource.calls[-1])

    async def test_error_resets_scope_and_does_not_retry(self):
        provider = Provider()
        provider.resource.failure = RuntimeError("synthetic failure")
        self.assertEqual(await hub(provider).chat("fail", max_tokens=2000), "")
        self.assertEqual(len(provider.resource.calls), 1)
        provider.resource.failure = None
        await provider.resource.create(prompt="after")
        self.assertNotIn("max_tokens", provider.resource.calls[-1])

    async def test_cancellation_resets_scope(self):
        provider = Provider()
        provider.resource.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await hub(provider).chat("cancel", max_tokens=2000)
        provider.resource.failure = None
        await provider.resource.create(prompt="after")
        self.assertNotIn("max_tokens", provider.resource.calls[-1])

    async def test_hot_reload_reuses_bridge(self):
        provider = Provider()
        await hub(provider).chat("first", max_tokens=2000)
        wrapper = provider.resource.create
        reloaded = load_modelhub()
        plugin = SimpleNamespace(cfg=object(), context=SimpleNamespace(get_using_provider=lambda: provider))
        await reloaded.ModelHub(plugin).chat("after reload", max_tokens=900)
        self.assertIs(provider.resource.create, wrapper)
        self.assertEqual(provider.resource.calls[-1]["max_tokens"], 900)

    async def test_router_without_sdk_receives_limit(self):
        calls = []
        async def text_chat(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(completion_text="路由摘要")
        self.assertEqual(await hub(SimpleNamespace(text_chat=text_chat)).chat(
            "route", max_tokens=2000), "路由摘要")
        self.assertEqual(calls[0]["max_tokens"], 2000)

    async def test_existing_completion_token_field_is_preserved(self):
        provider = Provider()
        provider.body = {"max_completion_tokens": 8192}
        await hub(provider).chat("reasoning", max_tokens=2000)
        call = provider.resource.calls[-1]
        self.assertEqual(call["max_completion_tokens"], 2000)
        self.assertNotIn("max_tokens", call)
        self.assertNotIn("max_completion_tokens", call["extra_body"])

    async def test_invalid_limit_does_not_call_upstream(self):
        provider = Provider()
        for invalid in (0, -1, True, None, "2000"):
            with self.subTest(limit=invalid):
                self.assertEqual(await hub(provider).chat("bad", max_tokens=invalid), "")
        self.assertEqual(provider.resource.calls, [])


if __name__ == "__main__":
    unittest.main()
