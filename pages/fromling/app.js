/* 从绫开始 · 插件页面
 * 原则：每个板块的设置在它自己的页面里，不堆在最后。
 * 每张配置卡只摆核心几项，其余收进「高级」。
 */

const view = document.getElementById("view");
const titleEl = document.getElementById("title");
const personaTag = document.getElementById("persona-tag");
const compatLine = document.getElementById("compat-line");

/* ------------------------------------------------------------------ 桥接 */
let bridgePromise = null;


/* ------------------------------------------------------------------ 外观 */
const THEMES = [
  { id: "moon", name: "月白", color: "#35685a" },
  { id: "mist", name: "雾蓝", color: "#35617d" },
  { id: "sand", name: "暖砂", color: "#8a6b3f" },
  { id: "lotus", name: "藕荷", color: "#6d5a80" },
  { id: "celadon", name: "青瓷", color: "#3f7a72" },
  { id: "graphite", name: "墨灰", color: "#3d4450" },
  { id: "dark", name: "深色", color: "#16171a" },
];

const FONTS = [
  { id: "sans", name: "黑体", sample: "从" },
  { id: "serif", name: "宋体", sample: "从" },
  { id: "kai", name: "楷体", sample: "从" },
  { id: "round", name: "圆润", sample: "从" },
  { id: "mono", name: "等宽", sample: "从" },
];

const LOOK_KEY = "congling.look";

function readLook() {
  try {
    const raw = localStorage.getItem(LOOK_KEY);
    const data = raw ? JSON.parse(raw) : {};
    return { theme: data.theme || "moon", font: data.font || "sans" };
  } catch (err) {
    return { theme: "moon", font: "sans" };
  }
}

function applyLook(look) {
  const root = document.documentElement;
  if (look.theme && look.theme !== "moon") root.dataset.theme = look.theme;
  else root.removeAttribute("data-theme");
  if (look.font && look.font !== "sans") root.dataset.font = look.font;
  else root.removeAttribute("data-font");
  try {
    localStorage.setItem(LOOK_KEY, JSON.stringify(look));
  } catch (err) {
    /* 存不了就算了 */
  }
}

function paintPickers(look) {
  const theme = THEMES.find((t) => t.id === look.theme) || THEMES[0];
  const dot = document.querySelector("#theme-picker .picker-dot");
  if (dot) dot.style.background = theme.color;

  const themeMenu = document.getElementById("theme-menu");
  if (themeMenu) {
    themeMenu.innerHTML = THEMES.map(
      (item) => `<button class="picker-item ${item.id === look.theme ? "is-on" : ""}" data-theme-id="${item.id}">
        <span class="picker-swatch" style="background:${item.color}"></span>${esc(item.name)}
      </button>`
    ).join("");
  }

  const fontMenu = document.getElementById("font-menu");
  if (fontMenu) {
    fontMenu.innerHTML = FONTS.map(
      (item) => `<button class="picker-item ${item.id === look.font ? "is-on" : ""}" data-font-id="${item.id}">
        <span style="font-size:13px;">${esc(item.sample)}</span>${esc(item.name)}
      </button>`
    ).join("");
  }
}

function initLook() {
  const look = readLook();
  applyLook(look);
  paintPickers(look);

  const pickers = [
    ["theme-picker", "theme", "theme-menu"],
    ["font-picker", "font", "font-menu"],
  ];

  pickers.forEach(([pickerId, field, menuId]) => {
    const picker = document.getElementById(pickerId);
    const btn = picker ? picker.querySelector(".picker-btn") : null;
    if (!picker || !btn) return;

    btn.onclick = (event) => {
      event.stopPropagation();
      document.querySelectorAll(".picker.is-open").forEach((node) => {
        if (node !== picker) node.classList.remove("is-open");
      });
      picker.classList.toggle("is-open");
    };

    picker.querySelectorAll(".picker-item").forEach((item) => {});
  });

  document.addEventListener("click", (event) => {
    const themeBtn = event.target.closest("[data-theme-id]");
    const fontBtn = event.target.closest("[data-font-id]");
    if (themeBtn || fontBtn) {
      const next = readLook();
      if (themeBtn) next.theme = themeBtn.dataset.themeId;
      if (fontBtn) next.font = fontBtn.dataset.fontId;
      applyLook(next);
      paintPickers(next);
      document.querySelectorAll(".picker.is-open").forEach((node) => node.classList.remove("is-open"));
      return;
    }
    if (!event.target.closest(".picker")) {
      document.querySelectorAll(".picker.is-open").forEach((node) => node.classList.remove("is-open"));
    }
  });
}

