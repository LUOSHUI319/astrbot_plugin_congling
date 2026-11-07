/**
 * 解包自检：三份前端的 unwrap() 必须行为一致，且不能把已经解过一层的载荷再解一次。
 *
 * 背景：AstrBot 新版页面 bridge 在宿主侧已经做过 a.data.data ?? a.data，
 * 送到页面里的可能直接是业务载荷。QQ 空间的 feed 载荷自带 ok 字段
 * （{"ok": true, "items": [...]}），旧的 unwrap 见到 ok === true 又取一次 .data，
 * 结果拿到 undefined，页面再读 .items 就报
 * "Cannot read properties of undefined (reading 'items')"。
 *
 * 跑法：node tests/unwrap_check.mjs
 */

import { readFileSync } from "node:fs";

const FILES = ["app.js", "app.1029c.js", "mobile.js"];

function extractUnwrap(source, name) {
  const start = source.indexOf("function unwrap(resp) {");
  if (start < 0) throw new Error(`${name}: 找不到 unwrap()`);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i += 1) {
    const ch = source[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) {
        const body = source.slice(start, i + 1);
        return new Function(`${body}\nreturn unwrap;`)();
      }
    }
  }
  throw new Error(`${name}: unwrap() 大括号不闭合`);
}

// 宿主已解包后的真实空间 feed 载荷（带 ok、items、text、target）
const BUSINESS = {
  ok: true,
  items: [{ fid: "x", uin: 307062828, nickname: "Sense", content: "哈！！！" }],
  text: "Sense的空间：哈！！！",
  target: { uin: 307062828, label: "Sense" },
};
// 老版 bridge 交给页面的整包结构
const LEGACY_OK = { status: "ok", ok: true, data: { items: [1, 2] }, ts: 1 };
const LEGACY_ERR = {
  status: "error",
  ok: false,
  message: "空间功能没接上",
  error: "空间功能没接上",
  data: {},
  ts: 1,
};

function run(name, unwrap) {
  const fails = [];
  const ok = (cond, msg) => {
    if (!cond) fails.push(`${name}: ${msg}`);
  };

  const business = unwrap(BUSINESS);
  ok(business && business.items && business.items.length === 1, "业务载荷被多解了一层");
  ok(business && business.target && business.target.uin === 307062828, "业务载荷丢了 target");

  const legacy = unwrap(LEGACY_OK);
  ok(legacy && legacy.items && legacy.items.length === 2, "老格式 data 没被解包");

  ok(JSON.stringify(unwrap(null)) === "{}", "null 应返回空对象");

  let errMsg = "";
  try {
    unwrap(LEGACY_ERR);
  } catch (e) {
    errMsg = e.message;
  }
  ok(errMsg === "空间功能没接上", "错误格式没有抛出 message");

  return fails;
}

let allFails = [];
const results = new Map();
for (const file of FILES) {
  const url = new URL(`../pages/fromling/${file}`, import.meta.url);
  const source = readFileSync(url, "utf8");
  const unwrap = extractUnwrap(source, file);
  results.set(file, String(JSON.stringify(unwrap(BUSINESS))));
  allFails = allFails.concat(run(file, unwrap));
}

const shapes = new Set(results.values());
if (shapes.size !== 1) {
  allFails.push(`三份 unwrap 结果不一致: ${[...results.entries()].map(([f, v]) => `${f}=${v.slice(0, 40)}`).join(" | ")}`);
}

if (allFails.length) {
  console.error("unwrap 自检失败：");
  for (const msg of allFails) console.error(`  - ${msg}`);
  process.exit(1);
}
console.log(`unwrap 自检通过（${FILES.length} 份文件，5 组用例，结果一致）`);
