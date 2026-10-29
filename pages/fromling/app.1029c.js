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
/* 只剩夜航手账一套。另外两套（仪表台 / 尺素）已经删掉，
   旧版本存在 localStorage 里的 theme 值在 readLook 里会被拉回 night。 */
const THEMES = [{ id: "night", name: "夜航手账", color: "#9bc4b1" }];

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
    const font = data.font || "sans";
    return { theme: "night", font };
  } catch (err) {
    return { theme: "night", font: "sans" };
  }
}

function applyLook(look) {
  const root = document.documentElement;
  root.dataset.ui = "night";
  if (look.font && look.font !== "sans") root.dataset.font = look.font;
  else root.removeAttribute("data-font");
  /* 仪表台是窄图标栏，导航只留一个字的缩写 */
  const nav = document.getElementById("nav");
  if (nav) {
    nav.querySelectorAll(".nav-item").forEach((btn) => {
      const text = (btn.textContent || "").trim();
      btn.dataset.abbr = text.slice(0, 1);
      btn.title = text;
    });
  }
  try {
    localStorage.setItem(LOOK_KEY, JSON.stringify({ theme: "night", font: look.font || "sans" }));
  } catch (err) {
    /* 存不了就算了 */
  }
}

function paintPickers(look) {
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

  const pickers = [["font-picker", "font", "font-menu"]];

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

/** iframe 沙箱里 window.confirm 会静默返回 false，用户看到的就是「点了没反应」 */
/** 轻提示：iframe 里 alert 会被拦，用它代替 */
function askToast(message, kind) {
  if (document.documentElement.dataset.ui === "night" && typeof njToast === "function") {
    return njToast(message, kind);
  }
  let box = document.getElementById("ask-toast-box");
  if (!box) {
    box = document.createElement("div");
    box.id = "ask-toast-box";
    box.className = "nj-toast-box";
    document.body.appendChild(box);
  }
  const item = document.createElement("div");
  item.className = `nj-toast${kind === "ok" ? " is-ok" : kind === "err" ? " is-err" : ""}`;
  item.textContent = String(message == null ? "" : message);
  box.appendChild(item);
  setTimeout(() => item.classList.add("is-out"), 2600);
  setTimeout(() => item.remove(), 3100);
}

function askConfirm(message, opts) {
  const o = opts || {};
  const isNight = document.documentElement.dataset.ui === "night" && typeof njConfirm === "function";
  if (isNight) return njConfirm(message, o);
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "nj-modal";
    wrap.innerHTML = `<div class="nj-modal-card">
      <p>${esc(message)}</p>
      <div class="nj-modal-ops">
        <button type="button" class="btn ghost" data-no>${esc(o.cancel || "算了")}</button>
        <button type="button" class="btn primary" data-yes>${esc(o.ok || "确定")}</button>
      </div>
    </div>`;
    const close = (value) => {
      wrap.remove();
      resolve(value);
    };
    wrap.querySelector("[data-no]").onclick = () => close(false);
    wrap.querySelector("[data-yes]").onclick = () => close(true);
    wrap.onclick = (ev) => {
      if (ev.target === wrap) close(false);
    };
    document.body.appendChild(wrap);
    const yes = wrap.querySelector("[data-yes]");
    if (yes) yes.focus();
  });
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
// 手改不等于冻结：真正决定「还会不会被自动加减」的是 manual_override，
// 而它默认是 false。以前这里无条件写「不再被自动加减」，跟实际行为相反。
function affFrozenHint(data) {
  const manual = Number(data && data.manual) === 1;
  const frozen = !!(data && data.manual_override);
  if (manual && frozen) return "已停用自动加减，分数只按你设的走";
  if (manual) return "已手动设置，仍会参与每日自动结算";
  return "还没手改过";
}

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
    ? `还剩 ${esc(s.remaining_text || "—")}，预计 ${esc((s.wake_day_label || "") + (s.wake_label || s.wake_window || ""))} 醒`
    : waking
      ? `正在翻没收的消息（${esc(s.pending_crawl || 0)} 个会话）`
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
      ${infoRow("夜间起床点", `${s.wake_time || "08:00"} ± ${s.wake_jitter || 0} 分钟`, "只影响夜里那一觉")}
      ${infoRow("午睡时长", `${s.noon_nap_minutes || 60} ± ${s.noon_nap_jitter || 0} 分钟`, "午睡按这个算，不跟夜间起床点")}
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
    ${nightCard("起床", "夜里睡醒了在固定点上下浮动，浮动上限 90 分钟", sliceItems(night, ["wake_time", "wake_jitter_minutes"]))}
    ${nightCard("午睡", "午睡按固定时长算，不跟夜里那个起床点走", sliceItems(night, ["noon_nap_minutes", "noon_nap_jitter_minutes"]))}
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
async function renderDiary() {
  if (isNightUI() && typeof njDiaryPage === "function") return njDiaryPage();
  const data = await get("diary/list", { limit: 60 }).catch(() => ({ items: [], enabled: false }));
  const rows = data.items || [];
  const cards = rows.length
    ? rows
        .map(
          (row) => `<details class="adv">
            <summary>${esc(row.day_label || row.day_key || "")} · ${esc(row.mood || "安静")}${
              (row.refs || []).length ? ` · 引了 ${(row.refs || []).length} 条记忆` : ""
            }${row.pinned ? " · 置顶" : ""}</summary>
            <div style="padding:14px 18px 16px">
              ${row.title ? `<h3 style="margin:0 0 6px">${esc(row.title)}</h3>` : ""}
              <p style="white-space:pre-wrap;line-height:1.85;margin:0">${esc(row.text || "")}</p>
              <div class="page-actions">
                <button class="ghost" data-diary-pin="${esc(row.id)}" data-pinned="${row.pinned ? 1 : 0}">${row.pinned ? "取消置顶" : "置顶"}</button>
                <button class="ghost" data-diary-del="${esc(row.id)}">删掉</button>
              </div>
            </div>
          </details>`
        )
        .join("")
    : `<div class="empty">她还没开始写${data.enabled ? "" : "（日记开关是关的）"}</div>`;

  view.innerHTML = `
    <div class="settings-intro">她自己的日记：晚上把这一天记下来，只写给自己看。素材只来自真实记忆、梦境和作息，写不出来就不写。</div>
    ${pageActions("现在就写一篇")}
    ${cards}
  `;
  const btn = document.getElementById("page-save");
  if (btn) {
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = "写着…";
      try {
        await post("diary/generate", {});
        renderDiary();
      } catch (err) {
        askToast(err.message || "这次没写成", "err");
        btn.disabled = false;
        btn.textContent = "现在就写一篇";
      }
    };
  }
  view.querySelectorAll("[data-diary-pin]").forEach((el) => {
    el.onclick = async () => {
      try {
        await post("diary/pin", { id: Number(el.dataset.diaryPin), pinned: el.dataset.pinned !== "1" });
        renderDiary();
      } catch (err) {
        askToast(err.message || "置顶失败", "err");
      }
    };
  });
  view.querySelectorAll("[data-diary-del]").forEach((el) => {
    el.onclick = async () => {
      if (!(await askConfirm("删掉这篇日记？删了就找不回来了"))) return;
      try {
        await post("diary/delete", { id: Number(el.dataset.diaryDel) });
        renderDiary();
      } catch (err) {
        askToast(err.message || "删除失败", "err");
      }
    };
  });
}

async function renderProactive() {
  if (isNightUI() && typeof njProactive === "function") return njProactive();
  const [stateData, sessionsData, logData] = await Promise.all([
    get("proactive/state").catch(() => ({})),
    get("proactive/sessions").catch(() => ({ items: [] })),
    get("proactive/log", { limit: 20 }).catch(() => ({ items: [] })),
  ]);
  const items = sessionsData.items || [];
  const logs = logData.items || [];
  view.innerHTML = `
    <div class="settings-intro">
      没人说话的时候她自己会想找你。睡着不发、免打扰时段不发、连着没被回会退避。
      今天已发 ${esc(stateData.sent_today || 0)}/${esc(stateData.daily_cap || 6)} 条。
    </div>
    ${items.length
      ? `<table class="table"><thead><tr><th>会话</th><th>空闲</th><th>未回复</th><th></th></tr></thead><tbody>
        ${items.map((item) => `<tr>
          <td>${esc(item.label || item.session_id)}${item.session_kind === "group" ? "（群）" : ""}</td>
          <td>${esc(item.idle_text || "")}</td>
          <td>${esc(item.unanswered || 0)}</td>
          <td><button class="ghost" data-px-send="${esc(item.session_id)}">发一条</button></td>
        </tr>`).join("")}
      </tbody></table>`
      : `<div class="empty">最近没人说话，她也没处可找</div>`}
    ${logs.length
      ? `<div class="settings-intro" style="margin-top:22px">最近主动</div>
        <table class="table"><tbody>
          ${logs.map((row) => `<tr>
            <td>${esc(row.at_text || "")}</td>
            <td>${esc(row.text || "")}</td>
            <td>${row.replied ? "回了" : "没回"}</td>
          </tr>`).join("")}
        </tbody></table>`
      : ""}
  `;
  view.querySelectorAll("[data-px-send]").forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await post("proactive/now", { session_id: btn.dataset.pxSend });
        renderProactive();
      } catch (err) {
        askToast(err.message || "没发出去", "err");
        btn.disabled = false;
      }
    };
  });
}

async function renderNight() {
  if (isNightUI() && typeof njNight === "function") return njNight();
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
    needState ? get("night/state").catch(() => ({})) : Promise.resolve({}),
    needSettings ? loadSettings() : Promise.resolve(null),
    needDreams ? get("/night/dreams", { limit: 40 }).catch((err) => ({ __error: String((err && err.message) || err) })) : Promise.resolve(null),
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
        askToast(err.message || "唤醒失败", "err");
        wakeBtn.disabled = false;
      }
    };
  }

  const sleepBtn = document.getElementById("night-sleep");
  if (sleepBtn) {
    sleepBtn.onclick = async () => {
      sleepBtn.disabled = true;
      try {
        // 不传 kind：让后端按当前时段判断是午睡还是夜里睡，
        // 写死 night 的话下午点一下会一路睡到第二天早上
        await post("/night/sleep", {});
        await renderNight();
      } catch (err) {
        askToast(err.message || "入睡失败", "err");
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
        askToast(err.message || "操作失败", "err");
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
        askToast(err.message || "操作失败", "err");
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
        askToast(err.message || "删除失败", "err");
        btn.disabled = false;
      }
    };
  });
}