function ensureBridge() {
  if (bridgePromise) return bridgePromise;
  bridgePromise = (async () => {
    const started = Date.now();
    while (!window.AstrBotPluginPage && Date.now() - started < 4000) {
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    const api = window.AstrBotPluginPage;
    if (!api) throw new Error("页面桥接未加载，请刷新页面");
    try {
      if (typeof api.ready === "function") await api.ready();
    } catch (err) {
      /* ready 失败不阻塞请求 */
    }
    return api;
  })();
  return bridgePromise;
}

function unwrap(resp) {
  if (resp == null) return {};
  if (resp.status === "ok") return resp.data;
  if (resp.ok === true) return resp.data;
  if (resp.status === "error") throw new Error(resp.message || "请求失败");
  if (resp.ok === false) throw new Error(resp.error || "请求失败");
  return resp;
}

function apiPath(path) {
  // AstrBot 新版的 bridge 走 /api/v1/plugins/extensions/{插件名}/{endpoint}，
  // 而插件的 web api 一律注册在 /{插件名}/page/... 下面。
  // endpoint 少这一层 page 就会 404，前端 catch 成空对象，
  // 表现出来就是「设置能存但状态永远是关的」。
  let p = String(path == null ? "" : path).replace(/^\/+/, "");
  if (!p.startsWith("page/")) p = `page/${p}`;
  return p;
}

async function get(path, params) {
  const api = await ensureBridge();
  return unwrap(await api.apiGet(apiPath(path), params || {}));
}

async function post(path, body) {
  const api = await ensureBridge();
  return unwrap(await api.apiPost(apiPath(path), body || {}));
}

/* ------------------------------------------------------------------ 工具 */
function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(Number(ts) * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function firstSentence(text, limit) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (!clean) return "";
  const cut = clean.split(/[。；;]/)[0];
  const out = cut.length > 8 ? cut : clean;
  return out.length > (limit || 42) ? out.slice(0, limit || 42) + "…" : out;
}

/* ------------------------------------------------------------------ 长夜 */
const NIGHT_LEVEL_TONE = [
  "",
  "var(--ink-faint)",
  "#8fa39b",
  "#6b8f83",
  "#4a7264",
  "var(--accent)",
];

/* 睡眠页要的配置项（其余归梦境/记忆） */
const NIGHT_SLEEP_KEYS = [
  "noon_fatigue",
  "night_fatigue",
  "wake_time",
  "wake_jitter_minutes",
  "latest_sleep_time",
  "wake_mode",
  "wake_window_minutes",
  "wake_start_count",
  "wake_step_count",
  "farewell_before_sleep",
  "farewell_same",
  "farewell_random",
  "farewell_use_llm",
  "min_turns_before_sleep",
];
const NIGHT_ENERGY_KEYS = [
  "energy_max",
  "energy_per_message",
  "energy_per_minute",
  "energy_recover_per_minute",
  "energy_after_wake",
];
const NIGHT_MEMORY_KEYS = ["crawl_enabled", "crawl_use_llm"];
const NIGHT_DREAM_KEYS = [
  "dream_enabled",
  "dream_start_after_minutes",
  "dream_interval_minutes",
  "dream_max_per_sleep",
  "noon_dream_chance",
  "dream_remember_by_level",
  "dream_tags",
  "dream_extra_tags",
];

let nightTab = "state";

function energyTone(percent) {
  if (percent > 60) return "is-good";
  if (percent > 25) return "is-mid";
  return "is-low";
}

function energyWord(percent) {
  if (percent > 75) return "精神不错";
  if (percent > 55) return "有点乏了";
  if (percent > 30) return "开始犯困";
  return "快撑不住了";
}

function levelStars(level) {
  const n = Math.max(0, Math.min(5, Number(level) || 0));
  let html = "";
  for (let i = 1; i <= 5; i += 1) {
    html += `<i class="${i <= n ? "on" : "off"}">★</i>`;
  }
  return html;
}

function clockOf(ts) {
  if (!ts) return "";
  const d = new Date(Number(ts) * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 从分组里挑出指定 key，包成一个伪分组 */
function sliceItems(group, keys) {
  if (!group) return [];
  return group.items.filter((item) => keys.includes(item.key));
}

/** 长夜的设置卡：全部平铺，不折进「高级」 */

/* ---- 长夜：不给手填，全用控件 ---- */
const NIGHT_NUM_RANGE = {
  wake_jitter_minutes: [0, 90, 1],
  wake_window_minutes: [1, 60, 1],
  wake_start_count: [1, 10, 1],
  wake_step_count: [1, 10, 1],
  energy_max: [10, 200, 10],
  energy_per_message: [0, 20, 0.5],
  energy_per_minute: [0, 10, 0.1],
  energy_recover_per_minute: [0, 10, 0.1],
  energy_after_wake: [0, 100, 5],
  min_turns_before_sleep: [0, 30, 1],
  dream_start_after_minutes: [10, 300, 10],
  dream_interval_minutes: [10, 180, 5],
  dream_max_per_sleep: [1, 12, 1],
};

const NIGHT_PERCENT_KEYS = ["noon_dream_chance"];

function splitTimeRange(raw) {
  const text = String(raw == null ? "" : raw).trim();
  const match = text.match(/^\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*$/);
  if (!match) return [text.match(/^\d{1,2}:\d{2}$/) ? text : "", ""];
  return [match[1], match[2]];
}

function timeRangeControl(item) {
  const [start, end] = splitTimeRange(item.value);
  return `<span class="time-range" data-path="${esc(item.path)}" data-type="timerange">
    <input type="time" class="tr-a" value="${esc(start)}" />
    <span class="tr-sep">到</span>
    <input type="time" class="tr-b" value="${esc(end)}" />
  </span>`;
}

function percentControl(item) {
  const raw = Number(item.value);
  const value = Number.isFinite(raw) ? Math.max(0, Math.min(1, raw)) : 0.5;
  return `<span class="pct-wrap">
    <input type="range" min="0" max="1" step="0.05" value="${value}"
      data-path="${esc(item.path)}" data-type="float" class="pct-input" />
    <span class="pct-num mono">${Math.round(value * 100)}%</span>
  </span>`;
}

function nightControl(item) {
  const key = item.key;
  if (item.type === "bool" || (Array.isArray(item.options) && item.options.length)) {
    return controlHtml(item);
  }
  if (key.endsWith("_fatigue")) return timeRangeControl(item);
  if (key === "wake_time" || key === "latest_sleep_time") {
    const value = String(item.value || "").match(/^\d{1,2}:\d{2}$/) ? item.value : "";
    return `<input type="time" data-path="${esc(item.path)}" data-type="string" value="${esc(value)}" />`;
  }
  if (NIGHT_PERCENT_KEYS.includes(key)) return percentControl(item);
  if (item.type === "int" || item.type === "float") {
    const range = NIGHT_NUM_RANGE[key];
    const min = range ? range[0] : 0;
    const max = range ? range[1] : 999;
    const step = range ? range[2] : item.type === "int" ? 1 : 0.1;
    return `<input type="number" min="${min}" max="${max}" step="${step}"
      data-path="${esc(item.path)}" data-type="${esc(item.type)}" value="${esc(item.value)}" />`;
  }
  return controlHtml(item);
}

function nightRowHtml(item) {
  const hint = item.hint ? `<span class="hint">${esc(firstSentence(item.hint))}</span>` : "";
  return `<div class="cfg-row">
    <div class="cfg-label">${esc(item.label)}${hint}</div>
    <div>${nightControl(item)}</div>
  </div>`;
}

function nightCard(title, desc, items) {
  if (!items || !items.length) return "";
  return `<section class="cfg-group">
    <div class="cfg-head">
      <h3>${esc(title)}</h3>
      <p>${esc(desc)}</p>
    </div>
    ${items.map((item) => nightRowHtml(item)).join("")}
  </section>`;
}

/* ---- 状态区 ---- */
function nightHero(s) {
  const enabled = !!(s && s.enabled);
  const mode = (s && s.mode) || "awake";
  const energy = Number((s && s.energy) || 0);
  const max = Number((s && s.energy_max) || 100) || 100;
  const percent = Math.max(0, Math.min(100, Math.round((energy / max) * 100)));
  const tone = energyTone(percent);

  if (!enabled) {
    return `
      <div class="night-hero is-off">
        <div class="hero-main">
          <div class="hero-line"><span class="hero-pip"></span><span class="hero-title">长夜如梦没开</span></div>
          <div class="hero-sub">去「设置」里把总开关打开，它才会累会睡</div>
        </div>
      </div>`;
  }

  const asleep = mode === "asleep";
  const waking = mode === "waking";
  const title = asleep ? "睡着中" : waking ? "刚醒，还没爬起来" : s.fatigue ? "醒着 · 疲劳期" : "醒着";
  const sub = asleep
    ? `还剩 ${esc(s.remaining_text || "—")}，预计 ${esc(s.wake_window || "")} 醒`
    : waking
      ? "正在翻昨晚的消息"
      : `起床点 ${esc(s.wake_time || "08:00")}，浮动 ±${esc(s.wake_jitter || 0)} 分钟`;

  return `
    <div class="night-hero ${asleep ? "is-asleep" : ""}">
      <div class="hero-main">
        <div class="hero-line">
          <span class="hero-pip"></span>
          <span class="hero-title">${esc(title)}</span>
          ${s.fatigue && !asleep ? '<span class="hero-flag">疲劳</span>' : ""}
        </div>
        <div class="hero-sub">${sub}</div>
      </div>
      <div class="hero-energy">
        <div class="energy-top">
          <span class="energy-word">${esc(energyWord(percent))}</span>
          <span class="energy-num mono">${energy.toFixed(0)}<em>/${max}</em></span>
        </div>
        <div class="energy-bar ${tone}"><span style="width:${percent}%"></span></div>
      </div>
      <div class="hero-acts">
        <button class="ghost" id="night-wake">唤醒</button>
        <button class="ghost" id="night-sleep">去睡</button>
      </div>
    </div>`;
}

/* ---- 梦境 ---- */
function dreamRow(dream) {
  const level = Math.max(1, Math.min(5, Number(dream.level) || 1));
  const tone = NIGHT_LEVEL_TONE[level] || "var(--ink-faint)";
  const tags = (dream.tags || []).map((t) => `<span class="dream-tag">${esc(t)}</span>`).join("");
  const days = Number(dream.remaining_days || 0);
  const fade = dream.expired ? "已经忘了" : days <= 0 ? "明天就忘" : `还能记 ${days} 天`;
  return `
    <div class="dream-row" style="--lvl:${tone}">
      <div class="dream-body">
        <div class="dream-top">
          <span class="dream-stars">${levelStars(level)}</span>
          ${tags}
          <span class="dream-fade mono">${esc(fade)}</span>
        </div>
        <div class="dream-text">${esc(firstSentence(dream.text, 88))}</div>
        <div class="dream-time mono">${fmtTime(dream.created_at)}</div>
      </div>
      <button class="dream-del" data-id="${esc(dream.dream_id)}" title="删掉">×</button>
    </div>`;
}

function unreadRow(item) {
  return `
    <div class="unread-row">
      <span class="unread-clock mono">${clockOf(item.unread_since)}</span>
      <span class="unread-text">${esc(firstSentence(item.text, 56))}</span>
    </div>`;
}

/* ---- 各页内容 ---- */
function nightStatePage(data, unread) {
  return `
    ${nightHero(data)}
    <div class="night-block">
      <div class="block-head">
        <span class="block-title">未曾爬楼</span>
        <span class="block-note">${unread.length} 条没看</span>
        ${unread.length ? '<button class="mini" id="night-crawl">标记已爬</button>' : ""}
      </div>
      ${unread.length ? `<div class="unread-list">${unread.slice(0, 6).map(unreadRow).join("")}</div>` : '<div class="night-empty">没有没爬的消息</div>'}
    </div>`;
}

function infoRow(label, value, hint) {
  return `<div class="info-row">
    <span class="info-label">${esc(label)}</span>
    <span class="info-value">${esc(value)}</span>
    ${hint ? `<span class="info-hint">${esc(hint)}</span>` : ""}
  </div>`;
}


function nightSleepPage(state) {
  const s = state || {};
  const enabled = !!s.enabled;
  const mode = s.mode || "awake";
  const asleep = mode === "asleep";
  const energy = Number(s.energy || 0);
  const max = Number(s.energy_max || 100) || 100;
  const percent = max ? Math.round((energy / max) * 100) : 0;

  const fatigueText = s.fatigue === "night" ? "夜间疲劳期" : s.fatigue === "noon" ? "午间疲劳期" : "不在疲劳期";

  return `
    <div class="settings-intro">这里是它现在的作息，改设置去「设置」那一页。</div>
    ${nightHero(s)}
    <div class="info-card">
      ${infoRow("当前时段", fatigueText)}
      ${infoRow("夜间疲劳", s.night_fatigue || "—", "这个时段里精力往下掉")}
      ${infoRow("午间疲劳", s.noon_fatigue || "—")}
      ${infoRow("最晚睡觉", s.latest_sleep_time || "—", "到点无论如何都睡")}
      ${infoRow("起床点", `${s.wake_time || "08:00"} ± ${s.wake_jitter || 0} 分钟`)}
      ${infoRow("唤醒档位", s.wake_mode === "both" ? "私聊和群聊都能叫" : s.wake_mode === "never" ? "叫不醒" : "只在私聊里能叫", s.wake_window ? `叫醒窗口 ${s.wake_window}` : "")}
      ${infoRow("睡前道别", s.farewell_on ? "会道别" : "不说，直接睡")}
      ${infoRow("做梦", s.dream_on ? "开着" : "关着")}
      ${infoRow("爬楼", s.crawl_on ? "开着" : "关着")}
      ${infoRow("真的会睡", s.allow_sleep === false ? "不会，只是累" : "会", s.allow_sleep === false ? "「设置」里关掉了允许入睡" : "")}
    </div>
    ${!enabled ? '<div class="night-empty">长夜如梦没开，这些设置都不生效</div>' : ""}
    <div class="info-bar">
      <span class="info-bar-label">精力</span>
      <span class="info-bar-num mono">${energy.toFixed(0)}<em>/${max}</em></span>
      <div class="info-bar-track"><span class="${energyTone(percent)}" style="width:${percent}%"></span></div>
      <span class="info-bar-word">${esc(energyWord(percent))}</span>
    </div>
  `;
}


function nightMemoryPage(unread, state) {
  const s = state || {};
  return `
    <div class="settings-intro">睡着期间进记忆的话会打上「未曾爬楼」，醒来先翻一遍才算真的看过。</div>
    <div class="info-card">
      ${infoRow("爬楼机制", s.crawl_on ? "开着" : "关着")}
      ${infoRow("没看的条数", `${unread.length} 条`)}
    </div>
    <div class="night-block">
      <div class="block-head">
        <span class="block-title">未曾爬楼</span>
        <span class="block-note">${unread.length} 条</span>
        ${unread.length ? '<button class="mini" id="night-crawl">标记已爬</button>' : ""}
      </div>
      ${unread.length ? `<div class="unread-list">${unread.map(unreadRow).join("")}</div>` : '<div class="night-empty">没有没爬的消息</div>'}
    </div>
  `;
}


const DREAM_TAG_CLASS = {
  噩梦: "t-nightmare",
  美梦: "t-dream",
  反映现实: "t-real",
  天马行空: "t-weird",
};

function fadeBand(days, total) {
  const ratio = total > 0 ? Number(days) / Number(total) : 0;
  if (ratio > 0.8) return 0;
  if (ratio > 0.6) return 1;
  if (ratio > 0.4) return 2;
  if (ratio > 0.18) return 3;
  return 4;
}

function dreamLead(text) {
  const clean = String(text || "").trim();
  const cut = clean.split("。")[0];
  if (!cut) return clean;
  return cut.length > 26 ? clean.slice(0, 26) + "…" : cut + "。";
}

function dreamRest(text) {
  const clean = String(text || "").trim();
  const lead = dreamLead(clean);
  return clean.startsWith(lead) ? clean.slice(lead.length).trim() : clean;
}

function nightTitle(key) {
  const parts = String(key || "").split("-");
  if (parts.length < 3) return key || "不知哪一夜";
  const d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
  const week = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][d.getDay()];
  return `${Number(parts[1])}月${Number(parts[2])}日`;
}

function nightWeekday(key) {
  const parts = String(key || "").split("-");
  if (parts.length < 3) return "";
  const d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
  return ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][d.getDay()];
}

function clockOfTs(ts) {
  if (!ts) return "";
  const d = new Date(Number(ts) * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function dreamSegHtml(seg) {
  const band = fadeBand(seg.remaining_days, seg.total_days);
  const tags = (seg.tags || [])
    .map((name) => `<span class="seg-tag ${DREAM_TAG_CLASS[name] || ""}">${esc(name)}</span>`)
    .join("");
  const pinned = seg.pinned ? '<span class="seg-hold is-on">已拽住</span>' : "";
  const clock = clockOfTs(seg.created_at);
  const lead = dreamLead(seg.text);
  const rest = dreamRest(seg.text);
  return `
    <article class="seg lv${Math.max(1, Math.min(5, Number(seg.level) || 3))} fade-${band}">
      <div class="seg-top">
        <span class="seg-time mono">${esc(clock)}</span>
        ${tags}
        <span class="seg-left mono">还能记 ${esc(seg.remaining_days)} 天</span>
        <button class="seg-hold ${seg.pinned ? "is-on" : ""}" data-id="${esc(seg.dream_id)}" data-pin="${seg.pinned ? "1" : "0"}">${seg.pinned ? "松开" : "拽住"}</button>
      </div>
      <p class="seg-lead">${esc(lead)}</p>
      <p class="seg-text">${esc(rest)}</p>
      <div class="trace"></div>
    </article>`;
}

function nightBlockHtml(night) {
  const segs = night.segments || [];
  const first = segs[0] || {};
  const last = segs[segs.length - 1] || {};
  const range = segs.length > 1
    ? `${clockOfTs(first.created_at)}–${clockOfTs(last.created_at)}`
    : clockOfTs(first.created_at);
  const subtitle = `${nightWeekday(night.night_key)} · ${segs.length} 段 · ${range}`;
  return `
    <section class="dream-night" data-key="${esc(night.night_key)}">
      <div class="dream-night-head">
        <span class="dn-date">${esc(nightTitle(night.night_key))}</span>
        <span class="dn-rule"></span>
        <span class="dn-meta">${esc(subtitle)}</span>
      </div>
      <div class="dn-body">
        ${segs.map(dreamSegHtml).join("")}
      </div>
    </section>`;
}

function nightDreamPage(dreamData) {
  const nights = (dreamData && dreamData.nights) || [];
  const stats = (dreamData && dreamData.stats) || {};
  const alive = Number(stats.living || 0);
  return `
    <div class="settings-intro">睡着满一小时开始做梦，之后每小时一段。梦会按等级一天天淡掉，拽住了就不会散。</div>
    <div class="night-block">
      <div class="block-head">
        <span class="block-title">做过的梦</span>
        <span class="block-note">${alive} 段还在 / 共 ${esc(stats.total || 0)} 段</span>
      </div>
      ${nights.length ? `<div class="dream-nights">${nights.map(nightBlockHtml).join("")}</div>` : '<div class="night-empty">还没做过梦</div>'}
    </div>
  `;
}


function nightSettingsPage(settings) {
  const night = pickGroup(settings, "night");
  const close = pickGroup(settings, "close");
  return `
    <div class="settings-intro">所有开关和参数都在这一页，改完点下面的保存。</div>
    ${nightCard("总开关", "关掉之后它就不知道累，也不会自己睡", sliceItems(night, ["enabled"]))}
    ${nightCard("疲劳时段", "这两个时段里它会犯困、精力往下掉", sliceItems(night, ["noon_fatigue", "night_fatigue", "latest_sleep_time"]))}
    ${nightCard("起床", "固定起床点上下浮动，浮动上限 90 分钟", sliceItems(night, ["wake_time", "wake_jitter_minutes"]))}
    ${nightCard("唤醒", "睡着之后有多难叫醒。y 到 y+n 之间概率平分 100%", sliceItems(night, ["wake_mode", "wake_window_minutes", "wake_start_count", "wake_step_count"]))}
    ${nightCard("入睡前的道别", "多个会话一起道别时，要不要说同一句", sliceItems(night, ["farewell_before_sleep", "farewell_same", "farewell_random", "farewell_use_llm"]))}
    ${nightCard("精力", "疲劳期里怎么掉、平时怎么回", sliceItems(night, [...NIGHT_ENERGY_KEYS, "min_turns_before_sleep"]))}
    ${nightCard("爬楼", "关掉之后睡着的记忆就直接可读了", sliceItems(night, NIGHT_MEMORY_KEYS))}
    ${nightCard("梦境", "关掉就不做梦，开着 token 消耗会明显增加", sliceItems(night, NIGHT_DREAM_KEYS))}
    ${configCard(close, { title: "收线静默", desc: "聊到晚安、拜拜这种话就真的停下来" })}
    ${pageActions("保存本页设置")}
  `;
}

/* ---- 主渲染 ---- */
async function renderNight() {
  const TABS = [
    ["state", "状态"],
    ["sleep", "睡眠"],
    ["memory", "记忆"],
    ["dream", "梦境"],
    ["config", "设置"],
  ];
  if (!TABS.some(([key]) => key === nightTab)) nightTab = "state";

  const tabsHtml = `<div class="sub-tabs">${TABS.map(
    ([key, label]) =>
      `<button class="sub-tab ${nightTab === key ? "is-on" : ""}" data-tab="${key}">${label}</button>`
  ).join("")}</div>`;

  let body = "";
  let needSettings = nightTab === "config";
  let needDreams = nightTab === "dream";
  let needUnread = nightTab === "state" || nightTab === "memory";
  let needState = nightTab === "state" || nightTab === "sleep" || nightTab === "memory";

  const [stateData, settings, dreamData, unreadData] = await Promise.all([
    needState ? get("/night/state").catch(() => ({})) : Promise.resolve({}),
    needSettings ? loadSettings() : Promise.resolve(null),
    needDreams ? get("/night/dreams", { limit: 40 }).catch(() => ({ items: [], nights: [], stats: {} })) : Promise.resolve(null),
    needUnread ? get("/night/unread", { limit: 40 }).catch(() => ({ items: [] })) : Promise.resolve(null),
  ]);

  const dreams = (dreamData && dreamData.items) || [];
  const stats = (dreamData && dreamData.stats) || {};
  const unread = (unreadData && unreadData.items) || [];

  if (nightTab === "sleep") body = nightSleepPage(stateData);
  else if (nightTab === "memory") body = nightMemoryPage(unread, stateData);
  else if (nightTab === "dream") body = nightDreamPage(dreamData);
  else if (nightTab === "config") body = nightSettingsPage(settings);
  else body = nightStatePage(stateData, unread);

  view.innerHTML = `<div class="night-page">${tabsHtml}${body}</div>`;

  /* 子标签切换 */
  view.querySelectorAll(".sub-tab").forEach((btn) => {
    btn.onclick = () => {
      nightTab = btn.dataset.tab;
      render();
    };
  });

  /* 保存 */
  bindConfigCards(view);
  bindPageSave();

  /* 快捷操作 */
  const wakeBtn = document.getElementById("night-wake");
  if (wakeBtn) {
    wakeBtn.onclick = async () => {
      wakeBtn.disabled = true;
      try {
        await post("/night/wake");
        await renderNight();
      } catch (err) {
        alert(err.message || "唤醒失败");
        wakeBtn.disabled = false;
      }
    };
  }

  const sleepBtn = document.getElementById("night-sleep");
  if (sleepBtn) {
    sleepBtn.onclick = async () => {
      sleepBtn.disabled = true;
      try {
        await post("/night/sleep", { kind: "night" });
        await renderNight();
      } catch (err) {
        alert(err.message || "入睡失败");
        sleepBtn.disabled = false;
      }
    };
  }

  const crawlBtn = document.getElementById("night-crawl");
  if (crawlBtn) {
    crawlBtn.onclick = async () => {
      crawlBtn.disabled = true;
      try {
        await post("/night/crawl", {});
        settingsCache = null;
        await renderNight();
      } catch (err) {
        alert(err.message || "操作失败");
        crawlBtn.disabled = false;
      }
    };
  }

  view.querySelectorAll(".seg-hold").forEach((btn) => {
    btn.onclick = async () => {
      const pinned = btn.dataset.pin === "1";
      btn.disabled = true;
      try {
        await post("/night/dream/pin", { dream_id: btn.dataset.id, pinned: !pinned });
        await renderNight();
      } catch (err) {
        alert(err.message || "操作失败");
        btn.disabled = false;
      }
    };
  });

  view.querySelectorAll(".dream-del").forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await post("/night/dream/delete", { dream_id: btn.dataset.id });
        await renderNight();
      } catch (err) {
        alert(err.message || "删除失败");
        btn.disabled = false;
      }
    };
  });
}

