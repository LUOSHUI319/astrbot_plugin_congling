"""真实 Chromium + 实际 P5 handler + 临时 SQLite 的四重确认验收。

无线上 HTTP/账号/数据。页面桥接在测试中连接真实 handler，身份由测试 fixture
提供；后端独立的伪造身份与跳步攻击见 run_round5_p5.py。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
from astrbot_plugin_congling.tests.test_round5_p5 import Fixture
from playwright.async_api import async_playwright


async def check(browser, filename, *, real_wait=False):
    fixture = Fixture()
    await fixture.asyncSetUp()
    page = await browser.new_page(viewport={"width": 390, "height": 844})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    calls = []
    try:
        if real_wait:
            fixture.reset.clock = time.monotonic
        async def bridge(path, body):
            assert path == "page/memory/reset"
            calls.append(body["action"])
            return await fixture.call(**body)
        await page.expose_function("p5Bridge", bridge)
        await page.set_content('<!doctype html><html data-ui="night" data-skin="day">'
                               '<head><meta charset="utf-8"></head><body><button id="launch">记忆页</button></body></html>')
        for style in ("style.1029c.css", "theme-v6.1029c.css", "night-light.1031.css"):
            await page.add_style_tag(content=(ROOT / "pages/fromling" / style).read_text(encoding="utf-8"))
        await page.add_script_tag(content="""
          function esc(v) { return String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
          async function post(path, body) { const r = await p5Bridge(path, body); if (!r.ok) throw new Error(r.error); return r.data; }
        """)
        await page.add_script_tag(content=(ROOT / "pages/fromling" / filename).read_text(encoding="utf-8"))
        await page.evaluate("njDeleteAllMemories()")
        title = page.locator("[data-reset-title]")
        nxt = page.locator("[data-reset-next]")
        assert await title.text_content() == "第 1 重确认 · 查看删除范围"
        for word in ("记忆", "主体", "画像", "好感", "日记", "梦", "账本", "排产", "不可恢复"):
            assert word in await page.locator("[data-reset-body]").text_content()
        assert (await page.locator("tr").first.text_content()).endswith("2 / 1 / 1 / 1")
        await nxt.click()
        phrase = page.locator("[data-reset-phrase]")
        assert await nxt.is_disabled()
        await phrase.fill("删除")
        assert await nxt.is_disabled()
        await phrase.fill("删除所有记忆 ")
        assert await nxt.is_disabled()
        await phrase.fill("删除所有记忆")
        assert await nxt.is_enabled()
        await nxt.click()
        ack = page.locator("[data-reset-ack]")
        assert await ack.is_disabled()
        assert await nxt.is_disabled()
        await page.locator("[data-reset-backup]").click()
        await page.wait_for_function("!document.querySelector('[data-reset-ack]').disabled")
        assert await nxt.is_disabled()
        assert list(fixture.backups.glob("memories_*.json"))
        await ack.check()
        assert await nxt.is_enabled()
        await ack.uncheck()
        assert await nxt.is_disabled()
        await ack.check()
        await nxt.click()
        assert await title.text_content() == "第 4 重确认 · 最后等待"
        assert await nxt.is_disabled()
        # 强行移除 disabled 并点击仍受 JS 时钟拦截，没有发 execute。
        await nxt.evaluate("el => { el.disabled = false; el.click(); }")
        assert "execute" not in calls
        await page.wait_for_function("document.querySelector('[data-reset-next]').disabled")
        if real_wait:
            output = ROOT / "docs" / "p5_confirmation_mobile.png"
            await page.screenshot(path=str(output))
            await page.wait_for_function("!document.querySelector('[data-reset-next]').disabled", timeout=15000)
        else:
            # 另一份脚本用浏览器虚拟时钟 + 服务端可控时钟，覆盖全部同一流程。
            await page.clock.install()
            fixture.now += 10
            await page.clock.fast_forward(11000)
            await page.wait_for_function("!document.querySelector('[data-reset-next]').disabled")
        await nxt.click()
        await page.wait_for_function("document.querySelector('[data-reset-title]').textContent === '所有记忆已删除'")
        assert await fixture.store.scalar("SELECT COUNT(*) FROM mem_atom") == 0
        assert await fixture.store.scalar("SELECT COUNT(*) FROM msg_archive") == 1
        assert calls == ["preview", "confirm", "phrase", "backup", "acknowledge", "execute"]
        assert not errors, errors
        await page.locator("[data-reset-cancel]").click()
        assert await page.locator("#nj-memory-reset-dialog").count() == 0
        print(f"PASS {filename}: four confirmations + real handler/SQLite + "
              f"{'real 10s wait' if real_wait else 'controlled clocks'}")
    finally:
        await page.close()
        await fixture.asyncTearDown()
        fixture.doCleanups()


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            await check(browser, "night.js", real_wait=True)
            await check(browser, "night.1029c.js")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