/* ------------------------------------------------------------------ 路由 */
const VIEWS = {
  overview: { title: "概览", render: renderOverview },
  changelog: { title: "更新日志", render: renderChangelog },
  persona: { title: "人格", render: renderPersona },
  behavior: { title: "行为", render: renderBehavior },
  memory: { title: "记忆", render: renderMemory },
  night: { title: "长夜", render: renderNight },
  diary: { title: "日记", render: renderDiary },
  tools: { title: "工具", render: renderTools },
  qq: { title: "QQ", render: renderQQ },
  system: { title: "系统", render: renderSystem },
};

let currentView = "overview";
let currentSub = "";
let state = { persona: null, compat: null, health: null };
let settingsCache = null;

/* 二级栏目：一个一级视图下面挂几个子页。
   没在这里登记的视图不显示子页栏，保持原来的长页。 */
const SUBTABS = {
  changelog: [
    { key: "fixes", title: "问题与修复", render: () => renderChangelog("fixes") },
    { key: "news", title: "新功能", render: () => renderChangelog("news") },
  ],
  memory: [
    { key: "summary", title: "记忆总览", render: renderM3Overview },
    /* 0.7.0：「按用户分」取消，认识的人合并进好感度页 */
    { key: "sessions", title: "按会话分", render: renderM3Sessions },
    { key: "library", title: "记忆库", render: () => renderMemory() },
    { key: "levels", title: "等级与遗忘", render: renderMemoryLevels },
    { key: "affinity", title: "好感度", render: renderAffinityPage },
    { key: "merge", title: "主体合并", render: renderSubjectMerge },
  ],
  behavior: [
    { key: "express", title: "表达", render: () => renderBehavior() },
    { key: "proactive", title: "主动找人", render: renderProactive },
  ],
  qq: [
    { key: "space", title: "空间", render: () => renderQQ() },
    { key: "avatar", title: "头像", render: renderAvatarPage },
  ],
};

const subtabsEl = document.getElementById("subtabs");

function subsOf(name) {
  return SUBTABS[name] || [];
}

function paintSubtabs() {
  if (!subtabsEl) return;
  const subs = subsOf(currentView);
  if (!subs.length) {
    subtabsEl.hidden = true;
    subtabsEl.innerHTML = "";
    return;
  }
  subtabsEl.hidden = false;
  subtabsEl.innerHTML = subs
    .map(
      (sub) =>
        `<button class="subtab${sub.key === currentSub ? " is-active" : ""}" data-sub="${esc(sub.key)}">${esc(
          sub.title
        )}</button>`
    )
    .join("");
  subtabsEl.querySelectorAll("[data-sub]").forEach((btn) => {
    btn.onclick = () => setSub(btn.dataset.sub);
  });
}

function setSub(key) {
  const subs = subsOf(currentView);
  const hit = subs.find((sub) => sub.key === key);
  if (!hit) return;
  currentSub = hit.key;
  applyHash(`#${currentView}/${currentSub}`);
  paintSubtabs();
  render();
}

function setView(name, sub) {
  if (!VIEWS[name]) return;
  settingsCache = null;
  currentView = name;
  const subs = subsOf(name);
  if (subs.length) {
    const hit = sub ? subs.find((item) => item.key === sub) : null;
    currentSub = (hit || subs[0]).key;
  } else {
    currentSub = "";
  }
  titleEl.textContent = VIEWS[name].title;
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.view === name);
  });
  const target = currentSub ? `#${name}/${currentSub}` : `#${name}`;
  applyHash(target);
  paintSubtabs();
  render();
  view.scrollIntoView({ block: "start" });
}

/* 页面可能在非浏览器环境被加载（渲染冒烟测试），这两处都要兜住 */
function readHash() {
  try {
    return String(window.location.hash || "");
  } catch (err) {
    return "";
  }
}

function applyHash(target) {
  try {
    if (!target) return;
    if (window.location.hash === target) return;
    history.replaceState(null, "", target);
  } catch (err) {
    /* 没有 history 就只改内部状态 */
  }
}