/* ------------------------------------------------------------------ 路由 */
const VIEWS = {
  overview: { title: "概览", render: renderOverview },
  persona: { title: "人格", render: renderPersona },
  behavior: { title: "行为", render: renderBehavior },
  memory: { title: "记忆", render: renderMemory },
  night: { title: "长夜", render: renderNight },
  tools: { title: "工具", render: renderTools },
  qq: { title: "QQ", render: renderQQ },
  system: { title: "系统", render: renderSystem },
};

let currentView = "overview";
let state = { persona: null, compat: null, health: null };
let settingsCache = null;

function setView(name) {
  if (!VIEWS[name]) return;
  settingsCache = null;
  currentView = name;
  titleEl.textContent = VIEWS[name].title;
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.view === name);
  });
  render();
  view.scrollIntoView({ block: "start" });
}

async function render() {
  view.innerHTML = '<div class="empty">读取中…</div>';
  try {
    await VIEWS[currentView].render();
  } catch (err) {
    view.innerHTML = `<div class="empty">出错了：${esc(err.message)}</div>`;
  }
}

/* ------------------------------------------------------------------ 配置卡 */
/* 每个板块只摆核心项，其余收进「高级」。 */
const CORE_KEYS = {
  basic: ["enabled", "enable_private", "enable_group", "platforms"],
  persona: ["active_hours", "version_history_limit"],
  humanize: ["reply_threshold", "max_bubbles_private", "max_bubbles_group", "think_delay_base", "quote_probability"],
  guard: ["strip_emoji", "strip_trailing_period", "max_chars_per_bubble"],
  perception: ["expand_forward", "image_describe", "voice_transcribe"],
  memory: ["enabled", "inject_top_k", "idle_extract_seconds", "embedding_provider_id"],
  capability: ["enabled", "groups", "rate_limit_per_min"],
  qq_extra: ["qzone_mode", "poke_enabled", "qzone_allow_write"],
  compat: ["mode", "yield_humanize", "yield_memory", "yield_rhythm"],
  debug: ["decision_log", "verbose"],
};

