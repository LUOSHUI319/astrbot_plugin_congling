/**
 * 日记本删除自检：条目里必须留着后端给的 id，删除必须真的等后端回话。
 *
 * 背景：normalizeEntries() 重新拼对象时漏了 id，API.deleteDiary() 里
 *   if (!row || !row.id) return Promise.resolve(false);
 * 于是请求根本没发出去，前端自己 splice 掉一条，刷新就回来了。
 *
 * 跑法：node tests/diary_delete_check.mjs
 */

import { readFileSync } from "node:fs";

const FILES = ["diarybook.js", "night.js", "night.1029c.js"];

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`找不到 ${name}()`);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    else if (source[i] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`${name}() 大括号不闭合`);
}

const sortEntries = (list) =>
  list.slice().sort((a, b) => String(b.day_key).localeCompare(String(a.day_key)));
const countCharacters = (text) => Array.from(String(text == null ? "" : text)).length;

const rows = [
  { id: 7, day_key: "2026-11-05", text: "夜", pinned: 1 },
  { day_key: "2026-11-04", text: "没有 id 的一条" },
];

const fails = [];
for (const file of FILES) {
  const source = readFileSync(new URL(`../pages/fromling/${file}`, import.meta.url), "utf8");

  let normalizeEntries;
  try {
    const body = extractFunction(source, "normalizeEntries");
    normalizeEntries = new Function(
      "sortEntries",
      "countCharacters",
      `${body}\nreturn normalizeEntries;`,
    )(sortEntries, countCharacters);
  } catch (err) {
    fails.push(`${file}: ${err.message}`);
    continue;
  }

  let out = [];
  try {
    out = normalizeEntries(rows);
  } catch (err) {
    fails.push(`${file}: normalizeEntries 抛错 ${err.message}`);
    continue;
  }

  const withId = out.find((item) => item.day_key === "2026-11-05");
  const withoutId = out.find((item) => item.day_key === "2026-11-04");
  if (!withId || withId.id !== 7) fails.push(`${file}: 条目丢了后端 id`);
  if (!withoutId || withoutId.id !== 0) fails.push(`${file}: 缺 id 时应回落成 0`);

  if (!source.includes("await API.deleteDiary(")) {
    fails.push(`${file}: 删除没有等待后端结果`);
  }
  if (source.includes("未请求服务端")) {
    fails.push(`${file}: 还留着「未请求服务端」的假提示`);
  }
}

if (fails.length) {
  console.error("日记删除自检失败：");
  for (const msg of fails) console.error(`  - ${msg}`);
  process.exit(1);
}
console.log(`日记删除自检通过（${FILES.length} 份文件，id 保留 + 删除真的走后端）`);
