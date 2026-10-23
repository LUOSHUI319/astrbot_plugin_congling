/**
 * 渲染自检：用最小 DOM stub 跑一遍 app.js，检查每页产出的 HTML 结构。
 *
 * 检查三件事：
 * 1. 设置是否按板块分散（而不是全堆在一页）
 * 2. 每页核心配置项是否收敛（简洁）
 * 3. 人格页在不同人设来源下内容是否真的切换
 *
 * 跑法：node tests/render_check.mjs
 */

const DATA = JSON.parse(process.env.CONGLING_PREVIEW_DATA || "{}");

function makeEl(id) {
  const el = {
    id,
    innerHTML: "",
    textContent: "",
    style: {},
    dataset: {},
    firstElementChild: null,
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    querySelector: (sel) => makeEl(`qs-${id}-${sel}`),
    querySelectorAll: () => [],
    appendChild() {},
    addEventListener() {},
    scrollIntoView() {},
    closest: () => null,
    onclick: null,
    onchange: null,
    onkeydown: null,
    value: "",
    checked: false,
    open: false,
  };
  return el;
}

const registry = new Map();
function byId(id) {
  if (!registry.has(id)) registry.set(id, makeEl(id));
  return registry.get(id);
}

const navButtons = ["overview", "persona", "behavior", "memory", "tools", "qq", "system"].map((viewName) => {
  const btn = makeEl(`nav-${viewName}`);
  btn.dataset = { view: viewName };
  return btn;
});

globalThis.window = globalThis;
globalThis.document = {
  getElementById: byId,
  querySelectorAll: (sel) => (sel === ".nav-item" ? navButtons : []),
  querySelector: () => null,
  createElement: (tag) => {
    const el = makeEl(`created-${tag}`);
    el.firstElementChild = makeEl(`child-${tag}`);
    return el;
  },
  addEventListener() {},
};

const views = {};
globalThis.AstrBotPluginPage = {
  ready: async () => ({}),
  apiGet: async (path) => ({ status: "ok", data: DATA[path] || {} }),
  apiPost: async () => ({ status: "ok", data: { applied: 3, saved: true } }),
};

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  await import("../pages/fromling/app.js");
  await wait(120);

  const viewEl = byId("view");
  const results = {};
  const titles = ["persona", "behavior", "memory", "tools", "qq", "system", "overview"];

  for (const name of titles) {
    const btn = navButtons.find((item) => item.dataset.view === name);
    viewEl.innerHTML = "";
    byId("persona-body").innerHTML = "";
    if (btn && typeof btn.onclick === "function") btn.onclick();
    await wait(120);
    // 人格正文写在 #persona-body 里，一起收进来
    results[name] = viewEl.innerHTML + (name === "persona" ? byId("persona-body").innerHTML : "");
  }

  function stat(html, pattern) {
    return (html.match(pattern) || []).length;
  }

  const summary = {};
  for (const [name, html] of Object.entries(results)) {
    summary[name] = {
      bytes: html.length,
      cards: stat(html, /class="cfg-group"/g),
      advanced: stat(html, /<details class="adv"/g),
      paths: stat(html, /data-path="/g),
      rows: stat(html, /class="cfg-row"/g),
      empty: html.includes("读取中") || html.includes("出错了"),
    };
  }

  console.log("=== 每页结构 ===");
  for (const [name, item] of Object.entries(summary)) {
    console.log(
      `${name.padEnd(9)} bytes=${String(item.bytes).padStart(5)} cards=${item.cards} advanced=${item.advanced} rows=${item.rows} paths=${item.paths}${item.empty ? "  !! 空/出错" : ""}`
    );
  }

  const problems = [];
  for (const [name, item] of Object.entries(summary)) {
    if (item.empty) problems.push(`${name} 页渲染为空`);
  }
  if (summary.behavior.cards < 3) problems.push("行为页应有 3 个配置卡（拟人/风格/感知）");
  if (summary.memory.cards < 1) problems.push("记忆页缺少记忆配置卡");
  if (summary.tools.cards < 1) problems.push("工具页缺少工具配置卡");
  if (summary.qq.cards < 1) problems.push("QQ 页缺少扩展配置卡");
  if (summary.system.cards < 3) problems.push("系统页应有 3 个配置卡（范围/共存/调试）");
  if (!results.behavior.includes("高级设置")) problems.push("行为页没有高级折叠，说明核心项可能过多");
  if (summary.persona.rows > 6) problems.push("人格页核心项过多，不够简洁");
  if (results.overview.includes("模型能力")) problems.push("概览页还在显示模型能力块");
  if (!results.system.includes("MiniMax 多模态工具")) problems.push("系统页没有 mmx 工具状态");

  console.log("\n=== 人格页联动 ===");
  const personaBody = byId("persona-body");
  const builtinHtml = results.persona;
  if (!builtinHtml.includes('id="p-identity"')) problems.push("builtin 模式没有内置人设编辑框");
  if (!builtinHtml.includes("内置人设")) problems.push("builtin 模式缺少内置人设标题");

  // 切到 astrbot 模式再看一次
  DATA["page/persona"].mode = "astrbot";
  DATA["page/persona"].astrbot_persona_id = "luoshui";
  viewEl.innerHTML = "";
  navButtons.find((item) => item.dataset.view === "persona").onclick();
  await wait(150);
  const astrHtml = personaBody.innerHTML;
  console.log("astrbot 模式 bytes =", astrHtml.length);
  if (!astrHtml.includes("选一个 AstrBot 人格")) problems.push("astrbot 模式没有出现人格选择区");
  if (!astrHtml.includes("你是洛水")) problems.push("astrbot 模式没有显示选中人格的正文");
  if (astrHtml.includes('id="p-identity"')) problems.push("astrbot 模式仍然显示内置人设编辑框（应随选择切换）");

  // 切到 none
  DATA["page/persona"].mode = "none";
  viewEl.innerHTML = "";
  navButtons.find((item) => item.dataset.view === "persona").onclick();
  await wait(150);
  const noneHtml = personaBody.innerHTML;
  console.log("none 模式 bytes =", noneHtml.length);
  if (!noneHtml.includes("不用插件人设")) problems.push("none 模式没有说明卡");
  if (noneHtml.includes('id="p-identity"')) problems.push("none 模式仍然显示内置人设编辑框");

  if (problems.length) {
    console.log("\n=== 问题 ===");
    problems.forEach((item) => console.log("-", item));
    process.exit(1);
  }
  console.log("\n渲染自检通过");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