async function loadSettings(force) {
  if (settingsCache && !force) return settingsCache;
  settingsCache = await get("page/settings");
  return settingsCache;
}

function pickGroup(data, name) {
  return (data.groups || []).find((group) => group.name === name) || null;
}

function controlHtml(item) {
  const path = esc(item.path);
  const value = item.value;
  if (item.type === "bool") {
    const on = value === true || value === "true" || value === 1;
    return `<label class="switch">
      <input type="checkbox" data-path="${path}" data-type="bool" ${on ? "checked" : ""} />
      <span>${on ? "开" : "关"}</span>
    </label>`;
  }
  if (Array.isArray(item.options) && item.options.length) {
    return `<select data-path="${path}" data-type="enum">${item.options
      .map(
        (opt) =>
          `<option value="${esc(opt)}" ${String(opt) === String(value) ? "selected" : ""}>${esc(opt)}</option>`
      )
      .join("")}</select>`;
  }
  if (item.type === "int" || item.type === "float") {
    return `<input type="number" step="${item.type === "int" ? "1" : "0.01"}"
      data-path="${path}" data-type="${esc(item.type)}" value="${esc(value)}" />`;
  }
  if (item.type === "text") {
    return `<textarea data-path="${path}" data-type="text">${esc(value)}</textarea>`;
  }
  if (item.type === "list") {
    const text = Array.isArray(value) ? value.join("\n") : String(value == null ? "" : value);
    return `<textarea data-path="${path}" data-type="list" placeholder="一行一条">${esc(text)}</textarea>`;
  }
  return `<input type="text" data-path="${path}" data-type="string" value="${esc(value)}" />`;
}

function rowHtml(item, compact) {
  const hint = item.hint ? `<span class="hint">${esc(compact ? firstSentence(item.hint) : item.hint)}</span>` : "";
  return `<div class="cfg-row">
    <div class="cfg-label">${esc(item.label)}${hint}</div>
    <div>${controlHtml(item)}</div>
  </div>`;
}

