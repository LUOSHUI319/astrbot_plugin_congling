/* 离线浏览器验收：不连接 AstrBot，不发送聊天；需可选 playwright-core。 */
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright-core");
const root = path.resolve(__dirname, "../pages/fromling");
const schema = JSON.parse(fs.readFileSync(path.join(root, "../../_conf_schema.json")));
const groups = Object.entries(schema).map(([name, spec]) => ({
  name, title: spec.description,
  items: Object.entries(spec.items || {}).flatMap(([key, item]) =>
    item.type === "object"
      ? Object.entries(item.items || {}).map(([sub, field]) => ({
          key: `${key}.${sub}`, path: `${name}.${key}.${sub}`, label: field.description,
          hint: field.hint || "", type: field.type, value: field.default, options: field.options || [],
        }))
      : [{ key, path: `${name}.${key}`, label: item.description, hint: item.hint || "",
           type: item.type, value: item.default, options: item.options || [] }]),
}));
const bridge = `
window.__posts = [];
window.__persona = {
  mode:"astrbot",astrbot_persona_id:"大小姐",persona_id:"ling",name:"绫",identity:"你叫绫",
  style:{max_chars:25,habits:["嗯"],tone:"平静"},versions:[],
  effective:{mode:"astrbot",id:"大小姐",name:"大小姐",token:"A"},
  astrbot_personas:{default:"大小姐",items:[
    {id:"大小姐",name:"大小姐",prompt:"你是大小姐"},
    {id:"另一个",name:"另一个",prompt:"你是另一个"},
  ]},
};
window.AstrBotPluginPage = {
  ready:async()=>({}),
  apiGet:async(p)=>({status:"ok",data:p==="page/settings"?{groups:${JSON.stringify(groups)}}:
    p==="page/persona"?window.__persona:
    p==="page/night/state"?{enabled:true,mode:"awake",energy:100,energy_max:100,waking_timeout_minutes:5}:
    p==="page/night/logs"?{items:[
      {id:2,ts:1700000060,event:"message_drain",label:"收到消息扣精力",energy_before:100,energy_after:98,delta:-2,details:{rate:2}},
      {id:1,ts:1700000000,event:"awake_recover",label:"非疲劳时段恢复",energy_before:95,energy_after:100,delta:5,details:{rate:1.5,minutes:4}},
    ],observed_since:1700000000}:{} }),
  apiPost:async(p,data)=>{
    window.__posts.push({p,data});
    if(p==="page/persona/save"){
      Object.assign(window.__persona,data);
      const mode=window.__persona.mode;
      const id=mode==="astrbot"?(window.__persona.astrbot_persona_id||"大小姐"):mode==="none"?"none":"ling";
      window.__persona.effective={mode,id,name:mode==="none"?"bot":mode==="builtin"?"绫":id,token:id};
      return {status:"ok",data:window.__persona};
    }
    return {status:"ok",data:{saved:true,applied:Object.keys(data.changes||{}).length}};
  },
};`;