function parseHash() {
  const raw = readHash().replace(/^#/, "");
  if (!raw) return { view: "overview", sub: "" };
  const [name, sub] = raw.split("/");
  return { view: VIEWS[name] ? name : "overview", sub: sub || "" };
}

async function render() {
  const subs = subsOf(currentView);
  const active = subs.length ? subs.find((sub) => sub.key === currentSub) || subs[0] : null;
  if (active) currentSub = active.key;
  view.innerHTML = '<div class="empty">读取中…</div>';
  try {
    if (active) await active.render();
    else await VIEWS[currentView].render();
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
  memory: ["enabled", "inject_top_k", "forgetting.mode", "levels.enabled", "affinity.enabled", "user_profile.enabled"],
  capability: ["enabled", "groups", "rate_limit_per_min"],
  qq_extra: ["qzone_mode", "qzone_auto_post", "qzone_daily_max", "avatar_auto_change", "poke_enabled"],
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
  // 核心项：提示收进取问号气泡，省地方；高级项：直接把 hint 摊开写清楚
  if (compact && item.hint) {
    return `<div class="cfg-row">
      <div class="cfg-label">${esc(item.label)}<span class="tip" tabindex="0" role="button" aria-label="说明" data-tip="${esc(
      item.hint
    )}">?</span></div>
      <div>${controlHtml(item)}</div>
    </div>`;
  }
  const hint = item.hint ? `<span class="hint">${esc(item.hint)}</span>` : "";
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
      else if (currentView === "diary") {
        if (isNightUI() && typeof njDiaryPage === "function") await njDiaryPage();
        else await renderDiary();
      }
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
/* ------------------------------------------------------------------ 更新日志
   0.7.3 → 1.0.0 的大更新。三块：出了什么问题、怎么修的、新功能在哪找。 */
function changelogHtml(key) {
  const sec = (title, inner) => `<h4 style="margin:16px 0 8px;font-size:14px;color:var(--accent,#c8a86a)">${title}</h4>${inner}`;
  const item = (head, body) => `<li><b>${head}</b><span>${body}</span></li>`;
  const ul = (inner) => `<ol class="cl-list">${inner}</ol>`;

  const merged = ul([
    item("发现并修复「唤醒后无响应」的问题",
      "唤醒时仅更新状态标志，未记录起始时间与待补会话，状态机无法完成转移。修复后统一出口写入状态、起始时间、待补会话三项，并增加 5 分钟超时兜底。"),
    item("发现并修复「睡眠与唤醒状态未区分」的问题",
      "判定逻辑将 waking 视同 asleep，唤醒瞬间的消息仍被拦截。修复后 is_asleep() 仅匹配 asleep，waking 由独立方法判定。"),
    item("发现并修复「补看消息从未执行」的问题",
      "缺少常量导入，函数入口即抛异常并被兜底吞掉。修复后补全导入，该流程首次可正常执行。"),
    item("发现并修复「作息依赖消息驱动」的问题",
      "无独立心跳，无消息时状态机停止推进。修复后启动流程注册节拍任务，周期 60 秒，tick 按 last_tick 补算。"),
    item("发现并修复「主动调度周期错误」的问题",
      "调度挂载于小时级任务，实际周期为设计值的 60 倍。修复后迁至心跳循环，恢复分钟级。"),
    item("发现并修复「精力补算偏移」的问题",
      "以终点时刻的阶段系数乘整段时长，深睡区间被低估。修复后按阶段边界切分、逐段以中点系数累加，同一场景 594 修正为 724.8。"),
    item("发现并修复「并发会话记忆串扰」的问题",
      "注入查询读取全局「最后一条消息」，多会话并发时取到其他会话内容。修复后将消息挂载至 event，各模块从 event 读取。"),
    item("发现并修复「跨会话召回缺少受众边界」的问题",
      "检索条件仅限定 persona，未限定 subject，候选池覆盖全库。修复后拆分为普通用户四档与管理员三档，默认收在最保守一档。"),
    item("发现并修复「人格键名不一致」的问题",
      "部分查询读取 persona.id 而非 persona_id。修复后统一为 persona_id。"),
    item("发现并修复「主体识别错误」的问题",
      "整批草稿沿用最后发言人，模型输出称谓被当作主键，同一人被拆成多条并导致好感度重复结算。修复后按证据反查真实发送者、优先使用 subject_key 入库，并提供历史数据合并工具。"),
    item("发现并修复「等级 0 被降级」的问题",
      "int(x or 3) 将 0 转为 3，不可遗忘标记失效。修复后仅 None 取默认值，0 原样保留。"),
    item("发现并修复「否定语义未参与去重」的问题",
      "相似度基于字符重合，肯定与否定被判 94% 相同并合并。修复后否定、数字、时间差异均判为不相似。"),
    item("发现并修复「抽取水位超前」的问题",
      "输入按 6000 字符截断，水位却推进至整批末尾，尾部不再抽取。修复后仅推进至实际送入模型的行，解析成功需完整结构。"),
    item("发现并修复「语音转写始终失败」的问题",
      "调用不存在的 hub.mode()，异常被吞，STT 服务同时未启用。修复后改为 hub.route() 并启用 provider。"),
    item("发现并修复「画像读取异常」的问题",
      "profiles.get() 调用未定义的 _loads()，存在画像数据即抛异常。修复后补全该方法。"),
    item("发现并修复「道别提示被屏蔽」的问题",
      "prompt_hint() 在 asleep 分支提前返回，待睡状态同样命中。修复后条件改为「asleep 且无待睡意图」。"),
    item("发现并修复「归档数据损坏」的问题",
      "空 msg_id 以 IS '' 去重导致首条之后全部丢弃，blocks_json 硬截断产生非法结构。修复后空 id 生成合成键，超长内容从尾部整块丢弃。"),
    item("发现并修复「输出清洗误删正文」的问题",
      "emoji 正则包含箭头区 U+2190–U+21FF。修复后该区间移出正则，正文箭头不再被移除。"),
    item("确认「流式输出跳过分句」为设计行为",
      "流式边生成边发，分句会破坏体验，故代码中明确跳过。未作修改，与分句功能互斥。"),
    item("发现并修复「交互元素不可见」的问题",
      "加减按钮默认 opacity: 0，依赖 hover 触发，移动端无悬停行为。修复后改为常显，尺寸 18→32（触屏 40）。"),
  ]);

  const FEATURES = [
    {
      name: "主体合并",
      path: "记忆 → 主体合并",
      intro: "把同一个人被拆出来的多个称呼合并成一条。",
      how: "点「扫描重复主体」，系统按已确认的别名匹配出候选，每行显示「旧称呼 → 目标 → 匹配依据 → 关联记忆数」。勾选后点「合并选中」，需要再点一次确认。",
      note: "合并会同时迁移记忆、合并好感度、把旧称呼写进别名。操作不可逆，但有撤销通道。",
    },
    {
      name: "合并撤销",
      path: "合并成功后原地出现",
      intro: "每次合并生成一个批次号，可整批退回。",
      how: "合并完成后原按钮位置会出现「撤销这次合并」，旁边带批次号。点击即回滚该批次涉及的全部记忆与主体。",
      note: "若合并之后目标主体上已产生新记忆，撤销会被拒绝并给出原因 —— 避免把新数据一起带回旧主体。",
    },
    {
      name: "跨会话检索分档",
      path: "记忆 → 记忆库 → 记忆设置",
      intro: "控制「除了当前会话，还能去哪些会话里找记忆」。",
      how: "在「记忆库」页拉到底，展开「记忆设置」，共四项：允许跨会话检索（总开关）、普通用户档、管理员档、白名单。三档含义：off 只查当前会话；same_user / self 只查这个人自己说过的话；all 查全库。",
      note: "默认收在最保守一档：管理员也看不到别人的私聊。选 all 时面板会提示模型可能复述他人私密内容。",
    },
    {
      name: "诊断轨迹",
      path: "GET /diagnostics",
      intro: "统一记录各阶段的执行结果，用于回答「为什么这个功能没生效」。",
      how: "环形缓冲保留最近 60 条，每条记录阶段名与若干关键字段（会话、模式、命中与否）。只记结构不记正文，异常只记类型。",
      note: "排查顺序建议：先看轨迹确认走的是哪个分支，再对照问题列表定位。",
    },
    {
      name: "好感度手动编辑",
      path: "记忆 → 好感度",
      intro: "排行榜每一行都带加减按钮，可直接调整分数。",
      how: "按钮常显，尺寸较上一版放大一倍（触屏再放大）。点击后立即生效，行内数字同步更新。",
      note: "手动修改不等于冻结：默认仍参与每日结算，只有开启 manual_override 才会停用自动加减。行内文案会写明当前处于哪种状态。",
    },
    {
      name: "日维护幂等",
      path: "自动生效",
      intro: "同一天内重复触发的维护只执行一次。",
      how: "以「人格 + 业务日」作为标记，维护成功后才写入。插件重启、定时器抖动、多实例并存都不会导致重复衰减。",
      note: "无需配置。中途异常不会写入标记，下次仍会重试。",
    },
    {
      name: "全局提示层",
      path: "全站生效",
      intro: "所有说明气泡改为固定定位，不再被容器裁切。",
      how: "鼠标悬停或键盘聚焦时显示，位置自动避让视口边缘；上方空间不足时翻到下方。",
      note: "此前采用 CSS 伪元素实现，在带 overflow: hidden 的折叠面板内会被裁掉，靠近屏幕边缘时也会溢出。",
    },
  ];

  const news = `<ul class="cl-paths">${FEATURES.map(
    (f) => `<li>
      <b>${f.name}</b><code>${f.path}</code>
      <span style="display:block;margin-top:6px">${f.intro}</span>
      <span style="display:block;margin-top:5px">${f.how}</span>
      <span style="display:block;margin-top:5px;font-size:calc(12.5px * var(--cl-fs, 1));opacity:.58">${f.note}</span>
    </li>`
  ).join("")}</ul>`;

  // 按块返回：侧边栏「更新日志」下面挂三个二级页
  if (key === "fixes") {
    return `
      <div class="cl-eyebrow">APOLOGY · 0.7.3</div>
      <div class="cl-appeal">
        <h2>致歉声明</h2>
        <p>0.7.3 版本在发布时并不成熟。</p>
        <p>该版本功能数量较多，且各模块之间耦合程度较高。记忆、夜间作息、人格注入、主动调度、
           消息归档与输出处理共享同一套运行状态、同一批全局变量、同一个时间基准，
           彼此之间缺乏明确的边界与责任划分。</p>
        <p>这种结构在单模块、单会话的理想条件下尚可维持；一旦进入并发场景 ——
           多会话同时活跃、插件重载、状态机跨时段推进、事务中途失败 ——
           即产生大量并发问题：状态机停滞在中间态、记忆归属错乱、
           消息被错误拦截、进度与数据不同步、部分功能静默失效。</p>
        <p>上述问题直接影响了实际使用体验。我们也注意到，不少持续关注本插件的用户
           在使用过程中反复遇到这些问题，并花费时间整理现象、提交反馈。
           这些反馈是本次重构的起点。</p>
        <p>对此，我们向全体用户致以诚挚歉意。1.0.0 版本针对上述结构性问题进行了重构，
           逐条复现、定位并修复，具体内容见下页。</p>
        <p class="sig">插件维护者 · 2026 年 10 月</p>
      </div>
      <div class="cl-eyebrow">WHAT WENT WRONG</div>
      <p class="cl-lead">经过各位用户耐心反馈，和我熬夜通宵奋战，发现在实际体验中，插件功能会出现如下问题：</p>
      ${merged}`;
  }
  if (key === "news") {
    return `
      <div class="cl-eyebrow">WHERE TO FIND</div>
      <p class="cl-lead">这一版新加的东西不多，但都在面板上有明确入口。下面每条都标了路径。</p>
      ${news}`;
  }
  return `<details class="adv" id="changelog" style="margin:20px 0 8px">
      <summary style="font-size:15px">更新日志 · 0.7.3 → 1.0.0（大更新）</summary>
      <div class="pad" style="font-size:13px">
        <p class="hint" style="margin:0 0 6px">这一版没有加多少新功能，主要是把 0.7.3 里那些「看着正常、实际不工作」的地方挖出来修掉。</p>
        ${sec("一、问题与修复", merged)}
        ${sec("二、新功能", news)}
      </div>
    </details>`;
}

async function renderChangelog() {
  const key = currentSub || "fixes";
  let scale = 1;
  try {
    const saved = Number(localStorage.getItem("cg-changelog-fs"));
    if (saved >= 0.8 && saved <= 1.6) scale = saved;
  } catch (_) {}
  view.style.setProperty("--cl-fs", String(scale));
  view.innerHTML = `
    <div class="cl-zoom">
      <button type="button" data-cl-fs="-1" title="调小">A−</button>
      <output>${Math.round(scale * 100)}%</output>
      <button type="button" data-cl-fs="1" title="调大">A+</button>
    </div>` + changelogHtml(key);

  view.querySelectorAll("[data-cl-fs]").forEach((btn) => {
    btn.onclick = () => {
      const step = Number(btn.dataset.clFs) > 0 ? 0.08 : -0.08;
      const next = Math.max(0.8, Math.min(1.6, Math.round((scale + step) * 100) / 100));
      scale = next;
      view.style.setProperty("--cl-fs", String(next));
      const out = view.querySelector(".cl-zoom output");
      if (out) out.textContent = `${Math.round(next * 100)}%`;
      try {
        localStorage.setItem("cg-changelog-fs", String(next));
      } catch (_) {}
    };
  });
}

async function renderOverview() {
  if (isNightUI() && typeof njOverview === "function") return njOverview();
  const data = await get("page/overview");
  state.health = data.health;
  state.compat = data.health.compat;
  const modules = data.health.modules || {};
  const stats = data.stats || {};
  personaTag.textContent = `${data.health.persona.name} · ${data.health.persona.id}`;

  view.innerHTML = `
    <div class="group-banner">
      <div class="group-banner-main">
        <span class="group-banner-label">官方交流群</span>
        <span class="group-banner-num">577084969 / 951213204</span>
        <span class="group-banner-desc">用着有问题、想提需求、想看更新，都来群里说</span>
      </div>
      <button class="group-banner-btn" id="group-copy" type="button">复制群号</button>
    </div>

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
  if (isNightUI() && typeof njPersona === "function") return njPersona();
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
  if (isNightUI() && typeof njBehavior === "function") return njBehavior();
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
      ${levelTag(row.mem_level)}
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
  if (isNightUI() && typeof njMemory === "function") return njMemory(keyword, sessionId, kind);
  const settings = await loadSettings();
  // 一次拉 500 条会让「记忆库」铺 500 张卡，滑动必卡。
  // 先取 150，底部给「再看一些」按需追加。
  const shown = Number(window.__memShown || 150);
  const params = { limit: shown, status: "active" };
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
        ${
          items.length >= shown && Number(data.total || 0) > items.length
            ? `<div style="padding:14px 0;text-align:center"><button class="ghost" id="mem-more">再看一些（已显示 ${items.length} / ${esc(data.total || 0)}）</button></div>`
            : ""
        }
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
  document.getElementById("m-go").onclick = () => {
    window.__memShown = 150;
    renderMemory(current(), sessionId, currentKind());
  };
  const moreBtn = document.getElementById("mem-more");
  if (moreBtn) {
    moreBtn.onclick = () => {
      window.__memShown = Number(window.__memShown || 150) + 150;
      renderMemory(current(), sessionId, currentKind());
    };
  }
  document.getElementById("m-key").onkeydown = (event) => {
    if (event.key === "Enter") renderMemory(current(), sessionId, currentKind());
  };
  document.getElementById("m-kind").onchange = () => renderMemory(current(), sessionId, currentKind());
  view.querySelectorAll("[data-session]").forEach((btn) => {
    btn.onclick = () => renderMemory(current(), btn.dataset.session || "", currentKind());
  });
  view.querySelectorAll("[data-forget]").forEach((btn) => {
    btn.onclick = async () => {
      if (!(await askConfirm("让它忘掉这一条？"))) return;
      try {
        await post("page/memory/delete", { id: Number(btn.dataset.forget) });
        renderMemory(current(), sessionId, currentKind());
      } catch (err) {
        askToast(err.message, "err");
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
  if (isNightUI() && typeof njTools === "function") return njTools();
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
        askToast("JSON 格式不对", "err");
        return;
      }
      try {
        const result = await post("page/tools/test", { name: btn.dataset.test, params });
        askToast(
          `${result.ok ? "成功" : "失败"} · ${result.elapsed}s${result.cached ? "（缓存）" : ""} · ${
            result.text || result.error
          }`,
          result.ok ? "ok" : "err"
        );
      } catch (err) {
        askToast(err.message, "err");
      }
    };
  });
}

/* ------------------------------------------------------------------ QQ */
async function renderQQ() {
  if (isNightUI() && typeof njQQ === "function") return njQQ();
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

    <h2 class="sec">三项能力，各管各的</h2>
    <div class="compat-grid">
      <div class="compat-cell"><div class="k">头像 / 昵称</div><div class="v"><span class="badge on">可用</span></div></div>
      <div class="compat-cell"><div class="k">靠什么</div><div class="v dim">协议端的 get_stranger_info，不吃 cookie</div></div>
      <div class="compat-cell"><div class="k">签名 / 状态</div><div class="v">${
        ((settings.qq_extra || {}).avatar_auto_change) ? '<span class="badge on">已开</span>' : '<span class="badge warn">关闭</span>'
      }</div></div>
      <div class="compat-cell"><div class="k">靠什么</div><div class="v dim">协议端的 set_self_longnick，也不吃 cookie</div></div>
      <div class="compat-cell"><div class="k">空间说说</div><div class="v">${
        auth.ok ? '<span class="badge on">已就绪</span>' : '<span class="badge warn">需要登录态</span>'
      }</div></div>
      <div class="compat-cell"><div class="k">靠什么</div><div class="v dim">唯一的例外：必须拿到空间 cookie</div></div>
    </div>
    <p class="hint" style="margin:10px 0 0">上面三项互不影响。头像和签名不要 cookie，只有空间说说才需要；登录态丢了不影响前两项。</p>

    <h2 class="sec">空间登录态</h2>
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
    if (!(await askConfirm(`确认发这条说说？\n\n${text.slice(0, 120)}`))) return;
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
  if (isNightUI() && typeof njSystem === "function") return njSystem();
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
          <textarea id="adm-extra" placeholder="123456789" style="min-height:70px">${esc(
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

/* 官方群：点一下把群号复制走 */
const GROUP_NO = "577084969";
const GROUP_NO_2 = "951213204";
/* 复制时两个号一起给，用空格分隔，粘到 QQ 里能直接搜 */
const GROUP_COPY = `${GROUP_NO} ${GROUP_NO_2}`;
const GROUP_SHOW = `${GROUP_NO} / ${GROUP_NO_2}`;

async function copyGroupNo(btn) {
  let ok = false;
  try {
    await navigator.clipboard.writeText(GROUP_COPY);
    ok = true;
  } catch (err) {
    try {
      const ta = document.createElement("textarea");
      ta.value = GROUP_COPY;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand("copy");
      document.body.removeChild(ta);
    } catch (err2) {
      ok = false;
    }
  }
  if (btn) {
    const old = btn.textContent;
    btn.textContent = ok ? "已复制" : GROUP_SHOW;
    setTimeout(() => { btn.textContent = old; }, 2000);
  }
  return ok;
}
function bindGroupCard() {
  const card = document.getElementById("group-card");
  if (!card) return;
  card.addEventListener("click", async () => {
    const hint = card.querySelector(".group-card-hint");
    let ok = false;
    try {
      await navigator.clipboard.writeText(GROUP_COPY);
      ok = true;
    } catch (err) {
      // 没权限（比如不在 https / 被 iframe 策略拦了）就退到老办法
      try {
        const ta = document.createElement("textarea");
        ta.value = GROUP_COPY;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        ok = document.execCommand("copy");
        document.body.removeChild(ta);
      } catch (err2) {
        ok = false;
      }
    }
    if (hint) {
      hint.textContent = ok ? `已复制 ${GROUP_COPY}` : `群号 ${GROUP_SHOW}`;
    }
    card.classList.toggle("is-copied", ok);
    setTimeout(() => {
      if (hint) hint.textContent = "有问题来群里喊";
      card.classList.remove("is-copied");
    }, 2200);
  });
}

view.addEventListener("click", (ev) => {
  const btn = ev.target.closest && ev.target.closest("#group-copy");
  if (btn) copyGroupNo(btn);
});


/* ------------------------------------------------------------------ 记忆 2.0 */
const LEVEL_NAMES = ["不可忘", "随口", "细节", "平常", "在意", "关键"];
let subjectKey = "";

function levelTag(level) {
  const n = Math.max(0, Math.min(5, Number(level) || 0));
  const name = LEVEL_NAMES[n] || "";
  if (n === 0) return `<span class="lv lv0" title="0 级 · ${esc(name)}"><i></i></span>`;
  const dots = Array.from({ length: 5 }, (_, i) => `<i class="${i < n ? "on" : ""}"></i>`).join("");
  return `<span class="lv lv${n}" title="${n} 级 · ${esc(name)}">${dots}</span>`;
}

function stageChip(stage, name) {
  const n = Math.max(0, Math.min(5, Number(stage) || 0));
  return `<span class="stage stage-${n}">${esc(name || "")}</span>`;
}

async function renderMemorySummary() {
  const [levels, curve, aff] = await Promise.all([
    get("page/memory/levels"),
    get("page/memory/forgetting", { days: 45 }),
    get("page/affinity"),
  ]);
  const items = levels.levels || [];
  const maxCount = Math.max(1, ...items.map((x) => Number(x.count) || 0));
  const bars = items
    .map((x) => {
      const count = Number(x.count) || 0;
      const width = Math.round((count / maxCount) * 100);
      return `<div class="bar-row">
        <span class="bar-key">${levelTag(x.level)}</span>
        <span class="bar"><i style="width:${width}%"></i></span>
        <span class="bar-num mono">${count}</span>
      </div>`;
    })
    .join("");
  const ranking = (aff.ranking || [])
    .slice(0, 6)
    .map(
      (row, index) =>
        `<li><span class="mono muted">${index + 1}</span><span class="grow">${esc(
          row.name || row.subject_key
        )}</span>${stageChip(row.stage, row.stage_name)}<span class="mono">${Math.round(row.score)}</span></li>`
    )
    .join("");
  const locked = items.filter((x) => Number(x.level) === 0).reduce((sum, x) => sum + (Number(x.count) || 0), 0);
  view.innerHTML = `
    <div class="grid-stats">
      <div class="stat"><div class="num">${esc(levels.total || 0)}</div><div class="lbl">活跃记忆</div></div>
      <div class="stat"><div class="num">${esc(locked)}</div><div class="lbl">不可忘</div></div>
      <div class="stat"><div class="num small">${esc(curve.label || "")}</div><div class="lbl">衰减档位</div></div>
      <div class="stat"><div class="num">${esc(curve.due_now || 0)}</div><div class="lbl">待复习</div></div>
    </div>
    <section class="cfg-group">
      <div class="cfg-head"><h3>等级分布</h3><p>等级越高越不容易忘，也越优先被想起来</p></div>
      <div class="pad">${bars || '<p class="hint">还没有记忆</p>'}</div>
    </section>
    <section class="cfg-group">
      <div class="cfg-head"><h3>谁跟我最好</h3><p>按好感度排，具体数值可以在「好感度」里手改</p></div>
      <div class="pad">${ranking ? `<ol class="rank-list">${ranking}</ol>` : '<p class="hint">还没有好感度记录</p>'}</div>
    </section>`;
}

/* ------------------------------------------------ 用户识别库 */
async function renderMemoryPeople() {
  if (subjectKey) return renderSubjectDetail(subjectKey);
  const data = await get("memory/subjects", { limit: 100 });
  const items = data.items || [];
  const rows = items
    .map((item) => {
      const aff = item.affinity || {};
      return `<button class="person" data-key="${esc(item.user_id)}">
        <span class="person-name">${esc(item.display_name || item.user_id)}</span>
        ${stageChip(aff.stage, aff.stage_name)}
        <span class="mono muted">${esc(item.atom_count)} 条</span>
        ${item.has_profile ? '<span class="chip">有画像</span>' : ""}
        <span class="mono muted small">${esc(item.last_text || "")}</span>
      </button>`;
    })
    .join("");
  view.innerHTML = `
    <section class="cfg-group">
      <div class="cfg-head"><h3>用户识别库</h3><p>${items.length} 个人被记住过；昵称会变，QQ 号不会</p></div>
      <div class="pad">
        <div class="row" style="margin-bottom:12px;align-items:center">
          <div class="field grow" style="margin:0">
            <input type="text" id="subj-search" placeholder="搜名字或 QQ 号，回车" />
          </div>
          <label class="switch"><input type="checkbox" id="subj-whitelist" ${
            data.whitelist_enabled ? "checked" : ""
          } /><span>仅白名单</span></label>
          <button class="ghost" id="subj-reload">刷新</button>
        </div>
        <p class="hint" style="margin:0 0 10px">
          「仅白名单」打开后，只有名单里的人会被画像；名单在下面这个面板里加，也可以去设置页改
        </p>
        <div class="person-list">${rows || '<p class="hint">还没识别到人，聊几句就有了</p>'}</div>
      </div>
    </section>
    <section class="cfg-group">
      <div class="cfg-head"><h3>画像名单</h3><p>白名单放行，不再构建名单拦住</p></div>
      <div class="pad">
        <div class="field"><label>白名单（一行一个 QQ 号）</label>
          <textarea id="prof-whitelist" data-type="list" style="min-height:80px">${esc(
            (data.whitelist || []).join("\n")
          )}</textarea></div>
        <div class="field"><label>不再构建名单</label>
          <textarea id="prof-blocklist" data-type="list" style="min-height:80px">${esc(
            (data.blocklist || []).join("\n")
          )}</textarea>
          <div class="hint">删画像时会自动加进来，清空这一栏就恢复</div></div>
        ${pageActions("保存名单")}
      </div>
    </section>`;

  document.querySelectorAll("[data-key]").forEach((btn) => {
    btn.onclick = () => {
      subjectKey = btn.dataset.key;
      render();
    };
  });
  const box = document.getElementById("subj-whitelist");
  if (box)
    box.onchange = async () => {
      await post("page/memory/profile/config", { whitelist_enabled: box.checked });
      settingsCache = null;
    };
  const search = document.getElementById("subj-search");
  if (search)
    search.onkeydown = async (ev) => {
      if (ev.key !== "Enter") return;
      const keyword = search.value.trim();
      const found = await get("memory/subjects", { keyword, limit: 100 });
      const list = document.querySelector(".person-list");
      if (list) {
        list.innerHTML = (found.items || [])
          .map(
            (item) =>
              `<button class="person" data-key="${esc(item.user_id)}"><span class="person-name">${esc(
                item.display_name || item.user_id
              )}</span><span class="mono muted">${esc(item.atom_count)} 条</span></button>`
          )
          .join("") || '<p class="hint">没找到</p>';
        list.querySelectorAll("[data-key]").forEach((btn) => {
          btn.onclick = () => {
            subjectKey = btn.dataset.key;
            render();
          };
        });
      }
    };
  const save = document.getElementById("page-save");
  if (save)
    save.onclick = async () => {
      const msg = document.getElementById("page-msg");
      const white = (document.getElementById("prof-whitelist") || {}).value || "";
      const block = (document.getElementById("prof-blocklist") || {}).value || "";
      const toList = (text) => text.split(/\s+/).map((x) => x.trim()).filter(Boolean);
      await post("page/memory/profile/config", {
        whitelist: toList(white),
        blocklist: toList(block),
      });
      settingsCache = null;
      if (msg) msg.textContent = "已保存";
    };
}

async function renderSubjectDetail(key) {
  const data = await get("page/memory/subject", { key });
  const aff = data.affinity || {};
  const profile = data.profile || {};
  const atoms = (data.atoms || []).slice(0, 80);
  const rows = atoms
    .map(
      (row) => `<div class="atom-row">
        ${levelTag(row.mem_level)}
        <span class="atom-text">${esc(firstSentence(row.text, 70))}</span>
        <select class="lv-pick" data-id="${row.id}" title="改等级">
          ${[0, 1, 2, 3, 4, 5]
            .map(
              (n) =>
                `<option value="${n}" ${Number(row.mem_level) === n ? "selected" : ""}>${LEVEL_NAMES[n]}</option>`
            )
            .join("")}
        </select>
      </div>`
    )
    .join("");
  const fields = profile.fields || {};
  const fieldRows = Object.entries(fields).filter(([, v]) => v).length
    ? Object.entries(fields)
        .filter(([, v]) => v)
        .map(([name, value]) => `<div class="kv"><span>${esc(name)}</span><p>${esc(value)}</p></div>`)
        .join("")
    : '<p class="hint">还没有画像</p>';
  const words = (profile.keywords || []).map((w) => `<span class="chip">${esc(w)}</span>`).join("");
  view.innerHTML = `
    <div class="page-actions">
      <button class="ghost" id="subj-back">返回列表</button>
      <span class="saved">${esc(data.name || key)}　好感度 ${Math.round(Number(aff.score) || 0)}</span>
    </div>
    <section class="cfg-group">
      <div class="cfg-head"><h3>关系</h3><p>${esc((data.stage_names || [])[aff.stage] || "")}</p></div>
      <div class="pad">
        <div class="row" style="align-items:center">
          <input type="range" id="aff-range" min="0" max="1000" step="10" value="${Math.round(
            Number(aff.score) || 0
          )}" style="flex:1" />
          <span class="mono" id="aff-num">${Math.round(Number(aff.score) || 0)}</span>
          <button class="primary" id="aff-save">保存分数</button>
        </div>
        <p class="hint" style="margin:8px 0 0">${affFrozenHint(data)}${data.blocked ? "；当前在不再构建名单里" : ""}</p>
      </div>
    </section>
    <section class="cfg-group">
      <div class="cfg-head"><h3>画像</h3><p>${profile.exists ? esc(profile.updated_text || "") : "还没生成"}</p></div>
      <div class="pad">
        <p class="profile-summary">${esc(profile.summary || "这个人还没有画像")}</p>
        <div class="chip-row">${words}</div>
        ${fieldRows}
        <div class="row" style="margin-top:12px">
          <button class="primary" id="prof-build">重新生成</button>
          <button class="ghost" id="prof-delete">删除画像并不再构建</button>
          ${data.blocked ? '<button class="ghost" id="prof-unblock">解除不再构建</button>' : ""}
        </div>
      </div>
    </section>
    <section class="cfg-group">
      <div class="cfg-head"><h3>记忆（${atoms.length}）</h3><p>改等级会立刻影响它忘得多快</p></div>
      <div class="pad">${rows || '<p class="hint">没有记忆</p>'}</div>
    </section>`;

  document.getElementById("subj-back").onclick = () => {
    subjectKey = "";
    render();
  };
  const range = document.getElementById("aff-range");
  const num = document.getElementById("aff-num");
  if (range)
    range.oninput = () => {
      if (num) num.textContent = range.value;
    };
  const affSave = document.getElementById("aff-save");
  if (affSave)
    affSave.onclick = async () => {
      await post("page/affinity/save", { key, score: Number(range.value) });
      render();
    };
  document.querySelectorAll(".lv-pick").forEach((sel) => {
    sel.onchange = async () => {
      await post("page/memory/level/set", { atom_id: Number(sel.dataset.id), level: Number(sel.value) });
      render();
    };
  });
  const build = document.getElementById("prof-build");
  if (build)
    build.onclick = async () => {
      build.textContent = "生成中…";
      try {
        await post("memory/profile/build", { key });
      } catch (err) {
        build.textContent = err.message;
        return;
      }
      render();
    };
  const del = document.getElementById("prof-delete");
  if (del)
    del.onclick = async () => {
      await post("page/memory/profile/delete", { key, block: true });
      render();
    };
  const unblock = document.getElementById("prof-unblock");
  if (unblock)
    unblock.onclick = async () => {
      await post("page/memory/profile/unblock", { key });
      render();
    };
}

/* ------------------------------------------------ 等级与遗忘 */
async function renderMemoryLevels() {
  if (isNightUI() && typeof njMemoryLevels === "function") return njMemoryLevels();
  const [levels, curve] = await Promise.all([
    get("page/memory/levels"),
    get("page/memory/forgetting", { days: 45 }),
  ]);
  const modes = curve.modes || ["off", "dream", "balanced", "ebbinghaus"];
  const modeText = {
    off: "不衰减",
    dream: "长夜式",
    balanced: "均衡",
    ebbinghaus: "保守",
  };
  const modeBtns = modes
    .map(
      (mode) =>
        `<button class="seg${mode === curve.mode ? " is-active" : ""}" data-mode="${mode}">${esc(
          modeText[mode] || mode
        )}</button>`
    )
    .join("");
  const marks = [7, 14, 30, 45];
  const head = marks.map((day) => `<th class="mono">${day} 天</th>`).join("");
  const body = [1, 2, 3, 4, 5]
    .map((level) => {
      const series = (curve.series || {})[String(level)] || [];
      const cells = marks
        .map((day) => `<td class="mono">${Math.round((series[day] || 0) * 100)}%</td>`)
        .join("");
      const review = (curve.review_days || {})[String(level)];
      return `<tr><td>${levelTag(level)} ${LEVEL_NAMES[level]}</td>${cells}<td class="mono">${
        review ? review + " 天" : "—"
      }</td></tr>`;
    })
    .join("");
  const dist = (levels.levels || [])
    .map((x) => `<li>${levelTag(x.level)} ${esc(x.name)}<span class="mono muted">${esc(x.count)} 条</span></li>`)
    .join("");
  view.innerHTML = `
    <section class="cfg-group">
      <div class="cfg-head"><h3>衰减档位</h3><p>决定一条记忆多久被忘掉。改完立刻生效，不用重启</p></div>
      <div class="pad">
        <div class="segmented" id="mode-row">${modeBtns}</div>
        <div class="row" style="margin-top:14px">
          <div class="field" style="margin:0"><label>归档阈值</label>
            <input type="number" step="0.01" id="fa-archive" value="${esc(curve.archive_ratio)}" /></div>
          <div class="field" style="margin:0"><label>遗忘阈值</label>
            <input type="number" step="0.01" id="fa-forget" value="${esc(curve.forget_ratio)}" /></div>
          <div class="field" style="margin:0"><label>命中加成</label>
            <input type="number" step="0.1" id="fa-boost" value="${esc(curve.hit_boost)}" /></div>
        </div>
        ${pageActions("保存衰减设置")}
      </div>
    </section>
    <section class="cfg-group">
      <div class="cfg-head"><h3>还能记住多久</h3><p>按当前档位模拟，数字是保留率</p></div>
      <div class="pad">
        <table class="mini-table"><thead><tr><th>等级</th>${head}<th>该复习</th></tr></thead>
        <tbody>${body}</tbody></table>
      </div>
    </section>
    <section class="cfg-group">
      <div class="cfg-head"><h3>等级定义</h3><p>0 级永不忘，1 级最容易被忘</p></div>
      <div class="pad"><ul class="plain-list">${dist}</ul></div>
    </section>`;

  document.querySelectorAll("[data-mode]").forEach((btn) => {
    btn.onclick = async () => {
      await post("page/memory/forgetting/save", { mode: btn.dataset.mode });
      settingsCache = null;
      render();
    };
  });
  const save = document.getElementById("page-save");
  if (save)
    save.onclick = async () => {
      const msg = document.getElementById("page-msg");
      await post("page/memory/forgetting/save", {
        archive_ratio: Number((document.getElementById("fa-archive") || {}).value),
        forget_ratio: Number((document.getElementById("fa-forget") || {}).value),
        hit_boost: Number((document.getElementById("fa-boost") || {}).value),
      });
      settingsCache = null;
      if (msg) msg.textContent = "已保存";
    };
}

/* ------------------------------------------------ 好感度 */
// 小工具：把任意值安全地转义成 HTML 文本（主体合并那边在用）
function pfEsc(s) {
  return esc(String(s == null ? "" : s));
}

/* ------------------------------------------------------------------ 主体合并
   同一个人的不同叫法（QQ 号 / 群名片 / 模型抽出来的称呼）会各算一个主体，
   好感度也因此重复结算。这里让人自己确认哪几个是同一个。 */
let mergeCache = null;
let lastMergeRunId = "";

async function renderSubjectMerge() {
  const host = view;
  const head = `
    <section class="cfg-group">
      <div class="cfg-head">
        <h3>主体合并</h3>
        <p>把同一个人的不同叫法合到一起。合并会迁移相关记忆、合并好感度，且不可撤销（执行前后台自动备份）。</p>
      </div>
      <div class="pad">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="primary" id="merge-scan">扫描重复主体</button>
          <span id="merge-tip" class="hint" style="margin:0"></span>
        </div>
        <div id="merge-list" style="margin-top:12px"></div>
        <div id="merge-actions" style="margin-top:12px;display:none">
          <button class="primary" id="merge-apply">合并选中</button>
        </div>
      </div>
    </section>`;
  if (host) host.innerHTML = head; else return;

  const listEl = document.getElementById("merge-list");
  const tipEl = document.getElementById("merge-tip");
  const actions = document.getElementById("merge-actions");

  const paint = () => {
    if (!mergeCache) return;
    if (mergeCache.error) {
      listEl.innerHTML = `<p class="hint">扫描失败：${esc(mergeCache.error)}</p>`;
      actions.style.display = "none";
      return;
    }
    const items = mergeCache.items || [];
    if (!items.length) {
      listEl.innerHTML = `<p class="hint">没有发现可合并的主体</p>`;
      actions.style.display = "none";
      return;
    }
    listEl.innerHTML = items
      .map((x, i) => `<label data-merge-row style="display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05);cursor:pointer">
        <input type="checkbox" data-merge-idx="${i}" ${x.atoms > 0 ? "checked" : ""} style="flex:none">
        <span style="flex:0 0 26%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(x.from)}">${esc(x.from)}</span>
        <span style="flex:none;opacity:.4">→</span>
        <span style="flex:0 0 26%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(x.to_name)}">${esc(x.to_name)}</span>
        <span class="hint" style="flex:1;min-width:0;margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(x.why)}</span>
        <span class="mono" style="flex:none;opacity:.6">${x.atoms}</span>
      </label>`)
      .join("");
    actions.style.display = "block";
  };

  document.getElementById("merge-scan").onclick = async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    btn.textContent = "扫描中…";
    try {
      const data = await get("subjects/merge_candidates");
      mergeCache = { items: (data && data.items) || [] };
    } catch (err) {
      mergeCache = { items: [], error: String((err && err.message) || err) };
    }
    btn.disabled = false;
    btn.textContent = "重新扫描";
    tipEl.textContent = mergeCache.error ? "" : `共 ${(mergeCache.items || []).length} 组候选`;
    paint();
  };

  let armed = false;
  document.getElementById("merge-apply").onclick = async (ev) => {
    const btn = ev.currentTarget;
    const picks = [...listEl.querySelectorAll("[data-merge-idx]:checked")].map((el) => {
      const it = (mergeCache.items || [])[Number(el.dataset.mergeIdx)] || {};
      return { from: it.from, to: it.to };
    }).filter((x) => x.from && x.to);
    if (!picks.length) {
      askToast("先勾选要合并的项", "err");
      return;
    }
    // iframe 里 confirm() 常常被浏览器静默拦掉（返回 false），所以用二次点击确认
    if (!armed) {
      armed = true;
      btn.textContent = `确定合并 ${picks.length} 组？再点一次`;
      setTimeout(() => {
        if (!armed) return;
        armed = false;
        btn.textContent = "合并选中";
      }, 6000);
      return;
    }
    armed = false;
    btn.disabled = true;
    btn.textContent = "合并中…";
    try {
      const r = await post("subjects/merge", { pairs: picks });
      const rid = (r && r.run_id) || "";
      lastMergeRunId = rid;
      askToast(`合并完成：${(r && r.merged) || 0} 组，迁移 ${(r && r.atoms) || 0} 条记忆`, "ok");
      mergeCache = null;
      if (tipEl) tipEl.textContent = "";
      if (listEl) listEl.innerHTML = "";
      if (actions) {
        actions.style.display = "block";
        actions.innerHTML = rid
          ? `<button class="danger" id="merge-undo">撤销这次合并</button>
             <span class="hint" style="margin-left:10px">run_id ${pfEsc(rid)}　只要之后没新写入就能整批退回</span>`
          : "";
        const undo = document.getElementById("merge-undo");
        if (undo) {
          undo.onclick = async () => {
            undo.disabled = true;
            undo.textContent = "撤销中…";
            try {
              const res = await post("subjects/rollback", { run_id: rid });
              askToast(res && res.already ? "这条已经撤销过了" : `已撤销 ${(res && res.reversed) || 0} 组`, "ok");
              mergeCache = null;
              renderSubjectMerge();
            } catch (err) {
              askToast((err && err.message) || "撤销失败", "err");
              undo.disabled = false;
              undo.textContent = "撤销这次合并";
            }
          };
        }
      }
      btn.textContent = "合并选中";
      btn.disabled = false;
    } catch (err) {
      askToast((err && err.message) || "合并失败", "err");
      btn.disabled = false;
      btn.textContent = "合并选中";
    }
  };

  if (mergeCache) paint();
}

async function renderAffinityPage() {
  if (isNightUI() && typeof njAffinityPage === "function") return njAffinityPage();
  const data = await get("page/affinity");
  const names = data.names || [];
  const thresholds = data.thresholds || [];
  const nameInputs = names
    .map(
      (name, index) =>
        `<div class="field" style="margin:0"><label>${index} 档</label>
          <input type="text" class="aff-name" data-index="${index}" value="${esc(name)}" /></div>`
    )
    .join("");
  const thresholdsInputs = thresholds
    .map(
      (value, index) =>
        `<div class="field" style="margin:0"><label>门槛 ${index}</label>
          <input type="number" class="aff-th" data-index="${index}" value="${esc(value)}" /></div>`
    )
    .join("");
  const ranking = (data.ranking || [])
    .map(
      (row, index) =>
        `<li><span class="mono muted">${index + 1}</span><span class="grow">${esc(
          row.name || row.subject_key
        )}</span>${stageChip(row.stage, row.stage_name)}<span class="mono">${Math.round(row.score)}</span></li>`
    )
    .join("");
  view.innerHTML = `
    <section class="cfg-group">
      <div class="cfg-head"><h3>阶段</h3><p>六个阶段的名字和分数门槛都能改</p></div>
      <div class="pad">
        <div class="row" style="margin-bottom:12px">${nameInputs}</div>
        <div class="row" style="margin-bottom:12px">${thresholdsInputs}</div>
        <div class="row">
          <div class="field" style="margin:0"><label>单日最大变化</label>
            <input type="number" step="1" id="aff-max" value="${esc(data.max_daily_delta)}" /></div>
          <div class="field" style="margin:0"><label>多少天不说话开始掉</label>
            <input type="number" step="1" id="aff-neglect" value="${esc(data.neglect_days)}" /></div>
        </div>
        ${pageActions("保存好感度设置")}
        <p class="hint" style="margin:10px 0 0">当前状态：${data.enabled ? "启用" : "关闭"}；${
          data.editable ? "面板可手改" : "面板只读"
        }</p>
      </div>
    </section>
    <section class="cfg-group">
      <div class="cfg-head"><h3>排行</h3><p>按分数从高到低</p></div>
      <div class="pad">
        ${ranking ? `<ol class="rank-list">${ranking}</ol>` : '<p class="hint">还没有记录</p>'}
        <div class="row" style="margin-top:12px"><button class="ghost" id="aff-settle">立刻结算一次</button></div>
      </div>
    </section>`;
  const save = document.getElementById("page-save");
  if (save)
    save.onclick = async () => {
      const msg = document.getElementById("page-msg");
      const names = [...document.querySelectorAll(".aff-name")].map((el) => el.value);
      const ths = [...document.querySelectorAll(".aff-th")].map((el) => Number(el.value));
      await post("page/affinity/config", {
        stage_names: names,
        stage_thresholds: ths,
        max_daily_delta: Number((document.getElementById("aff-max") || {}).value),
        neglect_days: Number((document.getElementById("aff-neglect") || {}).value),
      });
      settingsCache = null;
      if (msg) msg.textContent = "已保存";
    };
  const settle = document.getElementById("aff-settle");
  if (settle)
    settle.onclick = async () => {
      settle.textContent = "结算中…";
      try {
        await post("page/affinity/settle", {});
      } catch (err) {
        settle.textContent = err.message;
        return;
      }
      render();
    };
}

/* ------------------------------------------------ 头像 */
async function renderAvatarPage() {
  if (isNightUI() && typeof njAvatarPage === "function") return njAvatarPage();
  const data = await get("page/avatar");
  const items = data.items || [];
  const pending = data.pending || [];
  const card = (item, isPending) => `<div class="avatar-card">
      <img src="file://${esc(item.path)}" alt="" onerror="this.style.opacity=0.25" />
      <div class="avatar-meta">
        <span class="mono small">${esc(item.source)}</span>
        <span class="mono small muted">${esc(item.used_text || "")}</span>
      </div>
      <div class="avatar-actions">
        ${isPending ? `<button class="primary tiny" data-trust="${item.id}">允许</button>` : ""}
        <button class="ghost tiny" data-use="${item.id}">换成这张</button>
        <button class="ghost tiny" data-drop="${item.id}">删</button>
      </div>
    </div>`;
  const last = data.last || {};
  const lastText = last.at ? `${fmtTime(last.at)}　来自 ${esc(last.reason || "user")}` : "还没换过";
  view.innerHTML = `
    <section class="cfg-group">
      <div class="cfg-head"><h3>换头像</h3><p>${data.available ? "通道可用" : "现在拿不到 bot 客户端"}</p></div>
      <div class="pad">
        <p class="hint" style="margin:0 0 10px">上次更换：${lastText}；今天已自动换 ${esc(
          data.used_today || 0
        )} / ${esc(data.daily_limit || 0)} 次</p>
        <div class="row">
          <button class="primary" id="avatar-random">从库里挑一张换</button>
          <span class="saved" id="avatar-msg"></span>
        </div>
        <p class="hint" style="margin:10px 0 0">
          自动换只用「可信」库里的图：你主动存进来的，或者你点过「允许」的。她自己扒到的图只会待在待审区
        </p>
      </div>
    </section>
    <section class="cfg-group">
      <div class="cfg-head"><h3>可信库（${items.length}）</h3><p>能被自动换用到的图</p></div>
      <div class="pad"><div class="avatar-grid">${
        items.map((item) => card(item, false)).join("") || '<p class="hint">库里还是空的，先把喜欢的图存进来</p>'
      }</div></div>
    </section>
    <section class="cfg-group">
      <div class="cfg-head"><h3>待审（${pending.length}）</h3><p>她自己找的图，你允许了才会被用</p></div>
      <div class="pad"><div class="avatar-grid">${
        pending.map((item) => card(item, true)).join("") || '<p class="hint">没有待审的图</p>'
      }</div></div>
    </section>`;

  document.querySelectorAll("[data-use]").forEach((btn) => {
    btn.onclick = async () => {
      const msg = document.getElementById("avatar-msg");
      try {
        await post("page/avatar/set", { id: Number(btn.dataset.use) });
        if (msg) msg.textContent = "换好了";
      } catch (err) {
        if (msg) msg.textContent = err.message;
      }
    };
  });
  document.querySelectorAll("[data-trust]").forEach((btn) => {
    btn.onclick = async () => {
      await post("page/avatar/pool", { action: "trust", id: Number(btn.dataset.trust) });
      render();
    };
  });
  document.querySelectorAll("[data-drop]").forEach((btn) => {
    btn.onclick = async () => {
      await post("page/avatar/pool", { action: "delete", id: Number(btn.dataset.drop) });
      render();
    };
  });
  const random = document.getElementById("avatar-random");
  if (random)
    random.onclick = async () => {
      const msg = document.getElementById("avatar-msg");
      try {
        await post("page/avatar/set", { id: 0, pool_id: 0 });
        // id=0 时后端会从库里随机挑
        const picked = items[0];
        if (picked) await post("page/avatar/set", { id: picked.id });
        if (msg) msg.textContent = "换好了";
        render();
      } catch (err) {
        if (msg) msg.textContent = err.message;
      }
    };
}

(async function boot() {
  bindGroupCard();
  try {
    await ensureBridge();
  } catch (err) {
    view.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
    return;
  }
  const start = parseHash();
  setView(start.view, start.sub);
})();


/* ==================================================================
   v6 · 记忆三板块：记忆总览 / 按用户分 / 按会话分
   ================================================================== */

const M3_KIND_CN = { pref: "偏好", preference: "偏好", event: "事件", relation: "关系", emotion: "情绪", promise: "约定", fact: "事实" };

function m3KindCN(kind) {
  return M3_KIND_CN[kind] || memKindCN(kind) || "事实";
}
function m3KindClass(kind) {
  if (kind === "event") return "m";
  if (kind === "relation") return "v";
  return "p";
}

/* 头像：私聊圆形、群聊方形；取不到就回落首字。
   session_id 可能是纯号码（msg_archive 的存法），也可能是 umo，两种都认。 */
function avatarParts(kind, id, sessionId) {
  const raw = String(id == null ? "" : id).trim();
  const sid = String(sessionId || "").trim();
  const group = (sid.match(/GroupMessage[:_](\d{5,12})/i) || sid.match(/group[:_](\d{5,12})/i) || [])[1];
  const user = (sid.match(/(?:FriendMessage|PrivateMessage|private|friend)[:_](\d{5,12})/i) || [])[1];
  if (kind === "group") return { kind: "group", key: group || (/^\d{5,12}$/.test(raw) ? raw : "") };
  if (group) return { kind: "group", key: group };
  if (user) return { kind: "user", key: user };
  if (/^\d{5,12}$/.test(raw)) return { kind: kind || "user", key: raw };
  return { kind: kind || "user", key: "" };
}

function avatarUrl(kind, id, sessionId) {
  const parts = avatarParts(kind, id, sessionId);
  /* 只有纯数字才是 QQ 号或群号，昵称类主体直接回落首字 */
  if (!parts.key) return "";
  // 尺寸从 640 降到 140：列表里显示的就这么点大，拉 640 的图等于把流量放大 20 倍，
  // 「按会话分」一屏十几张图全靠它，卡顿主要来自这里。
  // 140 的图会被浏览器缓存，第二次进来就不发请求了。
  if (parts.kind === "group") return `https://p.qlogo.cn/gh/${parts.key}/${parts.key}/140`;
  return `https://q1.qlogo.cn/g?b=qq&nk=${parts.key}&s=140`;
}

function avatarHtml(kind, id, name, cls, sessionId) {
  const url = avatarUrl(kind, id, sessionId);
  const parts = avatarParts(kind, id, sessionId);
  const initial = esc(String(name || id || "?").slice(0, 1));
  const klass = `v6-av${parts.kind === "group" ? " sq" : ""}${cls ? " " + cls : ""}`;
  if (!url) return `<span class="${klass}"><span class="fb">${initial}</span></span>`;
  return `<span class="${klass}"><img src="${esc(url)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" data-fb="${initial}" onerror="var p=this.parentNode;this.remove();p.textContent=this.dataset.fb"></span>`;
}

function m3Day(ts) {
  if (!ts) return "";
  const d = new Date(Number(ts) * 1000);
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")}`;
}
function m3When(ts) {
  if (!ts) return "";
  const now = Date.now() / 1000;
  const diff = now - Number(ts);
  const d = new Date(Number(ts) * 1000);
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (diff < 86400) return `今天 ${hm}`;
  if (diff < 172800) return `昨天 ${hm}`;
  if (diff < 7 * 86400) return `${Math.floor(diff / 86400)} 天前`;
  return m3Day(ts);
}
function m3Remain(atom) {
  if (atom.expire_at == null || Number(atom.expire_at) === 0) return "永久";
  const left = Math.round((Number(atom.expire_at) - Date.now() / 1000) / 86400);
  if (left <= 0) return "已过期";
  return `剩 ${left} 天`;
}

function m3AtomRow(atom) {
  const kind = m3KindCN(atom.kind);
  return `<div class="m3-ev">
    <div class="pt"><i></i></div>
    <div class="bd">
      <div class="meta">
        <span class="m3-chip ${m3KindClass(atom.kind)}">${esc(kind)}</span>
        <em>${esc(m3Remain(atom))} · ${esc(m3When(atom.created_at))}</em>
      </div>
      <p>${esc(atom.text || "")}</p>
    </div>
  </div>`;
}

/* ------------------------------------------------------------ 记忆总览 */
async function renderM3Overview() {
  if (isNightUI() && typeof njM3Overview === "function") return njM3Overview();
  const data = await get("page/memory/m3/overview");
  const total = Number(data.total || 0);
  const kinds = data.kinds || [];
  const maxKind = Math.max(1, ...kinds.map((k) => Number(k.count || 0)));
  const heat = data.heatmap || {};
  const recent = data.recent || [];

  /* 最近 182 天按周聚合成 26 格 */
  const cells = [];
  const today = new Date();
  for (let w = 25; w >= 0; w--) {
    let sum = 0;
    for (let d = 0; d < 7; d++) {
      const day = new Date(today.getTime() - (w * 7 + d) * 86400000);
      const key = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
      sum += Number(heat[key] || 0);
    }
    cells.push(sum);
  }
  const maxCell = Math.max(1, ...cells);
  const heatHtml = cells
    .map((n) => {
      const ratio = n / maxCell;
      const alpha = n === 0 ? 0.06 : 0.18 + ratio * 0.72;
      return `<i style="background:rgba(155,196,177,${alpha.toFixed(2)})" title="${n} 条"></i>`;
    })
    .join("");

  const kindHtml = kinds.length
    ? kinds
        .map(
          (k) => `<div class="m3-bar">
            <div class="k">${esc(m3KindCN(k.kind))}</div>
            <div class="tr"><i style="width:${Math.round((Number(k.count) / maxKind) * 100)}%"></i></div>
            <div class="n">${esc(k.count)}</div>
          </div>`
        )
        .join("")
    : '<p class="hint">还没有记忆，聊几句就会开始记</p>';

  view.innerHTML = `
    <div class="m3-strip">
      <div class="m3-st"><div class="n">记忆总条数</div><div class="v">${esc(total)}<small>条</small></div>
        <div class="t">近 30 天新增 ${esc(data.month_new || 0)}</div></div>
      <div class="m3-st"><div class="n">永久记忆</div><div class="v">${esc(data.permanent_pct || 0)}<small>%</small></div>
        <div class="t">${esc(data.permanent || 0)} 条不会淡</div></div>
      <div class="m3-st"><div class="n">快忘掉的</div><div class="v">${esc(data.fading || 0)}<small>条</small></div>
        <div class="t">7 天内会淡出</div></div>
      <div class="m3-st"><div class="n">认识的人 / 会话</div><div class="v">${esc(data.people || 0)}<small>/ ${esc(data.sessions || 0)}</small></div>
        <div class="t">${data.pending ? `待确认 ${esc(data.pending)} 条` : "没有待确认"}</div></div>
    </div>

    <div class="m3-cols">
      <div>
        <div class="m3-blk-h"><h2>记忆类型</h2><span>按条数</span></div>
        ${kindHtml}
        <div class="m3-blk-h" style="margin-top:22px"><h2>记过话的日子</h2><span>近 26 周 · 颜色越深条数越多</span></div>
        <div class="m3-heat">${heatHtml}</div>
        <div class="m3-heatlbl"><span>半年前</span><span>3 个月前</span><span>现在</span></div>
        <div class="m3-note">
          <em>记忆系统的自述</em>
          <p>${esc(data.attitude || "")}</p>
        </div>
      </div>
      <div>
        <div class="m3-blk-h"><h2>最近写入</h2><span>新的在上面</span></div>
        ${recent.length ? recent.map((a) => m3AtomRow(a, true)).join("") : '<p class="hint">还没有最近写入</p>'}
      </div>
    </div>
  `;
}

/* ------------------------------------------------------------ 按用户分 */
let m3UserId = "";
/* @deprecated 0.7.0 起不再挂 tab（「按用户分」取消）。函数留着，回溯用。 */
async function renderM3Users() {
  if (isNightUI() && typeof njM3Users === "function") return njM3Users();
  const list = await get("memory/subjects", { limit: 200 });
  const items = (list.items || []).map((x) => ({ ...x, key: x.user_id || x.subject_id || "" }));
  if (!items.length) {
    view.innerHTML = '<div class="empty">还没有记住任何人</div>';
    return;
  }
  if (!m3UserId || !items.some((x) => x.key === m3UserId)) m3UserId = items[0].key;
  const detail = await get("page/memory/subject", { key: m3UserId });
  const atoms = (detail.atoms || []).slice(0, 6);
  const aff = detail.affinity || {};
  const profile = detail.profile || {};
  const prof = profile.data || profile.fields || profile || {};
  const score = aff.score != null ? aff.score : aff.value != null ? aff.value : 0;
  const stageName = aff.stage_name || aff.stage || "—";

  const rows = items
    .slice(0, 12)
    .map((x) => {
      const name = x.display_name || x.name || x.nickname || x.key;
      const cnt = x.atom_count != null ? x.atom_count : x.count != null ? x.count : 0;
      const affObj = x.affinity || {};
      const own = affObj.score != null ? affObj.score : affObj.value != null ? affObj.value : affObj;
      const lv = x.level || affObj.level || "";
      return `<div class="m3-row ${x.key === m3UserId ? "is-on" : ""}" data-m3-uid="${esc(x.key)}">
        ${avatarHtml("user", x.key, name)}
        <div><b>${esc(name)}</b>
          <span>${esc(cnt)} 条${lv ? ` · Lv.${esc(lv)}` : ""}</span></div>
        <em>${typeof own === "number" ? esc(Math.round(own)) : ""}</em>
      </div>`;
    })
    .join("");

  const profItems = [
    ["称呼", prof.call || prof.nickname || "—"],
    ["作息", prof.sleep || prof.routine || "—"],
    ["忌口", prof.food || prof.taste || "—"],
    ["雷点", prof.taboo || prof.dislike || "—"],
  ]
    .map(([k, v]) => `<div><span class="m3-chip">${esc(k)}</span><div style="font-size:15.5px;margin-top:6px">${esc(v)}</div></div>`)
    .join("");

  const stageBars = (detail.stage_names || ["陌生", "眼熟", "常聊", "熟", "很近", "亲近", "现在"])
    .map((name, i, arr) => {
      const on = aff.stage_index != null ? i <= Number(aff.stage_index) : i >= arr.length - 1;
      return `<i class="${on ? "hi" : ""}" style="height:${28 + i * 8}%" title="${esc(name)}"></i>`;
    })
    .join("");

  view.innerHTML = `
    <div style="display:grid;grid-template-columns:296px 1fr;gap:26px">
      <div class="m3-list">
        <div class="hd"><h2>认识的人</h2><span>${esc(list.total || items.length)}</span></div>
        ${rows}
      </div>
      <div>
        <div class="m3-hero">
          ${avatarHtml("user", m3UserId, detail.name || m3UserId, "lg")}
          <div><b>${esc(detail.name || m3UserId)}</b>
            <span>${esc(atoms.length)} 条近期记忆 · Lv.${esc(aff.level || "-")} · ${esc(stageName)} · 好感 ${esc(Math.round(score))}</span></div>
        </div>
        <div style="display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:26px">
          <div>
            <div class="m3-blk-h"><h2>这个人她记住了什么</h2><span>最近 ${esc(atoms.length)} 条</span></div>
            ${atoms.length ? atoms.map((a) => m3AtomRow(a, false)).join("") : '<p class="hint">关于这个人还没有记忆</p>'}
          </div>
          <div>
            <div class="m3-blk-h"><h2>画像与好感</h2><span>她的判断</span></div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">${profItems}</div>
            <div class="m3-bars">${stageBars}</div>
            <div class="m3-barlbl"><span>陌生</span><span>熟</span><span>现在</span></div>
            <div class="m3-note">
              <em>她私下记的一句</em>
              <p>${esc(prof.summary || prof.note || "还没有形成判断，多聊几次就有了。")}</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;

  view.querySelectorAll("[data-m3-uid]").forEach((row) => {
    row.onclick = () => {
      m3UserId = row.dataset.m3Uid;
      render();
    };
  });
}

/* ------------------------------------------------------------ 按会话分 */
let m3SessionId = "";
async function renderM3Sessions() {
  if (isNightUI() && typeof njM3Sessions === "function") return njM3Sessions();
  const data = await get("page/memory/sessions");
  const items = data.items || [];
  if (!items.length) {
    view.innerHTML = '<div class="empty">还没有按会话记下东西</div>';
    return;
  }
  if (!m3SessionId || !items.some((x) => x.session_id === m3SessionId)) m3SessionId = items[0].session_id;
  const detail = await get("page/memory/session", { session_id: m3SessionId });
  const atoms = (detail.atoms || []).slice(0, 6);
  const kinds = detail.kinds || [];
  const hours = detail.hours || {};
  const maxKind = Math.max(1, ...kinds.map((k) => Number(k.count || 0)));
  const maxHour = Math.max(1, ...Object.values(hours).map((n) => Number(n || 0)));

  const rows = items
    .slice(0, 12)
    .map((x) => {
      const isGroup = x.kind === "group" || String(x.label || "").includes("群");
      return `<div class="m3-row ${x.session_id === m3SessionId ? "is-on" : ""}" data-m3-sid="${esc(x.session_id)}">
        ${avatarHtml(isGroup ? "group" : "user", isGroup ? x.session_id : x.session_id, x.label)}
        <div><b>${esc(x.label || x.session_id)}</b>
          <span>${esc(x.count)} 条 · ${esc(m3When(x.last_at))}</span></div>
        <em>›</em>
      </div>`;
    })
    .join("");

  const kindHtml = kinds.length
    ? kinds
        .map(
          (k) => `<div class="m3-bar"><div class="k">${esc(m3KindCN(k.kind))}</div>
            <div class="tr"><i style="width:${Math.round((Number(k.count) / maxKind) * 100)}%"></i></div>
            <div class="n">${esc(k.count)}</div></div>`
        )
        .join("")
    : '<p class="hint">没有类型统计</p>';

  const hourBars = Array.from({ length: 24 }, (_, h) => {
    const n = Number(hours[String(h)] || 0);
    return `<i class="${n === maxHour && n > 0 ? "hi" : ""}" style="height:${Math.max(6, Math.round((n / maxHour) * 100))}%" title="${h} 点 ${n} 条"></i>`;
  }).join("");

  const spanDays = items.find((x) => x.session_id === m3SessionId)?.days || 1;
  const lastAt = items.find((x) => x.session_id === m3SessionId)?.last_at;

  view.innerHTML = `
    <div style="display:grid;grid-template-columns:320px 1fr;gap:26px">
      <div class="m3-list">
        <div class="hd"><h2>会话</h2><span>${esc(data.total || items.length)} 个</span></div>
        ${rows}
      </div>
      <div>
        <div class="m3-hero">
          ${avatarHtml(String(detail.kind || "").includes("group") ? "group" : "user", m3SessionId, detail.label, "lg")}
          <div><b>${esc(detail.label || m3SessionId)}</b>
            <span>跨度 ${esc(spanDays)} 天 · ${esc((detail.atoms || []).length)} 条记忆 · 最近 ${esc(m3When(lastAt))}</span></div>
        </div>
        <div style="display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:26px">
          <div>
            <div class="m3-blk-h"><h2>这个会话里记住的事</h2><span>按时间倒序</span></div>
            ${atoms.length ? atoms.map((a) => m3AtomRow(a, true)).join("") : '<p class="hint">这个会话还没有记忆</p>'}
          </div>
          <div>
            <div class="m3-blk-h"><h2>这个会话的形状</h2><span>活跃时段 / 类型</span></div>
            <p class="hint" style="margin:0 0 4px">她在这个会话最活跃的时段</p>
            <div class="m3-bars" style="height:88px">${hourBars}</div>
            <div class="m3-barlbl"><span>0 点</span><span>6</span><span>12</span><span>18</span><span>24 点</span></div>
            <div class="m3-blk-h" style="margin-top:20px"><h2>记忆类型</h2><span>这个会话</span></div>
            ${kindHtml}
          </div>
        </div>
      </div>
    </div>
  `;

  view.querySelectorAll("[data-m3-sid]").forEach((row) => {
    row.onclick = () => {
      m3SessionId = row.dataset.m3Sid;
      render();
    };
  });
}


/* ------------------------------------------------------------------ 夜航分发门
   data-ui="night" 时所有页面走 night.js 的全量实现（概览/人格/行为/记忆/长夜/工具/QQ/系统）
   这里只留判断，具体渲染在 night.js，文件更短也更好改
   ------------------------------------------------------------------ */
function isNightUI() {
  return document.documentElement.dataset.ui === "night";
}


/* ------------------------------------------------------------------ tooltip
   全局面板里每个 data-tip 都走这一个 fixed 层。
   为什么不用 CSS ::after：父容器一旦有 overflow:hidden 就会被裁掉（设置卡片
   里那张"高级"折叠面板就是），而且贴近屏幕边缘时会直接溢出屏幕。 */
(function initTooltip() {
  let layer = null;

  function ensure() {
    if (layer && layer.isConnected) return layer;
    layer = document.createElement("div");
    layer.id = "cg-tip";
    document.body.appendChild(layer);
    return layer;
  }

  function show(target) {
    const text = target.getAttribute("data-tip");
    if (!text || !text.trim()) return;
    const tip = ensure();
    tip.textContent = text;
    tip.style.display = "block";
    tip.style.opacity = "0";

    const box = target.getBoundingClientRect();
    const size = tip.getBoundingClientRect();
    let left = box.left + box.width / 2 - size.width / 2;
    left = Math.max(8, Math.min(window.innerWidth - size.width - 8, left));
    let top = box.top - size.height - 9;
    if (top < 8) top = box.bottom + 9;      // 上方放不下就翻到下方
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
    requestAnimationFrame(() => { tip.style.opacity = "1"; });
  }

  function hide() {
    if (layer) layer.style.opacity = "0";
  }

  const pick = (node) => (node && node.closest ? node.closest("[data-tip]") : null);
  document.addEventListener("mouseover", (e) => {
    const target = pick(e.target);
    if (target) show(target);
  });
  document.addEventListener("mouseout", (e) => {
    if (pick(e.target)) hide();
  });
  document.addEventListener("focusin", (e) => {
    const target = pick(e.target);
    if (target) show(target);
  });
  document.addEventListener("focusout", hide);
  window.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);
})();