function configCard(group, options) {
  if (!group) return "";
  const opts = options || {};
  const coreKeys = CORE_KEYS[group.name] || [];
  const core = group.items.filter((item) => coreKeys.includes(item.key));
  const rest = group.items.filter((item) => !coreKeys.includes(item.key));
  const uid = `grp-${group.name}`;

  return `<section class="cfg-group" id="${uid}">
    <div class="cfg-head">
      <h3>${esc(opts.title || group.title)}</h3>
      <p>${esc(firstSentence(opts.desc || group.desc, 60))}</p>
    </div>
    ${core.map((item) => rowHtml(item, true)).join("")}
    ${
      rest.length
        ? `<details class="adv">
            <summary>高级设置（${rest.length} 项）</summary>
            ${rest.map((item) => rowHtml(item, false)).join("")}
          </details>`
        : ""
    }
  </section>`;
}

function collectChanges(root) {
  const changes = {};
  root.querySelectorAll("[data-path]").forEach((el) => {
    const path = el.dataset.path;
    const type = el.dataset.type;
    if (type === "bool") changes[path] = el.checked;
    else if (type === "int" || type === "float") changes[path] = Number(el.value);
    else if (type === "timerange") {
      const a = el.querySelector(".tr-a");
      const b = el.querySelector(".tr-b");
      const start = a ? a.value : "";
      const end = b ? b.value : "";
      changes[path] = start && end ? `${start}-${end}` : start || end || "";
    } else changes[path] = el.value;
  });
  return changes;
}

function bindConfigCards(root) {
  root.querySelectorAll(".switch input").forEach((box) => {
    box.onchange = () => {
      const label = box.parentElement.querySelector("span");
      if (label) label.textContent = box.checked ? "开" : "关";
    };
  });
  // 概率滑块：拖的时候数字跟着走
  root.querySelectorAll(".pct-input").forEach((slider) => {
    const num = slider.parentElement.querySelector(".pct-num");
    slider.oninput = () => {
      if (num) num.textContent = Math.round(Number(slider.value) * 100) + "%";
    };
  });
}

/** 每页一个保存按钮：提交本页所有配置项。 */
function bindPageSave() {
  const btn = document.getElementById("page-save");
  if (!btn) return;
  btn.onclick = async () => {
    const msg = document.getElementById("page-msg");
    if (msg) msg.textContent = "保存中…";
    try {
      const result = await post("page/settings/save", { changes: collectChanges(view) });
      settingsCache = null;
      const note = `已保存 ${result.applied} 项${result.saved ? "" : "（未写入文件）"}`;
      if (currentView === "night") await renderNight();
      const again = document.getElementById("page-msg");
      if (again) again.textContent = note;
    } catch (err) {
      if (msg) msg.textContent = err.message;
    }
  };
}

function pageActions(label) {
  return `<div class="page-actions">
    <button class="primary" id="page-save">${esc(label || "保存本页设置")}</button>
    <span class="saved" id="page-msg"></span>
  </div>`;
}

/* ------------------------------------------------------------------ 概览 */
async function renderOverview() {
  const data = await get("page/overview");
  state.health = data.health;
  state.compat = data.health.compat;
  const modules = data.health.modules || {};
  const stats = data.stats || {};
  personaTag.textContent = `${data.health.persona.name} · ${data.health.persona.id}`;

  view.innerHTML = `
    <div class="grid-stats">
      <div class="stat"><div class="num">${esc(data.memory_active)}</div><div class="lbl">活跃记忆</div></div>
      <div class="stat"><div class="num">${esc(stats.msg_archive || 0)}</div><div class="lbl">归档消息</div></div>
      <div class="stat"><div class="num">${esc(stats.decision_log || 0)}</div><div class="lbl">决策记录</div></div>
      <div class="stat"><div class="num">${Math.floor((data.health.uptime || 0) / 60)}</div><div class="lbl">运行分钟</div></div>
    </div>

    <h2 class="sec">各板块</h2>
    <div class="entry-grid">
      ${[
        ["persona", "人格", "人设来源、说话方式"],
        ["behavior", "行为", "回不回、怎么分句、看懂什么"],
        ["memory", "记忆", "记什么、忘什么、注入多少"],
        ["tools", "工具", "能查什么、限流与开关"],
        ["qq", "QQ", "签名、名片、空间"],
        ["system", "系统", "生效范围、与其他插件共存"],
      ]
        .map(
          ([key, name, desc]) => `<button class="entry" data-go="${key}">
            <h3>${esc(name)}</h3><p>${esc(desc)}</p>
            <span class="badge ${modules[key] === false ? "warn" : ""}">${
              key === "qq" && modules.qqext === false ? "未就绪" : "进入"
            }</span>
          </button>`
        )
        .join("")}
    </div>

    <h2 class="sec">模块状态</h2>
    <div class="chips">
      ${Object.keys(modules)
        .map(
          (key) =>
            `<span class="chip ${modules[key] ? "on" : "off"}">${esc(key)}${
              modules[key] ? "" : " · 降级"
            }</span>`
        )
        .join("")}
    </div>

    <h2 class="sec">最近决策</h2>
    ${renderDecisions((data.recent || []).slice(0, 6))}
  `;

  view.querySelectorAll("[data-go]").forEach((btn) => {
    btn.onclick = () => setView(btn.dataset.go);
  });
  updateCompatLine();
}

function renderDecisions(rows) {
  if (!rows.length) return '<div class="empty">还没有决策记录</div>';
  return `<table class="list">
    <thead><tr><th>时间</th><th>结果</th><th>依据</th></tr></thead>
    <tbody>
      ${rows
        .map(
          (row) => `<tr>
            <td class="dim mono">${esc(row.time)}</td>
            <td><span class="badge ${row.decided === "sent" ? "on" : ""}">${esc(row.decided)}</span></td>
            <td class="txt">${esc(row.reason || "")}</td>
          </tr>`
        )
        .join("")}
    </tbody></table>`;
}