(async () => {
  const browser = await chromium.launch({
    ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}), headless: true,
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    const serveFixture = async route => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/plugin/page/bridge-sdk.js")
        return route.fulfill({ contentType: "application/javascript", body: bridge });
      const file = path.join(root, url.pathname === "/" ? "index.html" : url.pathname);
      if (!file.startsWith(root + path.sep) || !fs.existsSync(file))
        return route.fulfill({ status: 404, body: "" });
      return route.fulfill({
        contentType: ({ ".html": "text/html", ".js": "application/javascript", ".css": "text/css" })[path.extname(file)]
          || "application/octet-stream", body: fs.readFileSync(file),
      });
    };
    await page.route("**/*", serveFixture);
    await page.goto("http://congling-ui.test/");
    await page.locator('[data-view="persona"]').click();
    await page.locator("#p-astr").waitFor();
    assert.equal(await page.locator("#nj-persona-name").innerText(), "大小姐");
    assert.equal(await page.evaluate(() => window.__posts.length), 0);
    await page.locator("#p-astr").selectOption("另一个");
    await page.waitForFunction(() => window.__persona.astrbot_persona_id === "另一个"
      && document.querySelector("#p-saved").textContent === "已立即生效");
    assert.equal(await page.locator(".nj-herobar h2").innerText(), "另一个");
    await page.locator('.nj-mode[data-mode="builtin"]').click();
    await page.locator("#p-identity").waitFor();
    await page.locator('.nj-mode[data-mode="none"]').click();
    await page.waitForFunction(() => window.__persona.mode === "none");
    assert.equal(await page.locator("#nj-persona-name").innerText(), "bot");
    await page.locator('.nj-mode[data-mode="astrbot"]').click();
    await page.locator("#p-astr").waitFor();
    assert.equal(await page.locator("#p-astr").inputValue(), "另一个");
    await page.locator("#p-astr").selectOption("大小姐");
    await page.waitForFunction(() => window.__persona.astrbot_persona_id === "大小姐");
    assert.equal(await page.evaluate(() => window.__posts.length), 5);

    await page.locator('[data-view="night"]').click();
    await page.locator('#nj-night-tabs [data-night-tab="config"]').click();
    const timeout = page.locator('[data-path="night.waking_timeout_minutes"]');
    await timeout.waitFor();
    const card = timeout.locator("xpath=ancestor::section[1]");
    assert.equal(await card.locator("[data-path]").count(), 12);
    await timeout.fill("8");
    await page.getByRole("button", { name: "查看醒后补课配置说明", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "醒后补课 · 配置说明" });
    await dialog.waitFor();
    assert.equal(await dialog.locator("[data-help-key]").count(), 12);
    assert.match(await dialog.locator('[data-help-key="waking_timeout_minutes"]').innerText(), /当前填写：8/);
    assert.match(await dialog.locator('[data-help-key="rollup.wake_idle_minutes"]').innerText(), /精力未满/);
    await dialog.getByLabel("查找参数").fill("静默");
    assert.ok(await dialog.locator("[data-help-key]:visible").count() < 12);
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "hidden" });
    assert.equal(await timeout.inputValue(), "8");
    assert.equal(await page.evaluate(() => window.__posts.length), 5);

    await page.locator('#nj-night-tabs [data-night-tab="log"]').click();
    await page.locator(".night-log-row").first().waitFor();
    assert.equal(await page.locator(".night-log-row").count(), 2);
    assert.equal(await page.locator(".night-log-chart polyline").count(), 1);
    await page.locator("#night-log-category").selectOption("state");
    await page.locator("#night-log-auto").click();
    assert.equal(await page.locator("#night-log-auto").getAttribute("aria-pressed"), "false");

    // 1.1.1 手机端为独立页面，验证自动跳转、人格设置与返回桌面防循环。
    const mobile = await browser.newPage({
      viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true,
      userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148",
    });
    mobile.on("pageerror", e => errors.push(e.message));
    await mobile.route("**/*", serveFixture);
    await mobile.goto("http://congling-ui.test/");
    await mobile.waitForURL("**/mobile.html");
    await mobile.locator("#m-tabbar").waitFor();
    assert.equal(await mobile.locator("#m-tabbar [data-tab]").count(), 5);
    await mobile.locator('#m-tabbar [data-tab="more"]').click();
    await mobile.locator('.m-nav-row[data-act="open"][data-page="persona"]').click();
    await mobile.locator("#m-persona-identity").waitFor();
    assert.match(await mobile.locator("#m-main").innerText(), /当前生效：大小姐/);
    await mobile.getByRole("button", { name: "选择人格来源", exact: true }).click();
    const mode = mobile.locator('[data-path="persona.mode"]');
    await mode.waitFor();
    assert.equal(await mode.locator("option").count(), 3);
    assert.equal(await mobile.locator('[data-path="persona.astrbot_persona_id"]').count(), 1);
    assert.equal(await mobile.locator('[data-path="persona.persona_id"]').count(), 0);
    await mode.selectOption("none");
    await mobile.locator('#m-savedock [data-act="settings-save"]').click();
    await mobile.waitForFunction(() => window.__posts.length === 1);
    assert.deepEqual(await mobile.evaluate(() => window.__posts[0]), {
      p: "page/settings/save", data: { changes: { "persona.mode": "none" } },
    });
    await mobile.goto("http://congling-ui.test/index.html?desk=1");
    await mobile.locator("#nav").waitFor();
    assert.equal(new URL(mobile.url()).pathname, "/index.html");
    assert.equal(new URL(mobile.url()).searchParams.get("desk"), "1");
    assert.equal(await mobile.locator("#layout-toggle").count(), 0);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ ok: true, immediate_saves: 5, help: true, logs: true,
      mobile: true, mobile_persona_settings: true, desktop_return: true,
      console_errors: 0, live_requests: 0 }));
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