/* ------------------------------------------------------------------ 人格 */
async function renderPersona() {
  const data = await get("page/persona");
  const settings = await loadSettings();
  state.persona = data;

  const mode = data.mode || "builtin";
  const astr = data.astrbot_personas || { items: [], default: "default" };
  const personas = astr.items || [];
  const selected = data.astrbot_persona_id || "";
  const style = data.style || {};

  const options = [`<option value="">（AstrBot 默认人格）</option>`]
    .concat(
      personas.map(
        (item) =>
          `<option value="${esc(item.id)}" ${item.id === selected ? "selected" : ""}>${esc(item.name)}${
            item.id === astr.default ? "（默认）" : ""
          }</option>`
      )
    )
    .join("");

  view.innerHTML = `
    <div class="mode-picker" id="mode-picker">
      ${modeCard("builtin", "插件自带「绫」", "写在本插件里，可直接编辑", mode)}
      ${modeCard("astrbot", "AstrBot 人格", "用框架里已有的人格", mode)}
      ${modeCard("none", "只用默认", "完全交给 AstrBot 默认人设", mode)}
    </div>

    <div id="persona-body"></div>

    <div class="page-actions">
      <button class="primary" id="p-save">保存人格设置</button>
      <span class="saved" id="p-saved"></span>
    </div>
  `;

  const body = document.getElementById("persona-body");
  let currentMode = mode;
  let currentAstrId = selected;

  function drawBody() {
    if (currentMode === "builtin") {
      body.innerHTML = `
        <div class="cfg-group">
          <div class="cfg-head"><h3>内置人设</h3><p>这段文字会作为「她是谁」注入</p></div>
          <div class="pad">
            <div class="row" style="margin-bottom:14px">
              <div class="field grow" style="margin:0"><label>名字</label>
                <input type="text" id="p-name" value="${esc(data.name)}" /></div>
              <div class="field" style="margin:0;width:120px"><label>单句字数</label>
                <input type="number" id="p-max" value="${esc(style.max_chars || 25)}" /></div>
            </div>
            <div class="field"><label>身份 / 性格</label>
              <textarea id="p-identity" style="min-height:210px">${esc(data.identity)}</textarea>
              <div class="hint">用自然语言写她是谁、怎么说话、在意什么，越像人写的越好</div>
            </div>
            <div class="row" style="margin-bottom:14px">
              <div class="field grow" style="margin:0"><label>口头习惯（顿号分隔）</label>
                <input type="text" id="p-habits" value="${esc(
                  Array.isArray(style.habits) ? style.habits.join("、") : style.habits || ""
                )}" /></div>
              <div class="field grow" style="margin:0"><label>语气</label>
                <input type="text" id="p-tone" value="${esc(style.tone || "")}" /></div>
            </div>
            <div class="field" style="margin:0"><label>禁区（一行一条）</label>
              <textarea id="p-taboos" style="min-height:80px">${esc(
                Array.isArray(data.taboos) ? data.taboos.join("\n") : ""
              )}</textarea></div>
          </div>
        </div>`;
    } else if (currentMode === "astrbot") {
      const found =
        personas.find((item) => item.id === currentAstrId) ||
        personas.find((item) => item.id === astr.default);
      body.innerHTML = `
        <div class="cfg-group">
          <div class="cfg-head"><h3>选一个 AstrBot 人格</h3><p>下面显示的就是它实际会收到的人设文本</p></div>
          <div class="pad">
            <div class="field" style="margin-bottom:14px">
              <label>人格</label>
              <select id="p-astr">${options}</select>
              <div class="hint">要新建或修改人格，去 AstrBot 的「人格」页面，这里只做选择</div>
            </div>
            <div class="field" style="margin:0">
              <label>人设内容（只读）</label>
              <pre class="block prompt-box" id="astr-prompt">${esc(
                (found && found.prompt) || "这个人格没有写 system prompt，将使用 AstrBot 的默认设定"
              )}</pre>
            </div>
          </div>
        </div>`;
      const select = document.getElementById("p-astr");
      select.onchange = () => {
        currentAstrId = select.value;
        const pick = personas.find((item) => item.id === currentAstrId);
        document.getElementById("astr-prompt").textContent =
          (pick && pick.prompt) || "这个人格没有写 system prompt，将使用 AstrBot 的默认设定";
      };
    } else {
      body.innerHTML = `
        <div class="cfg-group">
          <div class="cfg-head"><h3>不用插件人设</h3><p>本插件不注入任何人设文本</p></div>
          <div class="pad">
            <p class="hint" style="margin:0 0 10px">
              它会完全按 AstrBot 的默认人格说话。插件仍然补充「关系远近、当前状态、群聊还是私聊」这三条信息，
              因为那些属于上下文，不属于人设。
            </p>
          </div>
        </div>`;
    }
  }

  drawBody();

  /* 人格相关配置（属于这个板块，就放这里） */
  const personaGroup = pickGroup(settings, "persona");
  if (personaGroup) {
    const extra = document.createElement("div");
    extra.innerHTML = configCard(
      {
        ...personaGroup,
        items: personaGroup.items.filter((item) =>
          ["active_hours", "version_history_limit"].includes(item.key)
        ),
      },
      { title: "人格附加设置", desc: "活跃时段决定它什么时候话多；历史版本用于回滚" }
    );
    body.appendChild(extra.firstElementChild);
    bindConfigCards(body);
  }

  view.querySelectorAll(".mode-card").forEach((card) => {
    card.onclick = () => {
      currentMode = card.dataset.mode;
      view.querySelectorAll(".mode-card").forEach((item) => {
        const on = item.dataset.mode === currentMode;
        item.classList.toggle("is-on", on);
        item.querySelector(".mark").textContent = on ? "当前" : "选择";
      });
      drawBody();
    };
  });

  document.getElementById("p-save").onclick = async () => {
    const msg = document.getElementById("p-saved");
    msg.textContent = "保存中…";
    const payload = {
      mode: currentMode,
      astrbot_persona_id: document.getElementById("p-astr")
        ? document.getElementById("p-astr").value
        : currentAstrId || "",
    };
    if (currentMode === "builtin") {
      Object.assign(payload, {
        name: document.getElementById("p-name").value.trim(),
        identity: document.getElementById("p-identity").value,
        taboos: document
          .getElementById("p-taboos")
          .value.split("\n")
          .map((s) => s.trim())
          .filter(Boolean),
        style: {
          max_chars: Number(document.getElementById("p-max").value) || 25,
          habits: document
            .getElementById("p-habits")
            .value.split(/[、,，\s]+/)
            .map((s) => s.trim())
            .filter(Boolean),
          tone: document.getElementById("p-tone").value.trim(),
        },
      });
    }
    try {
      await post("page/persona/save", payload);
      const extraCard = body.querySelector(".cfg-group:last-child");
      if (extraCard && extraCard.querySelector("[data-path]")) {
        await post("page/settings/save", { changes: collectChanges(extraCard) });
      }
      msg.textContent = "已保存";
      settingsCache = null;
      setTimeout(() => renderPersona(), 500);
    } catch (err) {
      msg.textContent = err.message;
    }
  };
}

function modeCard(id, title, desc, current) {
  return `<button class="mode-card ${id === current ? "is-on" : ""}" data-mode="${id}">
    <span class="mark">${id === current ? "当前" : "选择"}</span>
    <h3>${esc(title)}</h3>
    <p>${esc(desc)}</p>
  </button>`;
}

/* ------------------------------------------------------------------ 行为 */
async function renderBehavior() {
  const settings = await loadSettings();
  view.innerHTML = `
    <div class="settings-intro">这里决定「它怎么说话」和「它看得懂什么」。改完点各卡片自己的保存。</div>
    ${configCard(pickGroup(settings, "humanize"), {
      title: "什么情况下回",
      desc: "阈值越高越少接话；分句条数决定它一次发几条",
    })}
    ${configCard(pickGroup(settings, "guard"), {
      title: "输出风格",
      desc: "发送前的硬性清理：emoji、句末句号、换行、单条字数",
    })}
    ${configCard(pickGroup(settings, "perception"), {
      title: "看得懂什么",
      desc: "合并转发、图片、语音、卡片要不要解析",
    })}
    ${pageActions()}
  `;
  bindConfigCards(view);
  bindPageSave();
}

/* ------------------------------------------------------------------ 模型 */
/* ------------------------------------------------------------------ 记忆 */
const MEM_KIND_CN = {
  fact: "事实",
  pref: "偏好",
  event: "事件",
  promise: "承诺",
  relation: "关系",
  emotion: "情绪",
};

function memKindCN(kind) {
  return MEM_KIND_CN[kind] || kind || "其它";
}

function memImpBar(value) {
  const pct = Math.round(Math.max(0, Math.min(1, Number(value) || 0)) * 100);
  return `<span class="mem-imp" title="重要度 ${(pct / 100).toFixed(2)}"><i style="width:${pct}%"></i></span>`;
}

function memCard(row) {
  const where = row.session_label ? esc(row.session_label) : "未标来源";
  return `<article class="mem-card">
    <div class="mem-card-head">
      <span class="mem-kind k-${esc(row.kind || "fact")}">${esc(memKindCN(row.kind))}</span>
      <span class="mem-where">${where}</span>
      <span class="mem-time mono">${esc(row.created_text || "")}</span>
      ${memImpBar(row.importance)}
      <button class="plain mem-forget" data-forget="${row.id}">忘记</button>
    </div>
    <p class="mem-text">${esc(row.text)}</p>
  </article>`;
}

async function renderMemory(keyword, sessionId, kind) {
  const settings = await loadSettings();
  const params = { limit: 500, status: "active" };
  if (keyword) params.keyword = keyword;
  if (sessionId) params.session = sessionId;
  if (kind) params.kind = kind;
  const data = await get("page/memory/list", params);
  const items = data.items || [];
  const sessions = data.sessions || [];
  const important = sessions.reduce((sum, item) => sum + Number(item.important || 0), 0);

  view.innerHTML = `
    <div class="mem-stats">
      <div class="mem-stat"><div class="num">${esc(data.total || 0)}</div><div class="lbl">记忆总数</div></div>
      <div class="mem-stat"><div class="num">${sessions.length}</div><div class="lbl">来源会话</div></div>
      <div class="mem-stat"><div class="num">${important}</div><div class="lbl">重要记忆</div></div>
      <div class="mem-stat"><div class="num">${esc(data.tier || "—")}</div><div class="lbl">检索档位</div></div>
    </div>

    <div class="mem-toolbar">
      <input class="grow" type="search" id="m-key" placeholder="按关键词过滤，比如「插件」「熬夜」" value="${esc(keyword || "")}" />
      <select id="m-kind">
        ${["", "fact", "pref", "event", "promise", "relation", "emotion"]
          .map((key) => `<option value="${key}" ${kind === key ? "selected" : ""}>${key ? memKindCN(key) : "全部类型"}</option>`)
          .join("")}
      </select>
      <button class="ghost" id="m-go">筛选</button>
    </div>

    <div class="mem-body">
      <aside class="mem-sessions mem-scroll">
        <button class="mem-session ${sessionId ? "" : "is-active"}" data-session="">
          <span class="lbl">全部会话</span>
          <span class="meta"><span class="n">${esc(data.total || 0)}</span></span>
        </button>
        ${sessions
          .map(
            (item) => `<button class="mem-session ${sessionId === item.session_id ? "is-active" : ""}" data-session="${esc(item.session_id)}">
              <span class="lbl">${esc(item.label)}</span>
              <span class="meta"><span class="n">${esc(item.count)}</span>${item.important ? `<span class="dot" title="含 ${esc(item.important)} 条重要记忆"></span>` : ""}</span>
              <span class="stamp mono">${esc(item.last_text || "")}</span>
            </button>`
          )
          .join("")}
      </aside>
      <section class="mem-list mem-scroll">
        ${items.length ? items.map(memCard).join("") : '<div class="empty">这里还没有记忆，聊几句、等两分钟再看</div>'}
      </section>
    </div>

    <details class="adv" id="mem-settings" style="margin-top:22px">
      <summary>记忆设置</summary>
      <div class="pad">
        ${configCard(pickGroup(settings, "memory"), {
          title: "记忆设置",
          desc: "注入条数决定它一次想起多少；向量那项留空就只用关键词检索",
        })}
        ${pageActions()}
      </div>
    </details>

    <details class="adv" id="inbox-details" style="margin-top:14px">
      <summary>归档消息（收到过的原文）</summary>
      <div id="inbox-box" class="pad"><div class="hint">展开后自动加载</div></div>
    </details>
  `;

  bindConfigCards(view);
  bindPageSave();

  const current = () => document.getElementById("m-key").value.trim();
  const currentKind = () => document.getElementById("m-kind").value;
  document.getElementById("m-go").onclick = () => renderMemory(current(), sessionId, currentKind());
  document.getElementById("m-key").onkeydown = (event) => {
    if (event.key === "Enter") renderMemory(current(), sessionId, currentKind());
  };
  document.getElementById("m-kind").onchange = () => renderMemory(current(), sessionId, currentKind());
  view.querySelectorAll("[data-session]").forEach((btn) => {
    btn.onclick = () => renderMemory(current(), btn.dataset.session || "", currentKind());
  });
  view.querySelectorAll("[data-forget]").forEach((btn) => {
    btn.onclick = async () => {
      if (!confirm("让它忘掉这一条？")) return;
      try {
        await post("page/memory/delete", { id: Number(btn.dataset.forget) });
        renderMemory(current(), sessionId, currentKind());
      } catch (err) {
        alert(err.message);
      }
    };
  });

  const details = document.getElementById("inbox-details");
  let loaded = false;
  if (details) details.addEventListener("toggle", async () => {
    if (!details.open || loaded) return;
    loaded = true;
    const box = document.getElementById("inbox-box");
    try {
      const inbox = await get("page/inbox", { limit: 20 });
      box.innerHTML = (inbox.items || []).length
        ? `<table class="list">
            <thead><tr><th>时间</th><th>来源</th><th>内容</th></tr></thead>
            <tbody>${(inbox.items || [])
              .map(
                (row) => `<tr>
                  <td class="dim mono">${esc(row.time)}</td>
                  <td class="dim">${esc(row.sender_name || row.session_id)}</td>
                  <td class="txt">${esc(row.text)}</td>
                </tr>`
              )
              .join("")}</tbody></table>`
        : '<div class="hint">还没有归档消息</div>';
    } catch (err) {
      box.innerHTML = `<div class="hint">${esc(err.message)}</div>`;
    }
  });
}

/* ------------------------------------------------------------------ 工具 */
async function renderTools() {
  const settings = await loadSettings();
  const data = await get("page/tools");
  const items = data.items || [];

  view.innerHTML = `
    ${configCard(pickGroup(settings, "capability"), {
      title: "工具设置",
      desc: "分组决定开放哪些能力；限流是每会话每分钟的次数上限",
    })}
    ${pageActions()}

    <h2 class="sec">已注册能力</h2>
    ${
      items.length
        ? `<table class="list">
            <thead><tr><th>名称</th><th>分组</th><th>说明</th><th>调用</th><th></th></tr></thead>
            <tbody>
              ${items
                .map(
                  (row) => `<tr>
                    <td class="mono">${esc(row.name)}</td>
                    <td class="dim">${esc(row.group)}</td>
                    <td class="txt dim">${esc(firstSentence(row.description, 40))}</td>
                    <td class="dim mono">${esc(row.calls)}/${esc(row.errors)} · ${esc(row.avg_ms)}ms</td>
                    <td><button class="plain" data-test="${esc(row.name)}">试跑</button></td>
                  </tr>`
                )
                .join("")}
            </tbody></table>`
        : '<div class="empty">能力模块未启用</div>'
    }
  `;

  bindConfigCards(view);
  bindPageSave();
  view.querySelectorAll("[data-test]").forEach((btn) => {
    btn.onclick = async () => {
      const raw = prompt(`${btn.dataset.test} 参数（JSON，可留空）`, "{}");
      if (raw == null) return;
      let params = {};
      try {
        params = raw.trim() ? JSON.parse(raw) : {};
      } catch (err) {
        alert("JSON 格式不对");
        return;
      }
      try {
        const result = await post("page/tools/test", { name: btn.dataset.test, params });
        alert(
          `${result.ok ? "成功" : "失败"} · ${result.elapsed}s${result.cached ? "（缓存）" : ""}\n\n${
            result.text || result.error
          }`
        );
      } catch (err) {
        alert(err.message);
      }
    };
  });
}

/* ------------------------------------------------------------------ QQ */
async function renderQQ() {
  const settings = await loadSettings();
  const qzone = await get("page/qzone");
  const auth = qzone.auth || {};
  const modeText = { auto: "自动", builtin: "内置", off: "关闭" };
  const canWrite = !!qzone.write_allowed;

  view.innerHTML = `
    ${configCard(pickGroup(settings, "qq_extra"), {
      title: "QQ 扩展设置",
      desc: "发说说需要先打开写权限；空间访问靠协议端的登录态，不用另外装插件",
    })}
    ${pageActions()}

    <h2 class="sec">空间状态</h2>
    <div class="compat-grid">
      <div class="compat-cell"><div class="k">模式</div><div class="v">${esc(
        modeText[qzone.mode] || qzone.mode
      )}</div></div>
      <div class="compat-cell"><div class="k">登录态</div><div class="v">${
        auth.ok ? `<span class="badge on">已就绪 · ${esc(auth.uin)}</span>` : '<span class="badge warn">未获取</span>'
      }</div></div>
      <div class="compat-cell"><div class="k">来源</div><div class="v dim">${esc(auth.source || "—")}</div></div>
      <div class="compat-cell"><div class="k">获取时间</div><div class="v dim">${esc(
        auth.refreshed_text || "—"
      )}</div></div>
      <div class="compat-cell"><div class="k">写权限</div><div class="v">${
        canWrite ? '<span class="badge on">已开</span>' : '<span class="badge warn">关闭</span>'
      }</div></div>
      <div class="compat-cell"><div class="k">外部空间插件</div><div class="v dim">${
        qzone.external ? "已装（可共存）" : "未装"
      }</div></div>
    </div>
    ${qzone.note ? `<p class="hint" style="margin:10px 0 0">${esc(qzone.note)}</p>` : ""}

    <h2 class="sec">看谁的空间</h2>
    <div class="row" style="margin-bottom:14px">
      <input class="grow" type="text" id="qz-target" placeholder="留空 = 我自己；也可以填 QQ 号" />
      <button class="ghost" id="qz-read">读说说</button>
      <button class="plain" id="qz-auth">重新获取登录态</button>
      <span id="qz-msg" class="hint"></span>
    </div>
    <p class="hint" style="margin:-6px 0 14px">群里让机器人看某人的空间时，它会自己认人（@、QQ 号、群名片）；这里只是手动指定</p>
    <div id="qz-feed"><div class="empty">还没读</div></div>

    <h2 class="sec">发说说</h2>
    <div class="cfg-group">
      <div class="pad">
        <div class="field" style="margin:0 0 12px">
          <textarea id="qz-content" placeholder="写点什么…" style="min-height:110px"></textarea>
          <div class="hint"><span id="qz-count">0</span> / 1000</div>
        </div>
        <div class="row">
          <button class="primary" id="qz-publish" ${canWrite ? "" : "disabled"}>发出去</button>
          <span class="saved" id="qz-pub-msg">${
            canWrite ? "" : "先在上面打开「允许写入空间」并保存"
          }</span>
        </div>
      </div>
    </div>
  `;

  bindConfigCards(view);
  bindPageSave();

  const msg = document.getElementById("qz-msg");
  const feedBox = document.getElementById("qz-feed");

  function renderFeed(items) {
    if (!items.length) {
      feedBox.innerHTML = '<div class="empty">最近没什么动静</div>';
      return;
    }
    feedBox.innerHTML = `<table class="list">
      <thead><tr><th>时间</th><th>谁</th><th>内容</th><th>互动</th></tr></thead>
      <tbody>
        ${items
          .map(
            (item) => `<tr>
              <td class="dim mono">${esc(item.created_text || "—")}</td>
              <td class="dim">${esc(item.nickname || item.uin || "")}</td>
              <td class="txt">${esc(item.content)}</td>
              <td class="dim mono">赞 ${esc(item.like_count || 0)} · 评 ${esc(item.comment_count || 0)}</td>
            </tr>`
          )
          .join("")}
      </tbody></table>`;
  }

  document.getElementById("qz-read").onclick = async () => {
    msg.textContent = "读取中…";
    feedBox.innerHTML = '<div class="empty">读取中…</div>';
    try {
      const target = document.getElementById("qz-target").value.trim();
      const data = await get("page/qzone/feed", target ? { target } : {});
      renderFeed(data.items || []);
      const who = (data.target || {}).label || "";
      msg.textContent = `${who ? who + " · " : ""}读到 ${(data.items || []).length} 条`;
    } catch (err) {
      feedBox.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
      msg.textContent = "";
    }
  };

  document.getElementById("qz-auth").onclick = async () => {
    msg.textContent = "获取中…";
    try {
      await post("page/qzone/auth", {});
      msg.textContent = "登录态已更新";
      setTimeout(() => renderQQ(), 400);
    } catch (err) {
      msg.textContent = err.message;
    }
  };

  const content = document.getElementById("qz-content");
  const counter = document.getElementById("qz-count");
  content.oninput = () => {
    counter.textContent = String(content.value.length);
  };

  const publishBtn = document.getElementById("qz-publish");
  publishBtn.onclick = async () => {
    const pubMsg = document.getElementById("qz-pub-msg");
    const text = content.value.trim();
    if (!text) {
      pubMsg.textContent = "内容是空的";
      return;
    }
    if (!confirm(`确认发这条说说？\n\n${text.slice(0, 120)}`)) return;
    pubMsg.textContent = "发送中…";
    publishBtn.disabled = true;
    try {
      await post("page/qzone/publish", { content: text });
      pubMsg.textContent = "发出去了";
      content.value = "";
      counter.textContent = "0";
    } catch (err) {
      pubMsg.textContent = err.message;
    } finally {
      publishBtn.disabled = !canWrite;
    }
  };
}

/* ------------------------------------------------------------------ 系统 */
async function renderSystem() {
  const settings = await loadSettings();
  const admins = await get("page/admins");
  const compat = settings.compat || { yield: {}, present: {} };
  const present = Object.keys(compat.present || {});

  let mmx = null;
  try {
    const overview = await get("page/overview");
    mmx = overview.mmx || null;
  } catch (err) {
    mmx = null;
  }

  const basicGroup = pickGroup(settings, "basic");
  const basicForCard = basicGroup
    ? {
        ...basicGroup,
        items: basicGroup.items.filter(
          (item) => !["extra_admins", "include_group_role"].includes(item.key)
        ),
      }
    : null;

  const astrBotList = admins.astrbot || [];
  const effective = admins.effective || [];

  view.innerHTML = `
    <div class="settings-intro">这里是插件本身怎么跑：生效范围、谁能管、和别的插件怎么共存、日志。</div>

    <h2 class="sec">MiniMax 多模态工具</h2>
    <div class="model-head">
      <span class="badge ${mmx && mmx.available ? "on" : "warn"}">${mmx && mmx.available ? "可用" : "不可用"}</span>
      <span class="dim">${esc(
        mmx && mmx.available
          ? "语音识别、语音合成、看图缺 provider 时会自动用它"
          : (mmx && mmx.reason) || "没读到状态"
      )}</span>
    </div>

    <h2 class="sec">管理员</h2>
    <div class="cfg-group">
      <div class="cfg-head">
        <h3>谁能用管理功能</h3>
        <p>发说说、看别人的空间、读调试信息</p>
      </div>
      <div class="pad">
        <div class="field" style="margin-bottom:16px">
          <label>来自 AstrBot 全局配置（只读）</label>
          <div class="chips">
            ${
              astrBotList.length
                ? astrBotList.map((id) => `<span class="chip on">${esc(id)}</span>`).join("")
                : '<span class="hint">没有配置，去 WebUI 全局配置的 admins_id 里加</span>'
            }
          </div>
          <div class="hint">这部分跟着 AstrBot 走，避免两套名单打架</div>
        </div>
        <div class="field" style="margin-bottom:16px">
          <label>插件额外管理员（一行一个 QQ 号）</label>
          <textarea id="adm-extra" placeholder="3190568934" style="min-height:70px">${esc(
            (admins.extra || []).join("\n")
          )}</textarea>
          <div class="hint">只补额外的人，AstrBot 名单里的自动算</div>
        </div>
        <label class="switch" style="margin-bottom:4px">
          <input type="checkbox" id="adm-role" ${admins.include_group_role ? "checked" : ""} />
          <span>群主 / 群管也算管理员</span>
        </label>
        <div class="hint">默认不算：群管理员不等于机器人管理员，开了之后他们能发说说</div>
        <div class="row" style="margin-top:16px">
          <button class="primary" id="adm-save">保存管理员设置</button>
          <span class="saved" id="adm-msg"></span>
        </div>
        <div class="hint" style="margin-top:12px">现在生效：${
          effective.length ? esc(effective.join("、")) : "只有群主/群管（如果开了上面那个开关）"
        }</div>
      </div>
    </div>

    ${configCard(basicForCard, {
      title: "生效范围",
      desc: "哪些平台、私聊还是群聊；不加平台名就完全不工作",
    })}

    <h2 class="sec">与其他插件共存</h2>
    <div class="compat-grid">
      ${["humanize", "memory", "rhythm"]
        .map(
          (key) => `<div class="compat-cell"><div class="k">${esc(key)}</div><div class="v">${
            compat.yield && compat.yield[key]
              ? '<span class="badge warn">让位</span>'
              : '<span class="badge on">本插件负责</span>'
          }</div></div>`
        )
        .join("")}
    </div>
    <p class="hint" style="margin:10px 0 0">检测到的外部插件：${esc(present.join("、") || "无")}</p>

    ${configCard(pickGroup(settings, "compat"), {
      title: "让位策略",
      desc: "auto 会检测到别的插件就自动让位，避免双人格和双延时",
    })}

    ${configCard(pickGroup(settings, "debug"), {
      title: "调试",
      desc: "决策日志记录它为什么回、为什么不回",
    })}
    ${pageActions()}
  `;
  bindConfigCards(view);
  bindPageSave();

  document.getElementById("adm-save").onclick = async () => {
    const msg = document.getElementById("adm-msg");
    const extra = document
      .getElementById("adm-extra")
      .value.split(/[\n,，\s]+/)
      .map((item) => item.trim())
      .filter(Boolean);
    const includeRole = document.getElementById("adm-role").checked;
    msg.textContent = "保存中…";
    try {
      await post("page/settings/save", {
        changes: {
          "basic.extra_admins": extra,
          "basic.include_group_role": includeRole,
        },
      });
      settingsCache = null;
      msg.textContent = "已保存";
      setTimeout(() => renderSystem(), 500);
    } catch (err) {
      msg.textContent = err.message;
    }
  };
}

/* ------------------------------------------------------------------ 侧栏 */
function updateCompatLine() {
  if (!state.compat) return;
  const present = Object.keys(state.compat.present || {});
  const yielding = Object.keys(state.compat.yield || {}).filter((k) => state.compat.yield[k]);
  compatLine.textContent = `外部 ${present.length ? present.join(" / ") : "无"}${
    yielding.length ? `\n让位 ${yielding.join(" / ")}` : "\n全部自管"
  }`;
}

document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.onclick = () => setView(btn.dataset.view);
});
document.getElementById("refresh").onclick = () => {
  settingsCache = null;
  render();
};

try {
  initLook();
} catch (err) {
  /* 外观起不来不影响主流程 */
}

(async function boot() {
  try {
    await ensureBridge();
  } catch (err) {
    view.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
    return;
  }
  await render();
})();
