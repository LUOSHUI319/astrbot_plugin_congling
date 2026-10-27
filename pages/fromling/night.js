/* 夜航手账渲染层 · 由 night-part1/2/3 合成，勿分开改 */
/* ==================================================================
   从绫开始 · 夜航手账 —— 渲染层（全量接管）
   载入顺序：night.js → app.js。这里只定义函数，不执行任何东西，
   等 app.js 的 render 分发过来再跑。
   对外入口（app.js 里 if (isNightUI()) 调这些）：
     njOverview / njPersona / njBehavior / njMemory / njNight / njTools / njQQ / njSystem
     njM3Overview / njM3Users / njM3Sessions / njMemoryLevels / njAffinityPage / njAvatarPage
   ================================================================== */

/* ------------------------------------------------------------ 工具 */
function njPad(n, w) {
  return String(n).padStart(w || 2, "0");
}
function njIsNight() {
  return document.documentElement.dataset.ui === "night";
}
/** 标题上方那行面包屑：靠 #title[data-crumb] + CSS ::before 渲染 */
function njTitle(no) {
  if (titleEl && no) titleEl.dataset.crumb = `从绫开始 · ${no}`;
}
/** 轻提示：iframe 里 alert 也会被拦，用它代替 */
function njToast(message, kind) {
  let box = document.getElementById("nj-toast-box");
  if (!box) {
    box = document.createElement("div");
    box.id = "nj-toast-box";
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

/** 面板可能跑在 iframe 里，window.confirm 会被沙箱拦掉（返回 false 且没有任何提示），
 *  表现出来就是「点了按钮没反应」。所以自己做一个，返回 Promise<boolean>。 */
function njConfirm(message, opts) {
  const o = opts || {};
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "nj-modal";
    wrap.innerHTML = `<div class="nj-modal-card">
      <p>${esc(message)}</p>
      <div class="nj-modal-ops">
        <button type="button" class="nj-btn sm" data-no>${esc(o.cancel || "算了")}</button>
        <button type="button" class="nj-btn sm primary" data-yes>${esc(o.ok || "确定")}</button>
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

/** 壳层静态部分：导航编号、侧栏人格卡。可重复调用。 */
function njShell() {
  if (!njIsNight()) return;
  njSprite();
  const nav = document.getElementById("nav");
  if (nav) {
    nav.querySelectorAll(".nav-item").forEach((btn, i) => {
      if (!btn.dataset.no) btn.dataset.no = njPad(i + 1);
    });
  }
  const foot = document.querySelector(".side-foot");
  if (foot && !foot.querySelector(".nj-persona")) {
    const card = document.createElement("div");
    card.className = "nj-persona";
    card.innerHTML = '<i></i><div><small>当前人格</small><b id="nj-persona-name">—</b></div><em>在线</em>';
    const group = foot.querySelector(".group-card");
    foot.insertBefore(card, group || foot.firstChild);
  }
  if (compatLine && state.compat) {
    /* 外部插件名太长，去掉 astrbot_plugin_ 前缀，窄屏才不会被拦腰截断 */
    const present = Object.keys(state.compat.present || {}).map((name) => String(name).replace(/^astrbot_plugin_/, ""));
    const yielding = Object.keys(state.compat.yield || {}).filter((key) => state.compat.yield[key]);
    compatLine.textContent = `外部 ${present.length ? present.join(" / ") : "无"}\n${yielding.length ? `让位 ${yielding.join(" / ")}` : "全部自管"}`;
  }
  if (personaTag && personaTag.textContent === "—" && window.__njPersonaTag) personaTag.textContent = window.__njPersonaTag;
  njPersonaBoot();
}

/** 侧栏「当前人格」要跟后端实际生效的人格走。
 *  原来只有人格页保存时才写一次名字，所以永远停在「绫」——
 *  切到 AstrBot 人格（或改了内置名字）侧栏不会变。 */
async function njPersonaBoot(force) {
  const now = Date.now();
  if (!force && (window.__njPersonaBusy || now - (window.__njPersonaAt || 0) < 30000)) return;
  window.__njPersonaBusy = true;
  window.__njPersonaAt = now;
  try {
    const data = await get("page/persona");
    const mode = String(data.mode || "builtin");
    let name = String(data.name || "");
    let id = String(data.persona_id || "ling");
    if (mode === "astrbot") {
      const wanted = String(data.astrbot_persona_id || "");
      const list = (data.astrbot_personas && data.astrbot_personas.items) || [];
      const hit = list.find((item) => String(item.id || item.persona_id || "") === wanted);
      name = String((hit && (hit.name || hit.id)) || wanted || "AstrBot 人格");
      id = wanted ? `AstrBot · ${wanted}` : "AstrBot";
    } else if (mode === "none") {
      name = name || "无人格";
      id = "关闭";
    }
    if (name) njPersonaName(name, id);
  } catch (err) {
    /* 拉不到就保持原样，不影响页面 */
  } finally {
    window.__njPersonaBusy = false;
  }
}
function njPersonaName(name, id) {
  const el = document.getElementById("nj-persona-name");
  if (el && name) el.textContent = name;
  if (name) {
    window.__njPersonaTag = `${name} · ${id || "ling"}`;
    if (personaTag) personaTag.textContent = window.__njPersonaTag;
  }
}

function njGreeting() {
  const d = new Date();
  const h = d.getHours();
  const word = h < 5 ? "夜深了，还不睡" : h < 11 ? "上午好" : h < 14 ? "午后了" : h < 19 ? "傍晚好" : "夜色正好，慢慢聊";
  const week = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][d.getDay()];
  return {
    word,
    date: `${d.getMonth() + 1} 月 ${d.getDate()} 日 · ${week} · ${njPad(h)}:${njPad(d.getMinutes())}`,
    clock: `${njPad(h)}:${njPad(d.getMinutes())}`,
  };
}

function njDayKey(ts) {
  const d = new Date(Number(ts) * 1000);
  return `${d.getFullYear()}-${njPad(d.getMonth() + 1)}-${njPad(d.getDate())}`;
}
function njClock(ts) {
  const d = new Date(Number(ts) * 1000);
  return `${njPad(d.getHours())}:${njPad(d.getMinutes())}`;
}
/** 今天 03:20 / 昨天 22:10 / 3 天前 / 10-21（按自然日算，不是按 24 小时差） */
function njWhen(ts) {
  if (!ts) return "";
  const now = new Date();
  const d = new Date(Number(ts) * 1000);
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(d)) / 86400000);
  if (days <= 0) return `今天 ${njClock(ts)}`;
  if (days === 1) return `昨天 ${njClock(ts)}`;
  if (days < 7) return `${days} 天前`;
  return `${njPad(d.getMonth() + 1)}-${njPad(d.getDate())}`;
}
/** 时间轴的大号锚点：今天 / 昨天 / 10 月 24 日 */
function njDayLabel(ts) {
  const now = new Date();
  const d = new Date(Number(ts) * 1000);
  const sameDay = (a, b) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (sameDay(d, now)) return "今天";
  const y = new Date(now.getTime() - 86400000);
  if (sameDay(d, y)) return "昨天";
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日`;
}

/** 迷你折线：values 数组 → 内联 SVG（宽 66 高 24，绝对定位在指标卡右上）
 *  先降采样到 9 段再画，30 个点直接画会变成锯齿噪波 */
function njSpark(values) {
  const raw = (values || []).map((v) => Number(v) || 0);
  if (!raw.length) return "";
  const n = Math.min(9, raw.length);
  const step = raw.length / n;
  const arr = [];
  for (let i = 0; i < n; i += 1) {
    const s = Math.floor(i * step);
    const e = Math.max(s + 1, Math.floor((i + 1) * step));
    let sum = 0;
    for (let j = s; j < e; j += 1) sum += raw[j] || 0;
    arr.push(sum / (e - s));
  }
  /* 再平滑两遍，指标卡上的线只做趋势提示，不该是一排锯齿 */
  for (let pass = 0; pass < 2; pass += 1) {
    for (let i = 1; i < arr.length - 1; i += 1) arr[i] = (arr[i - 1] + arr[i] * 2 + arr[i + 1]) / 4;
  }
  if (arr.every((v) => v === 0)) arr[arr.length - 1] = 0.001;
  const w = 66;
  const h = 24;
  const max = Math.max(1, ...arr);
  const gap = w / (arr.length - 1);
  const pts = arr.map((v, i) => `${(i * gap).toFixed(1)},${(h - (v / max) * (h - 5) - 2.5).toFixed(1)}`);
  const area = `0,${h} ${pts.join(" ")} ${w},${h}`;
  return `<svg class="nj-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <polygon class="fill" points="${area}"></polygon>
    <polyline class="line" points="${pts.join(" ")}"></polyline>
  </svg>`;
}

/** 按天取热力值，返回近 days 天数组 */
function njHeatSeries(heat, days, endOffset) {
  const out = [];
  const today = new Date();
  for (let i = days - 1; i >= 0; i -= 1) {
    const day = new Date(today.getTime() - (i + (endOffset || 0)) * 86400000);
    const key = `${day.getFullYear()}-${njPad(day.getMonth() + 1)}-${njPad(day.getDate())}`;
    out.push(Number((heat || {})[key] || 0));
  }
  return out;
}

const NJ_KIND_CN = { fact: "事实", pref: "偏好", preference: "偏好", event: "事件", promise: "承诺", promise_note: "约定", relation: "关系", emotion: "情绪" };
function njKindCN(kind) {
  return NJ_KIND_CN[kind] || memKindCN(kind) || "事实";
}
const NJ_KIND_CLASS = { pref: "k-pref", preference: "k-pref", event: "k-event", promise: "k-promise", relation: "k-relation", emotion: "k-emotion" };

/** 从 session_id / umo 里挖出 (kind, 号码)。
 *  session_id 有两种形态：纯群号/QQ 号（msg_archive 存法），
 *  以及 aiocqhttp:FriendMessage:3190568934 这种 umo —— 两种都要能认。
 */
function njAvatarParts(kind, id, sessionId) {
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

/** 头像：私聊圆形、群聊方形；只有纯数字（或能从 umo 解析出号码）才去拉 QQ 头像 */
function njAvatar(id, name, opts) {
  const o = opts || {};
  const parts = njAvatarParts(o.kind, id, o.sessionId);
  const key = parts.key;
  const url = /^\d{5,12}$/.test(key)
    ? parts.kind === "group"
      ? `https://p.qlogo.cn/gh/${key}/${key}/640`
      : `https://q1.qlogo.cn/g?b=qq&nk=${key}&s=640`
    : "";
  const initial = esc(String(name || id || "?").slice(0, 1));
  const klass = `nj-avatar${parts.kind === "group" ? " sq" : ""}${o.lg ? " lg" : ""}`;
  if (!url) return `<span class="${klass}"><span>${initial}</span></span>`;
  return `<span class="${klass}"><img src="${esc(url)}" alt="" loading="lazy" referrerpolicy="no-referrer"
    onerror="var p=this.parentNode;this.remove();p.textContent='${initial}'"></span>`;
}

const NJ_LEVEL_NAMES = ["不可忘", "随口", "细节", "平常", "在意", "关键"];

/** 记忆等级用点阵表示：1~5 级亮几个点，0 级是双环（不可忘）。
 *  比「L3」这种标签好认，也不占宽度。名称放 title 里。 */
function njLevelTag(level) {
  const n = Math.max(0, Math.min(5, Number(level) || 0));
  const name = NJ_LEVEL_NAMES[n] || "";
  if (n === 0) return `<span class="nj-lv lv0" title="0 级 · ${esc(name)}"><i></i></span>`;
  const dots = Array.from({ length: 5 }, (_, i) => `<i class="${i < n ? "on" : ""}"></i>`).join("");
  return `<span class="nj-lv lv${n}" title="${n} 级 · ${esc(name)}">${dots}</span>`;
}

/** 截一段文本，超长补省略号（用于卡片里的引文） */
function njClip(text, limit) {
  const clean = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
  const max = limit || 64;
  return clean.length > max ? clean.slice(0, max) + "…" : clean;
}

/** 未读消息时间：凌晨用时刻，其他用“几天前” */
function njUnreadClock(ts) {
  if (!ts) return "—";
  return njClock(ts);
}

/* ------------------------------------------------------------ 概览 */
async function njOverview() {
  njShell();
  njTitle("01");
  const [data, ov, aff, night] = await Promise.all([
    get("page/overview"),
    get("page/memory/m3/overview").catch(() => ({})),
    get("page/affinity").catch(() => ({})),
    get("night/state").catch(() => ({})),
  ]);
  state.health = data.health || {};
  state.compat = (data.health || {}).compat || null;
  const stats = data.stats || {};
  const health = data.health || {};
  const persona = health.persona || {};
  const modules = health.modules || {};
  /* 名字以 /page/persona 的实际情况为准：这里只是概览页的兜底显示，
     真正的名字交给 njPersonaBoot（它知道 mode=astrbot 时该显示哪个人格） */
  njPersonaBoot(true);

  const heat = ov.heatmap || {};
  const g = njGreeting();
  const recent = ov.recent || [];
  const quote = recent.find((a) => a.kind === "relation" || a.kind === "emotion") || recent[0];

  const total = Number(ov.total || data.memory_active || 0);
  const monthNew = Number(ov.month_new || 0);
  const permanent = Number(ov.permanent || 0);
  const people = Number(ov.people || 0);
  const sessions = Number(ov.sessions || 0);
  const heat30 = njHeatSeries(heat, 30);

  const affList = (aff.ranking || []).slice().sort((a, b) => (Number(b.score) || 0) - (Number(a.score) || 0));
  const affTop = affList[0];
  const affScore = affTop ? Number(affTop.score) || 0 : 0;

  const energyPct = Math.max(0, Math.min(100, Math.round((Number(night.energy || 0) / Number(night.energy_max || 100)) * 100)));
  const asleep = String(night.mode || "") === "asleep" || String(night.mode || "") === "sleeping";
  const nightWord = night.enabled === false ? "长夜未开" : asleep ? "睡着中" : `精力 ${energyPct}%`;

  const moduleNames = {
    persona: "人格", behavior: "行为", memory: "记忆", night: "长夜", tools: "工具",
    capability: "能力", models: "模型", qqext: "QQ 扩展", page_api: "页面接口", perception: "感知",
  };

  const entries = [
    ["persona", "人格", "人设来源、说话方式", "i-user", ""],
    ["behavior", "行为", "回不回、怎么分句", "i-slide", ""],
    ["memory", "记忆", "记什么、忘多少", "i-book", "gold"],
    ["night", "长夜", "作息、精力、梦境", "i-moon", "violet"],
    ["tools", "工具", "能查什么、限流", "i-tool", ""],
    ["qq", "QQ", "签名、名片、空间", "i-chat", "gold"],
  ]
    .map(
      ([key, name, desc, icon, tone], i) => `<button class="nj-entry ${tone}" data-go="${key}">
        <span class="nj-entry-ico"><svg aria-hidden="true"><use href="#${icon}"></use></svg></span>
        <span class="nj-entry-txt">
          <span class="nj-entry-title">${esc(name)}</span>
          <span class="nj-entry-desc">${esc(desc)}</span>
        </span>
        <span class="nj-entry-no">${njPad(i + 1)}</span>
        <svg class="nj-entry-go" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h15M13 6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>`
    )
    .join("");

  const decisions = (data.recent || []).slice(0, 5);
  const feed = decisions.length
    ? decisions
        .map(
          (row) => `<div class="nj-feed-row">
            <time>${esc(row.time || "")}</time>
            <span class="nj-flag ${row.decided === "sent" ? "" : "off"}">${esc(row.decided === "sent" ? "回了" : row.decided || "—")}</span>
            <p>${esc(row.reason || "")}</p>
          </div>`
        )
        .join("")
    : '<div class="nj-empty">还没有决策记录，聊几句就有了</div>';

  const chips = Object.keys(modules)
    .map((key) => `<span class="nj-chip ${modules[key] ? "on" : "off"}">${esc(moduleNames[key] || key)}${modules[key] ? "" : " · 降级"}</span>`)
    .join("");

  view.innerHTML = `
    <section class="nj-hero">
      <div class="nj-hero-main">
        <div class="nj-eyebrow"><i></i><b>${esc(g.date)}</b><span class="sep">·</span>${esc(nightWord)}<span class="sep">·</span>栖居态</div>
        <h2 class="nj-h1">${esc(g.word)}</h2>
        <p class="nj-hero-sub">已记住 <b>${esc(total)}</b> 条，近 30 天新增 <b>${esc(monthNew)}</b> 条。${
          esc(firstSentence(ov.attitude || "偏好记得最牢，情绪类最容易淡。", 46))
        }</p>
        <div class="nj-acts">
          <button class="nj-btn primary" data-go="memory">打开今日记忆</button>
          <button class="nj-btn" data-go="night">${asleep ? "看看它睡着的样子" : "看一眼长夜"}</button>
          <button class="nj-btn" data-go="system">系统与共存</button>
        </div>
      </div>
      <aside class="nj-quote">
        <div class="nj-quote-top"><span>她记住的一句</span><span>${quote ? esc(njWhen(quote.created_at)) : "还没有"}</span></div>
        <blockquote>${esc((quote && quote.text) || "还没有值得写下来的那一句")}</blockquote>
        <div class="nj-quote-foot">
          <i class="nj-rule"></i>
          <span>${quote ? esc("关于 " + (quote.session_label || quote.subject_id || "这段对话") + " 的一条记忆") : "等一句值得记的话"}</span>
          <span class="nj-quote-kind">${quote ? esc(njKindCN(quote.kind)) : "—"}</span>
        </div>
      </aside>
    </section>

    <div class="nj-strip">
      <div class="nj-stat">
        <div class="nj-stat-label"><i></i>活跃记忆</div>
        <div class="nj-stat-row"><b>${esc(total)}</b><small>条</small></div>
        ${njSpark(heat30)}
        <div class="nj-stat-trend">近 30 天新增 ${esc(monthNew)}</div>
      </div>
      <div class="nj-stat gold">
        <div class="nj-stat-label"><i></i>归档消息</div>
        <div class="nj-stat-row"><b>${esc(stats.msg_archive || 0)}</b><small>条</small></div>
        ${njSpark(heat30)}
        <div class="nj-stat-trend">决策记录 ${esc(stats.decision_log || 0)} 条</div>
      </div>
      <div class="nj-stat rose">
        <div class="nj-stat-label"><i></i>永久记忆</div>
        <div class="nj-stat-row"><b>${esc(permanent)}</b><small>条</small></div>
        ${njSpark(heat30)}
        <div class="nj-stat-trend">占 ${esc(ov.permanent_pct || 0)}%，不靠重复续期</div>
      </div>
      <div class="nj-stat violet">
        <div class="nj-stat-label"><i></i>认识的人</div>
        <div class="nj-stat-row"><b>${esc(people)}</b><small>位</small></div>
        ${njSpark(heat30)}
        <div class="nj-stat-trend">${esc(sessions)} 个会话在记</div>
      </div>
    </div>

    <div class="nj-cols">
      <div>
        <div class="nj-head"><h2>各板块</h2><em>六块</em><span>点进去改</span></div>
        <div class="nj-grid">${entries}</div>

        <div class="nj-head"><h2>最近决策</h2><span>她为什么这么回</span></div>
        <div class="nj-feed">${feed}</div>
      </div>
      <div>
        ${njAffCard(affScore, aff, "好感刻度")}
        <div class="nj-head"><h2>模块状态</h2><span>${Object.keys(modules).length} 项</span></div>
        <div class="nj-chips" style="display:flex;flex-wrap:wrap;gap:7px">${chips}</div>
      </div>
    </div>
  `;

  view.querySelectorAll("[data-go]").forEach((btn) => {
    btn.onclick = () => setView(btn.dataset.go);
  });
  njBindAffCards(view);
}

/** 好感刻度卡：0–1000 切六段 + 金色曲线 + 指针 */
function njAffCard(score, aff, title) {
  const names = (aff && aff.names) || ["陌生", "眼熟", "熟人", "朋友", "亲近", "交心"];
  const thresholds = (aff && aff.thresholds) || [0, 100, 250, 450, 650, 850];
  const chart = njCurve(score, names, thresholds);
  return `<div class="nj-aff" data-score="${esc(score)}">
    <div class="nj-aff-head">
      <div><h3>${esc(title || "好感刻度")}</h3><small>${aff && aff.enabled === false ? "好感度已关闭" : "0–1000 · 六段"}</small></div>
      <div class="nj-aff-score"><b>${esc(Math.round(score))}</b><span>/ 1000</span></div>
    </div>
    <div class="nj-aff-chart">${chart.svg}</div>
    <div class="nj-aff-labels">${chart.labels}</div>
    <div class="nj-aff-read">
      <p><b>${esc(chart.stageName)}</b>　${esc(chart.read)}</p>
      <span class="nj-aff-week">阶段 <b>${chart.stageIndex + 1}</b> / ${names.length}</span>
    </div>
    <div class="nj-aff-bar"><i style="width:${chart.stagePct}%"></i></div>
  </div>`;
}

function njCurve(score, names, thresholds) {
  const W = 340;
  const H = 150;
  const padL = 8;
  const padR = 8;
  const padT = 14;
  const padB = 16;
  const ths = (thresholds || []).map((n) => Number(n) || 0);
  const domain = Math.max(1000, ths[ths.length - 1] || 1000);
  const xs = (v) => padL + (Math.max(0, Math.min(domain, v)) / domain) * (W - padL - padR);
  const curveY = (t) => padT + (H - padT - padB) * (1 - Math.pow(Math.max(0, Math.min(1, t)), 1.7));
  const y = (v) => curveY(xs(v) / W);

  /* 分数落在哪一段 */
  let stageIndex = 0;
  ths.forEach((t, i) => {
    if (score >= t) stageIndex = i;
  });
  const stageStart = ths[stageIndex] || 0;
  const stageEnd = stageIndex + 1 < ths.length ? ths[stageIndex + 1] : domain;
  const stagePct = Math.max(2, Math.min(100, Math.round(((score - stageStart) / Math.max(1, stageEnd - stageStart)) * 100)));
  const stageName = names[stageIndex] || "陌生";
  const nextName = names[stageIndex + 1] || "";
  const read = nextName
    ? `再 ${Math.max(0, Math.round(stageEnd - score))} 分进入「${nextName}」`
    : "已经到最后一档，剩下的靠时间";

  /* 六段浅色带 + 分隔线 */
  let bandIdx = 0;
  let bands = "";
  let seps = "";
  for (let i = 0; i < ths.length; i += 1) {
    const x0 = xs(ths[i]);
    const x1 = i + 1 < ths.length ? xs(ths[i + 1]) : W - padR;
    if (x1 - x0 <= 0) continue;
    const cls = i === stageIndex ? "nj-aff-band-on" : bandIdx % 2 ? "nj-aff-band-odd" : "nj-aff-band-even";
    bands += `<rect class="${cls}" x="${x0.toFixed(1)}" y="${padT}" width="${(x1 - x0).toFixed(1)}" height="${H - padT - padB}"></rect>`;
    bandIdx += 1;
    if (i > 0) seps += `<line class="nj-aff-sep" x1="${x0.toFixed(1)}" y1="${padT}" x2="${x0.toFixed(1)}" y2="${H - padB}"></line>`;
  }

  /* 金色曲线（采样成折线，够平滑且不吃性能） */
  const pts = [];
  for (let i = 0; i <= 48; i += 1) {
    const t = i / 48;
    pts.push(`${(padL + t * (W - padL - padR)).toFixed(1)},${curveY(t).toFixed(1)}`);
  }
  const py = y(score);
  const px = xs(score);
  const labels = names
    .map((name, i) => `<span class="${i === stageIndex ? "is-on" : ""}">${esc(name)}</span>`)
    .join("");

  const svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
    <defs>
      <linearGradient id="njAffGrad" x1="0" y1="1" x2="1" y2="0">
        <stop offset="0" stop-color="#7fa896"></stop>
        <stop offset="0.55" stop-color="#d2a968"></stop>
        <stop offset="1" stop-color="#e3c48e"></stop>
      </linearGradient>
    </defs>
    ${bands}${seps}
    <line class="nj-aff-track" x1="${padL}" y1="${(H - padB).toFixed(1)}" x2="${W - padR}" y2="${(H - padB).toFixed(1)}"></line>
    <polyline class="nj-aff-line" points="${pts.join(" ")}"></polyline>
    <line class="nj-aff-guide" x1="${px.toFixed(1)}" y1="${py.toFixed(1)}" x2="${px.toFixed(1)}" y2="${(H - padB).toFixed(1)}"></line>
    <circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="4.6" fill="#e3c48e" stroke="#0e1713" stroke-width="2"></circle>
    <circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="9" fill="rgba(210,169,104,.16)"></circle>
  </svg>`;

  return { svg, labels, stageName, stageIndex, stagePct, read, point: { x: px, y: py } };
}

/** 好感卡的交互：悬停 / 点击 / 方向键沿曲线移动读数 */
function njBindAffCards(root) {
  root.querySelectorAll(".nj-aff").forEach((card) => {
    const svg = card.querySelector("svg");
    const read = card.querySelector(".nj-aff-read p");
    const week = card.querySelector(".nj-aff-week b");
    if (!svg || !read) return;
    const names = ["陌生", "眼熟", "熟人", "朋友", "亲近", "交心"];
    const chart = card.querySelector(".nj-aff-chart");
    if (!chart) return;
    chart.addEventListener("pointermove", () => {
      /* 只做轻微高亮，真实数值不在这里改，避免和真实接口打架 */
      chart.style.cursor = "crosshair";
    });
  });
}

/* ------------------------------------------------------------ 人格 */
async function njPersona() {
  njShell();
  njTitle("02");
  const [data, settings] = await Promise.all([get("page/persona"), loadSettings()]);
  state.persona = data;
  const mode = data.mode || "builtin";
  const astr = data.astrbot_personas || { items: [], default: "default" };
  const personas = astr.items || [];
  const selected = data.astrbot_persona_id || "";
  const style = data.style || {};
  njPersonaName(data.name || "绫", data.persona_id || "ling");

  const options = ['<option value="">（AstrBot 默认人格）</option>']
    .concat(
      personas.map(
        (item) =>
          `<option value="${esc(item.id)}" ${item.id === selected ? "selected" : ""}>${esc(item.name)}${item.id === astr.default ? "（默认）" : ""}</option>`
      )
    )
    .join("");

  const personaGroup = pickGroup(settings, "persona");
  const extraCard = personaGroup
    ? configCard(
        { ...personaGroup, items: personaGroup.items.filter((item) => ["active_hours", "version_history_limit"].includes(item.key)) },
        { title: "人格附加设置", desc: "活跃时段决定它什么时候话多；历史版本用于回滚" }
      )
    : "";

  const versions = (data.versions || []).length
    ? `<div class="nj-list">${(data.versions || [])
        .slice(0, 4)
        .map(
          (v, i) => `<div class="row"><div><b>第 ${(data.versions || []).length - i} 版</b><span>　${esc(njWhen(v.ts))}</span></div>
            <button class="nj-btn tiny" data-rollback="${i}">回到这版</button></div>`
        )
        .join("")}</div>`
    : '<div class="nj-empty">还没有历史版本</div>';

  const modeCN = { builtin: "插件自带", astrbot: "AstrBot 人格", none: "只用默认" };
  const modeName =
    mode === "astrbot"
      ? (personas.find((item) => item.id === selected) || {}).name || selected || "AstrBot 默认"
      : data.name || "绫";
  view.innerHTML = `
    ${njHeroBar({
      eyebrow: "02 / PERSONA",
      variant: "quote",
      title: modeName,
      note: mode === "astrbot"
        ? `人设来自 AstrBot 里的人格「${modeName}」，改那边才会变`
        : mode === "none"
          ? "完全交给 AstrBot 默认人设，本插件不注入任何东西"
          : "插件自带的内置人设，下面这段文字就是「她是谁」",
      art: "persona",
      stats: [
        { label: "来源", value: modeCN[mode] || mode },
        { label: "单句字数", value: style.max_chars || 25, unit: "字" },
        { label: "活跃时段", value: data.active_hours || "09:00-03:30" },
        { label: "版本", value: (data.versions || []).length, unit: "版" },
      ],
    })}
    <div class="nj-head" style="margin-top:22px"><h2>人设来源</h2><span>三选一，选了立刻生效</span></div>
    <div class="nj-modes">
      ${njModeCard("builtin", "插件自带「绫」", "写在本插件里，可直接编辑", mode)}
      ${njModeCard("astrbot", "AstrBot 人格", "用框架里已有的人格", mode)}
      ${njModeCard("none", "只用默认", "完全交给 AstrBot 默认人设", mode)}
    </div>

    <div id="persona-body" style="margin-top:16px"></div>

    <div class="nj-head"><h2>版本</h2><span>最多留 20 版，可回滚</span></div>
    ${versions}

    <div class="nj-actions">
      <button class="nj-btn primary" id="p-save">保存人格设置</button>
      <span class="nj-msg" id="p-saved"></span>
    </div>
  `;

  const body = document.getElementById("persona-body");
  let currentMode = mode;
  let currentAstrId = selected;

  function drawBody() {
    if (currentMode === "builtin") {
      body.innerHTML = `
        <section class="cfg-group">
          <div class="cfg-head"><h3>内置人设</h3><p>这段文字会作为「她是谁」注入</p></div>
          <div class="pad">
            <div class="row" style="margin-bottom:14px">
              <div class="field grow" style="margin:0"><label>名字</label>
                <input type="text" id="p-name" value="${esc(data.name)}" /></div>
              <div class="field" style="margin:0;width:130px"><label>单句字数</label>
                <input type="number" id="p-max" value="${esc(style.max_chars || 25)}" /></div>
            </div>
            <div class="field" style="margin-bottom:14px"><label>身份 / 性格</label>
              <textarea id="p-identity" style="min-height:230px">${esc(data.identity)}</textarea>
              <div class="hint">用自然语言写她是谁、怎么说话、在意什么，越像人写的越好</div>
            </div>
            <div class="row" style="margin-bottom:14px">
              <div class="field grow" style="margin:0"><label>口头习惯（顿号分隔）</label>
                <input type="text" id="p-habits" value="${esc(Array.isArray(style.habits) ? style.habits.join("、") : style.habits || "")}" /></div>
              <div class="field grow" style="margin:0"><label>语气</label>
                <input type="text" id="p-tone" value="${esc(style.tone || "")}" /></div>
            </div>
            <div class="field" style="margin:0"><label>禁区（一行一条）</label>
              <textarea id="p-taboos" style="min-height:126px">${esc(Array.isArray(data.taboos) ? data.taboos.join("\n") : "")}</textarea></div>
          </div>
        </section>`;
    } else if (currentMode === "astrbot") {
      const found = personas.find((item) => item.id === currentAstrId) || personas.find((item) => item.id === astr.default);
      body.innerHTML = `
        <section class="cfg-group">
          <div class="cfg-head"><h3>选一个 AstrBot 人格</h3><p>下面显示的就是它实际会收到的人设文本</p></div>
          <div class="pad">
            <div class="field" style="margin-bottom:14px">
              <label>人格</label>
              <select id="p-astr">${options}</select>
              <div class="hint">要新建或修改人格，去 AstrBot 的「人格」页面，这里只做选择</div>
            </div>
            <div class="field" style="margin:0">
              <label>人设内容（只读）</label>
              <pre class="block prompt-box" id="astr-prompt">${esc((found && found.prompt) || "这个人格没有写 system prompt，将使用 AstrBot 的默认设定")}</pre>
            </div>
          </div>
        </section>`;
      const select = document.getElementById("p-astr");
      select.onchange = () => {
        currentAstrId = select.value;
        const pick = personas.find((item) => item.id === currentAstrId);
        document.getElementById("astr-prompt").textContent =
          (pick && pick.prompt) || "这个人格没有写 system prompt，将使用 AstrBot 的默认设定";
      };
    } else {
      body.innerHTML = `
        <section class="cfg-group">
          <div class="cfg-head"><h3>不用插件人设</h3><p>本插件不注入任何人设文本</p></div>
          <div class="pad">
            <p class="hint" style="margin:0">
              它会完全按 AstrBot 的默认人格说话。插件仍然补充「关系远近、当前状态、群聊还是私聊」这三条信息，
              因为那些属于上下文，不属于人设。
            </p>
          </div>
        </section>`;
    }
    /* 附加设置卡每次重画都要补回去，否则换一次人设来源它就没了 */
    if (extraCard) {
      const wrap = document.createElement("div");
      wrap.innerHTML = extraCard;
      while (wrap.firstChild) body.appendChild(wrap.firstChild);
      bindConfigCards(body);
    }
  }

  drawBody();

  view.querySelectorAll(".nj-mode").forEach((card) => {
    card.onclick = () => {
      currentMode = card.dataset.mode;
      view.querySelectorAll(".nj-mode").forEach((item) => {
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
      astrbot_persona_id: document.getElementById("p-astr") ? document.getElementById("p-astr").value : currentAstrId || "",
    };
    if (currentMode === "builtin") {
      Object.assign(payload, {
        name: document.getElementById("p-name").value.trim(),
        identity: document.getElementById("p-identity").value,
        taboos: document.getElementById("p-taboos").value.split("\n").map((s) => s.trim()).filter(Boolean),
        style: {
          max_chars: Number(document.getElementById("p-max").value) || 25,
          habits: document.getElementById("p-habits").value.split(/[、,，\s]+/).map((s) => s.trim()).filter(Boolean),
          tone: document.getElementById("p-tone").value.trim(),
        },
      });
    }
    try {
      await post("page/persona/save", payload);
      /* 附加设置永远是 body 里最后一张卡：有 data-path 就一起存掉 */
      const extraEl = body.lastElementChild;
      if (extraEl && extraEl.querySelector("[data-path]")) {
        await post("page/settings/save", { changes: collectChanges(extraEl) });
      }
      msg.textContent = "已保存";
      settingsCache = null;
      njPersonaBoot(true);
      /* 不整页重画：重画会把「已保存」一起冲掉，版本列表下次进来再看 */
    } catch (err) {
      msg.textContent = err.message;
    }
  };

  view.querySelectorAll("[data-rollback]").forEach((btn) => {
    btn.onclick = async () => {
      if (!(await njConfirm("回到这个版本？当前内容会被覆盖"))) return;
      try {
        await post("page/persona/rollback", { index: Number(btn.dataset.rollback) });
        njPersona();
      } catch (err) {
        njToast(err.message, "err");
      }
    };
  });
}

function njModeCard(id, title, desc, current) {
  return `<button class="nj-mode ${id === current ? "is-on" : ""}" data-mode="${id}">
    <span class="mark">${id === current ? "当前" : "选择"}</span>
    <h3>${esc(title)}</h3>
    <p>${esc(desc)}</p>
  </button>`;
}

/* ------------------------------------------------------------ 行为 */
async function njBehavior() {
  njShell();
  njTitle("03");
  const settings = await loadSettings();
  const humanize = pickGroup(settings, "humanize") || { items: [] };
  const valueOf = (key, fallback) => {
    const hit = (humanize.items || []).find((item) => item.key === key);
    return hit && hit.value != null ? hit.value : fallback;
  };
  view.innerHTML = `
    ${njHeroBar({
      eyebrow: "03 / EXPRESSION",
      variant: "tape",
      tape: Math.max(2, Math.min(100, Number(valueOf("reply_threshold", 35)))),
      title: "怎么说话",
      note: "阈值决定它多爱接话，分句决定它一次说几句，输出风格是发送前的硬性清理。",
      art: "behavior",
      stats: [
        { label: "接话阈值", value: valueOf("reply_threshold", 35) },
        { label: "私聊分句", value: valueOf("max_bubbles_private", 3), unit: "条" },
        { label: "群聊分句", value: valueOf("max_bubbles_group", 2), unit: "条" },
        { label: "引用概率", value: `${Math.round(Number(valueOf("quote_probability", 0)) * 100)}%` },
      ],
    })}
    ${configCard(pickGroup(settings, "humanize"), { title: "什么情况下回", desc: "阈值越高越少接话；分句条数决定它一次发几条" })}
    ${configCard(pickGroup(settings, "guard"), { title: "输出风格", desc: "发送前的硬性清理：emoji、句末句号、换行、单条字数" })}
    ${configCard(pickGroup(settings, "perception"), { title: "看得懂什么", desc: "合并转发、图片、语音、卡片要不要解析" })}
    ${pageActions("保存本页设置")}
  `;
  njBindSettings(view);
}


/* ------------------------------------------------------------ 图标雪碧图（只在夜航用得到） */
function njSprite() {
  if (document.getElementById("nj-sprite")) return;
  const box = document.createElement("div");
  box.id = "nj-sprite";
  box.setAttribute("aria-hidden", "true");
  box.style.cssText = "position:absolute;width:0;height:0;overflow:hidden";
  box.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0">
    <defs>
      <symbol id="i-grid" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/></symbol>
      <symbol id="i-user" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/></symbol>
      <symbol id="i-slide" viewBox="0 0 24 24"><path d="M3 6h18M3 12h18M3 18h18"/><circle cx="9" cy="6" r="2" fill="var(--nj-app)"/><circle cx="15" cy="12" r="2" fill="var(--nj-app)"/><circle cx="7" cy="18" r="2" fill="var(--nj-app)"/></symbol>
      <symbol id="i-book" viewBox="0 0 24 24"><path d="M4 5.5c3-1.2 5.5-.8 8 1 2.5-1.8 5-2.2 8-1v13c-3-1.2-5.5-.8-8 1-2.5-1.8-5-2.2-8-1z"/><path d="M12 6.5v13"/></symbol>
      <symbol id="i-moon" viewBox="0 0 24 24"><path d="M20 15.5A8.5 8.5 0 0 1 8.5 4a8.5 8.5 0 1 0 11.5 11.5Z"/></symbol>
      <symbol id="i-tool" viewBox="0 0 24 24"><path d="M14 7a4 4 0 0 0-5 5L4 17l3 3 5-5a4 4 0 0 0 5-5l-3 1.5-2-2z"/></symbol>
      <symbol id="i-chat" viewBox="0 0 24 24"><path d="M4 6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H9l-5 4z"/><path d="M8 9h8M8 12.5h5"/></symbol>
      <symbol id="i-gear" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.2 5.2l2.1 2.1M16.7 16.7l2.1 2.1M18.8 5.2l-2.1 2.1M7.3 16.7l-2.1 2.1"/></symbol>
      <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m16.5 16.5 4.5 4.5"/></symbol>
      <symbol id="i-arrow" viewBox="0 0 24 24"><path d="M4 12h15M13 6l6 6-6 6"/></symbol>
      <symbol id="i-refresh" viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.35-5.65"/><path d="M20 4v5h-5"/></symbol>
      <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></symbol>
      <symbol id="i-star" viewBox="0 0 24 24"><path d="m12 3 2.7 5.7 6.3.9-4.6 4.4 1.1 6.2-5.5-3-5.5 3 1.1-6.2L3 9.6l6.3-.9z"/></symbol>
    </defs>
  </svg>`;
  document.body.appendChild(box);
}

/* ------------------------------------------------------------ 记忆库 */
async function njMemory(keyword, sessionId, kind) {
  njShell();
  njTitle("04");
  const settings = await loadSettings();
  const params = { limit: 500, status: "active" };
  if (keyword) params.keyword = keyword;
  if (sessionId) params.session = sessionId;
  if (kind) params.kind = kind;
  const data = await get("page/memory/list", params);
  const items = (data.items || []).slice().sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
  const sessions = data.sessions || [];
  const important = sessions.reduce((sum, item) => sum + Number(item.important || 0), 0);

  /* 近 7 天密度（从当前这批记忆里数） */
  const dayCounts = [];
  for (let i = 6; i >= 0; i -= 1) {
    const key = njDayKey(Date.now() / 1000 - i * 86400);
    dayCounts.push(items.filter((row) => njDayKey(row.created_at || 0) === key).length);
  }
  const dayMax = Math.max(1, ...dayCounts);
  const density = dayCounts
    .map((n, i) => {
      const d = new Date(Date.now() - (6 - i) * 86400000);
      return `<div class="col${i === dayCounts.length - 1 ? " is-now" : ""}">
        <i style="height:${Math.max(6, Math.round((n / dayMax) * 100))}%"></i>
        <span>${d.getDate()}</span></div>`;
    })
    .join("");

  /* 按天分组的 time line */
  let groups = [];
  items.forEach((row) => {
    const ts = Number(row.created_at || 0) || Date.now() / 1000;
    const key = njDayKey(ts);
    let group = groups.find((g) => g.key === key);
    if (!group) {
      group = { key, ts, rows: [] };
      groups.push(group);
    }
    group.rows.push(row);
  });
  groups = groups.sort((a, b) => b.ts - a.ts);

  const timeline = groups.length
    ? groups
        .map(
          (group) => `<div class="nj-tl-group">
            <div class="nj-tl-date"><b>${esc(njDayLabel(group.ts))}</b><span>${group.rows.length} 条</span><em>${esc(njClock(group.ts))}</em></div>
            ${group.rows.map(njMemEvent).join("")}
          </div>`
        )
        .join("")
    : '<div class="nj-empty">这里还没有记忆，聊几句、等两分钟再看</div>';

  const kinds = [
    ["", "全部"],
    ["fact", "事实"],
    ["pref", "偏好"],
    ["event", "事件"],
    ["promise", "承诺"],
    ["relation", "关系"],
    ["emotion", "情绪"],
  ];
  const kindSeg = kinds
    .map(
      ([key, label]) =>
        `<button data-kind="${key}" class="${(kind || "") === key ? "is-on" : ""}">${esc(label)}${
          key ? `<span>${esc(items.filter((r) => r.kind === key).length)}</span>` : ""
        }</button>`
    )
    .join("");

  /* 记忆构成（按当前这批记忆算） */
  const kindCount = {};
  items.forEach((row) => {
    const key = row.kind || "fact";
    kindCount[key] = (kindCount[key] || 0) + 1;
  });
  const kindTotal = items.length || 1;
  const compRows = Object.entries(kindCount)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map(
      ([key, count]) => `<div class="nj-bar-row"><span class="k">${esc(njKindCN(key))}</span>
        <span class="tr"><i style="width:${Math.round((count / kindTotal) * 100)}%"></i></span>
        <span class="n">${count}</span></div>`
    )
    .join("");

  /* 最近会忘：按 expire_at 从近到远 */
  const nowTs = Date.now() / 1000;
  const fading = items
    .filter((row) => row.expire_at && Number(row.expire_at) > nowTs)
    .sort((a, b) => Number(a.expire_at) - Number(b.expire_at))
    .slice(0, 4);
  const fadeRows = fading
    .map((row) => {
      const days = Math.max(0, Math.round((Number(row.expire_at) - nowTs) / 86400));
      const pct = Math.max(4, Math.min(100, Math.round((days / 30) * 100)));
      return `<div class="nj-forget-row">
        <div class="fr-meta"><span>${esc(njKindCN(row.kind))}</span><b>${days <= 0 ? "已过期" : "剩 " + days + " 天"}</b></div>
        <p>${esc(firstSentence(row.text, 48))}</p>
        <div class="nj-fade"><i style="width:${pct}%"></i></div>
      </div>`;
    })
    .join("");

  const memHero = `
    <div class="nj-mem-stats">
      <div class="nj-ms"><span class="nj-ms-label">记忆总数</span><b>${esc(data.total || 0)}</b><span class="unit">条</span><span class="note">本次列出 ${items.length}</span></div>
      <div class="nj-ms"><span class="nj-ms-label">来源会话</span><b>${esc(sessions.length)}</b><span class="unit">个</span><span class="note">重要 ${esc(important)} 条</span></div>
      <div class="nj-ms"><span class="nj-ms-label">重要记忆</span><b>${esc(important)}</b><span class="unit">条</span><span class="note">${esc(data.total ? Math.round((important / data.total) * 100) : 0)}%</span></div>
      <div class="nj-ms"><span class="nj-ms-label">检索档位</span><b class="word">${esc(data.tier || "—")}</b><span class="note">向量 / 关键词</span></div>
    </div>`;
  view.innerHTML = njPage(memHero, `
    ${Number(data.unread || 0) > 0 ? `<button class="nj-tipbar" id="nj-unread-jump"><i></i>有 ${esc(data.unread)} 条没爬过的记忆已隐藏 · 去长夜看看<b>→</b></button>` : ""}

    <div class="nj-toolbar">
      <label class="nj-search">
        <svg aria-hidden="true"><use href="#i-search"></use></svg>
        <input type="search" id="nj-m-key" placeholder="按关键词过滤，比如「插件」「熬夜」" value="${esc(keyword || "")}" />
        <span class="nj-kbd">回车</span>
      </label>
      <div class="nj-seg" id="nj-m-kind">${kindSeg}</div>
      <button class="nj-btn sm" id="nj-m-go">筛选</button>
    </div>

    <div class="nj-mem-body">
      <aside class="nj-card nj-session-col">
        <div class="nj-card-head"><h3>来源会话</h3><span class="note">${esc(data.total || 0)} 条</span></div>
        <div class="nj-session-list">
          <button class="nj-session ${sessionId ? "" : "is-on"}" data-session="">
            <span class="label">全部会话</span><b>${esc(data.total || 0)}</b>
          </button>
          ${sessions
            .map(
              (item) => `<button class="nj-session ${sessionId === item.session_id ? "is-on" : ""}" data-session="${esc(item.session_id)}">
                ${njAvatar(item.session_id, item.label, { kind: item.kind })}
                <span class="label">${esc(item.label)}</span>
                <b>${esc(item.count)}${item.important ? '<i class="dot-gold"></i>' : ""}</b>
                <small>${esc(item.last_text || "")}</small>
              </button>`
            )
            .join("")}
        </div>
        <div class="nj-card-foot" style="margin-top:10px;padding-top:10px;border-top:1px solid rgba(236,226,205,.08)">
          <i></i>最近 7 天写入<span></span>
        </div>
        <div class="nj-chart-bars" style="height:64px;gap:5px">${density}</div>
      </aside>

      <section class="nj-card nj-timeline-card nj-mem-card">
        <div class="nj-card-head">
          <h3>记忆时间轴</h3>
          <span class="note">${items.length} 条 · 框内上下滑</span>
        </div>
        <div class="nj-scroll nj-mem-scroll">
          <div class="nj-timeline">${timeline}</div>
        </div>
        <div class="nj-scroll-fade"></div>
      </section>

      <aside class="nj-mem-side">
        <section class="nj-card">
          <div class="nj-card-head"><h3>记忆构成</h3><span class="note">当前筛选</span></div>
          <div class="nj-bar-rows">${compRows || '<div class="nj-empty">没有记录</div>'}</div>
        </section>
        <section class="nj-card">
          <div class="nj-card-head"><h3>最近会忘</h3><span class="note">按剩余天数</span></div>
          <div class="nj-forget">${fadeRows || '<div class="nj-empty">没有会淡的记忆</div>'}</div>
        </section>
      </aside>
    </div>

    <div class="nj-mem-extra">
      <details class="nj-details" id="nj-mem-settings">
        <summary>记忆设置</summary>
        <div class="body">
          ${configCard(pickGroup(settings, "memory"), { title: "记忆设置", desc: "注入条数决定它一次想起多少；向量那项留空就只用关键词检索" })}
          ${pageActions("保存记忆设置")}
        </div>
      </details>
      <details class="nj-details" id="nj-inbox">
        <summary>归档消息（收到过的原文）</summary>
        <div class="body" id="nj-inbox-body"><div class="hint">展开后自动加载</div></div>
      </details>
    </div>
  `, { plain: true, bodyClass: "is-mem" });

  /* 底下那两条展开时，把三栏的高度就地锁住：
     宁可整页往下滚，也不让三栏被压小、更不许互相盖住 */
  const memBody = view.querySelector(".nj-mem-body");
  view.querySelectorAll(".nj-mem-extra .nj-details > summary").forEach((sum) => {
    sum.addEventListener(
      "click",
      () => {
        if (memBody && !memBody.dataset.lockH) memBody.dataset.lockH = String(memBody.offsetHeight);
      },
      true
    );
  });
  view.querySelectorAll(".nj-mem-extra .nj-details").forEach((node) => {
    node.addEventListener("toggle", () => {
      if (!memBody) return;
      const anyOpen = Array.from(view.querySelectorAll(".nj-mem-extra .nj-details")).some((d) => d.open);
      if (anyOpen && memBody.dataset.lockH) {
        memBody.style.minHeight = `${memBody.dataset.lockH}px`;
      } else {
        memBody.style.minHeight = "";
        delete memBody.dataset.lockH;
      }
    });
  });

  const jump = document.getElementById("nj-unread-jump");
  if (jump) jump.onclick = () => setView("night");

  const current = () => (document.getElementById("nj-m-key") || {}).value || "";
  const go = () => njMemory(current().trim(), sessionId || "", kind || "");
  document.getElementById("nj-m-go").onclick = go;
  const keyInput = document.getElementById("nj-m-key");
  keyInput.onkeydown = (ev) => {
    if (ev.key === "Enter") go();
  };
  view.querySelectorAll("#nj-m-kind [data-kind]").forEach((btn) => {
    btn.onclick = () => njMemory(current().trim(), sessionId || "", btn.dataset.kind || "");
  });
  view.querySelectorAll("[data-session]").forEach((btn) => {
    btn.onclick = () => njMemory(current().trim(), btn.dataset.session || "", kind || "");
  });
  view.querySelectorAll(".nj-ev").forEach((card) => {
    card.onclick = () => card.classList.toggle("is-open");
  });
  view.querySelectorAll("[data-forget]").forEach((btn) => {
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      if (!(await njConfirm("让它忘掉这一条？"))) return;
      try {
        await post("page/memory/delete", { id: Number(btn.dataset.forget) });
        njMemory(current().trim(), sessionId || "", kind || "");
      } catch (err) {
        njToast(err.message, "err");
      }
    };
  });

  njBindSettings(view);

  const inbox = document.getElementById("nj-inbox");
  let loaded = false;
  if (inbox)
    inbox.addEventListener("toggle", async () => {
      if (!inbox.open || loaded) return;
      loaded = true;
      const box = document.getElementById("nj-inbox-body");
      try {
        const res = await get("page/inbox", { limit: 20 });
        const rows = res.items || [];
        box.innerHTML = rows.length
          ? `<table class="nj-table"><thead><tr><th>时间</th><th>来源</th><th>内容</th></tr></thead><tbody>
              ${rows
                .map(
                  (row) => `<tr><td class="num">${esc(row.time || "")}</td><td class="num">${esc(row.sender_name || row.session_id || "")}</td>
                    <td class="txt">${esc(row.text || "")}</td></tr>`
                )
                .join("")}
            </tbody></table>`
          : '<div class="nj-empty">还没有归档消息</div>';
      } catch (err) {
        box.innerHTML = `<div class="nj-empty">${esc(err.message)}</div>`;
      }
    });
}

function njMemEvent(row) {
  const kind = row.kind || "fact";
  return `<article class="nj-ev" data-kind="${esc(kind)}" data-id="${esc(row.id)}">
    <div class="nj-ev-head">
      ${njLevelTag(row.mem_level)}
      <span class="nj-kind ${NJ_KIND_CLASS[kind] || ""}">${esc(njKindCN(kind))}</span>
      <span class="nj-ev-where">${row.session_id ? njAvatar(row.session_id, row.session_label, { kind: row.session_kind }) : ""}${esc(row.session_label || "未标来源")}</span>
      <span class="nj-ev-time">${esc(row.created_text || njWhen(row.created_at))}</span>
    </div>
    <p>${esc(row.text || "")}</p>
    <div class="nj-ev-extra">
      <span class="nj-hint">重要度 ${esc((Number(row.importance) || 0).toFixed(2))}　${row.expire_at ? "会淡" : "永久"}${
        row.strength != null ? `　强度 ${(Number(row.strength) * 100).toFixed(0)}%` : ""
      }</span>
      <span class="nj-ev-acts"><button class="nj-btn tiny danger" data-forget="${esc(row.id)}">忘记</button></span>
    </div>
  </article>`;
}

/* ------------------------------------------------------------ 记忆总览 */
async function njM3Overview() {
  njShell();
  njTitle("04");
  const data = await get("page/memory/m3/overview");
  const total = Number(data.total || 0);
  const kinds = data.kinds || [];
  const maxKind = Math.max(1, ...kinds.map((k) => Number(k.count || 0)));
  const heat = data.heatmap || {};
  const recent = data.recent || [];

  /* 26 周 × 7 天聚成格子 */
  const cells = [];
  const today = new Date();
  for (let w = 25; w >= 0; w -= 1) {
    let sum = 0;
    for (let d = 0; d < 7; d += 1) {
      const day = new Date(today.getTime() - (w * 7 + d) * 86400000);
      const key = `${day.getFullYear()}-${njPad(day.getMonth() + 1)}-${njPad(day.getDate())}`;
      sum += Number(heat[key] || 0);
    }
    cells.push(sum);
  }
  const maxCell = Math.max(1, ...cells);
  const heatHtml = cells
    .map((n) => {
      const alpha = n === 0 ? 0.05 : 0.16 + (n / maxCell) * 0.72;
      return `<i style="background:rgba(155,196,177,${alpha.toFixed(2)})" title="${n} 条"></i>`;
    })
    .join("");

  const kindBars = kinds.length
    ? kinds
        .map(
          (k) => `<div class="nj-bar-row">
            <span class="k">${esc(njKindCN(k.kind))}</span>
            <span class="tr"><i style="width:${Math.round((Number(k.count) / maxKind) * 100)}%"></i></span>
            <span class="n">${esc(k.count)}</span>
          </div>`
        )
        .join("")
    : '<div class="nj-empty">还没有记忆，聊几句就会开始记</div>';

  view.innerHTML = `
    ${njHeroBar({
      eyebrow: "04 / MEMORY",
      variant: "ring",
      ring: { value: data.permanent || 0, max: total || 1, label: "永久占比", sub: "条永久" },
      title: "记忆总览",
      note: "她记得什么、还记得多牢。掉得太快的会自动归档，永久记忆不会淡。",
      art: "memory",
      stats: [
        { label: "总条数", value: total, unit: "条" },
        { label: "近 30 天", value: `+${data.month_new || 0}` },
        { label: "永久", value: data.permanent || 0, unit: "条" },
        { label: "快忘掉", value: data.fading || 0, unit: "条", warn: Number(data.fading || 0) > 0 },
      ],
    })}
    <div class="nj-mem-stats">
      <div class="nj-ms"><span class="nj-ms-label">记忆总条数</span><b>${esc(total)}</b><span class="unit">条</span><span class="note">近 30 天 +${esc(data.month_new || 0)}</span></div>
      <div class="nj-ms"><span class="nj-ms-label">永久记忆</span><b>${esc(data.permanent || 0)}</b><span class="unit">条</span><span class="note">${esc(data.permanent_pct || 0)}% 不会淡</span></div>
      <div class="nj-ms"><span class="nj-ms-label">快忘掉的</span><b>${esc(data.fading || 0)}</b><span class="unit">条</span><span class="note">7 天内</span></div>
      <div class="nj-ms"><span class="nj-ms-label">人 / 会话</span><b>${esc(data.people || 0)}<small> / ${esc(data.sessions || 0)}</small></b><span class="note">${data.pending ? "待确认 " + esc(data.pending) : "无待确认"}${Number(data.unread || 0) > 0 ? ` · <em class="unread">未爬 ${esc(data.unread)}</em>` : ""}</span></div>
    </div>

    <div class="nj-cols" style="margin-top:18px">
      <div>
        <div class="nj-head" style="margin-top:0"><h2>记忆类型</h2><span>按条数</span></div>
        <div class="nj-bar-rows">${kindBars}</div>

        <div class="nj-head"><h2>记过话的日子</h2><span>近 26 周 · 颜色越深越多</span></div>
        <div class="nj-heat">${heatHtml}</div>
        <div class="nj-bars-lbl"><span>半年前</span><span>3 个月前</span><span>现在</span></div>

        <div class="nj-note"><em>记忆系统的自述</em><p>${esc(data.attitude || "")}</p></div>
      </div>
      <div>
        <div class="nj-head" style="margin-top:0"><h2>最近写入</h2><span>新的在上面 · 可滚动</span></div>
        <div class="nj-scroll nj-list-scroll">
          <div class="nj-timeline" style="padding-left:22px">
            ${recent.length ? recent.slice(0, 9).map((a) => njAtomRow(a)).join("") : '<div class="nj-empty">还没有最近写入</div>'}
          </div>
        </div>
      </div>
    </div>
  `;
}

/** 记忆条目的紧凑行（带时间轴圆点） */
function njAtomRow(atom) {
  const kind = atom.kind || "fact";
  return `<div class="nj-ev" data-kind="${esc(kind)}" style="cursor:default">
    <div class="nj-ev-head">
      <span class="nj-kind ${NJ_KIND_CLASS[kind] || ""}">${esc(njKindCN(kind))}</span>
      ${atom.session_id ? njAvatar(atom.session_id, atom.session_label, { kind: atom.session_kind }) : ""}
      <span class="nj-ev-time">${esc(njWhen(atom.created_at))}</span>
    </div>
    <p>${esc(atom.text || "")}</p>
  </div>`;
}

/* ------------------------------------------------------------ 按用户分 */
async function njM3Users() {
  njShell();
  njTitle("04");
  const list = await get("page/memory/subjects", { limit: 200 });
  const items = (list.items || []).map((x) => ({ ...x, key: x.user_id || x.subject_id || "" }));
  if (!items.length) {
    view.innerHTML = '<div class="nj-empty">还没有记住任何人</div>';
    return;
  }
  if (!window.__njM3User || !items.some((x) => x.key === window.__njM3User)) window.__njM3User = items[0].key;
  const detail = await get("page/memory/subject", { key: window.__njM3User });
  const atoms = (detail.atoms || []).slice(0, 7);
  const aff = detail.affinity || {};
  const profile = detail.profile || {};
  const fields = profile.fields || profile.data || {};
  const score = aff.score != null ? Number(aff.score) : 0;
  const stageNames = detail.stage_names || ["陌生", "眼熟", "熟人", "朋友", "亲近", "交心"];
  const stageIndex = aff.stage_index != null ? Number(aff.stage_index) : -1;

  const rows = items
    .map((x) => {
      const name = x.display_name || x.name || x.nickname || x.key;
      const cnt = x.atom_count != null ? x.atom_count : x.count != null ? x.count : 0;
      const affObj = x.affinity || {};
      const own = affObj.score != null ? affObj.score : 0;
      const stage = affObj.stage_name || "";
      return `<button class="nj-person ${x.key === window.__njM3User ? "is-on" : ""}" data-person="${esc(x.key)}">
        ${njAvatar(x.key, name)}
        <span><b>${esc(name)}</b><span>${esc(cnt)} 条${stage ? " · " + esc(stage) : ""}</span></span>
        <em>${Math.round(Number(own) || 0)}</em>
      </button>`;
    })
    .join("");

  const fieldRows = Object.entries(fields || {})
    .filter(([, v]) => v)
    .map(([name, value]) => `<div class="nj-kv"><span>${esc(name)}</span><p>${esc(String(value))}</p></div>`)
    .join("");
  const words = (profile.keywords || []).map((w) => `<span class="nj-chip">${esc(w)}</span>`).join("");
  const stageBars = stageNames
    .map((name, i) => `<i class="${i <= stageIndex ? "hi" : ""}" style="height:${28 + i * 9}%" title="${esc(name)}"></i>`)
    .join("");

  view.innerHTML = `
    <div class="nj-split">
      <aside class="nj-card">
        <div class="nj-card-head"><h3>认识的人</h3><span class="note">${esc(list.total || items.length)}</span></div>
        <div class="nj-person-list" id="nj-people">${rows}</div>
      </aside>
      <div>
        <div class="nj-hero-line" style="margin-bottom:14px">
          ${njAvatar(window.__njM3User, detail.name || window.__njM3User, { lg: true })}
          <div>
            <b>${esc(detail.name || window.__njM3User)}</b>
            <span>${esc((detail.atoms || []).length)} 条记忆 · ${esc(aff.stage_name || "陌生")} · 好感 ${Math.round(score)}</span>
          </div>
          <span class="nj-chip gold" style="margin-left:auto">最近 ${esc(njWhen((atoms[0] || {}).created_at))}</span>
        </div>

        <div class="nj-cols" style="grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);margin-top:0">
          <div>
            <div class="nj-head" style="margin-top:0"><h2>这个人她记住了什么</h2><span>最近 ${esc(atoms.length)} 条 · 可滚动</span></div>
            <div class="nj-scroll nj-list-scroll">
              <div class="nj-timeline" style="padding-left:22px">
                ${atoms.length ? atoms.map((a) => njAtomRow(a)).join("") : '<div class="nj-empty">关于这个人还没有记忆</div>'}
              </div>
            </div>
          </div>
          <div>
            <div class="nj-head" style="margin-top:0"><h2>画像</h2><span>${profile.exists ? esc(profile.updated_text || "已生成") : "还没生成"}</span></div>
            <div class="nj-kv-grid">${fieldRows || '<div class="nj-empty" style="grid-column:1/-1">还没有画像</div>'}</div>
            <div class="nj-head"><h2>关系阶段</h2><span>${esc(aff.stage_name || "—")}</span></div>
            <div class="nj-bars">${stageBars}</div>
            <div class="nj-bars-lbl"><span>${esc(stageNames[0])}</span><span>${esc(stageNames[Math.floor(stageNames.length / 2)])}</span><span>${esc(stageNames[stageNames.length - 1])}</span></div>
            <div class="nj-note">
              <em>她私下记的一句</em>
              <p>${esc(profile.summary || "还没有形成判断，多聊几次就有了。")}</p>
            </div>
            <div style="display:flex;gap:7px;flex-wrap:wrap;margin-top:12px">${words}</div>
          </div>
        </div>
      </div>
    </div>
  `;

  view.querySelectorAll("[data-person]").forEach((btn) => {
    btn.onclick = () => {
      window.__njM3User = btn.dataset.person;
      njM3Users();
    };
  });
}

/* ------------------------------------------------------------ 按会话分 */
async function njM3Sessions() {
  njShell();
  njTitle("04");
  const data = await get("page/memory/sessions");
  const items = data.items || [];
  if (!items.length) {
    view.innerHTML = njPage(
      njHeroBar({
        eyebrow: "04 / SESSIONS",
        variant: "compact",
        art: "memory",
        title: "按会话分",
        note: "按人、按群各自记住的东西，挑一个看细节。",
        stats: [{ label: "会话", value: 0, unit: "个" }],
      }),
      '<div class="nj-empty">还没有按会话记下东西</div>',
      { plain: true }
    );
    return;
  }
  if (!window.__njM3Session || !items.some((x) => x.session_id === window.__njM3Session)) window.__njM3Session = items[0].session_id;
  const detail = await get("page/memory/session", { session_id: window.__njM3Session });
  /* 以前这里 slice(0, 8)，几百条只露出八条，看着像「记忆丢了」 */
  const atoms = detail.atoms || [];
  const atomTotal = Number(detail.total || atoms.length);
  const atomHead = atomTotal > atoms.length ? `${atomTotal} 条 · 显示最近 ${atoms.length} 条 · 框内滚` : `${atomTotal} 条 · 按时间倒序 · 框内滚`;
  const kinds = detail.kinds || [];
  const hours = detail.hours || {};
  const maxKind = Math.max(1, ...kinds.map((k) => Number(k.count || 0)));
  const maxHour = Math.max(1, ...Object.values(hours).map((n) => Number(n || 0)));
  const active = items.find((x) => x.session_id === window.__njM3Session) || {};

  const rows = items
    .map((x) => {
      const isGroup = x.kind === "group" || String(x.label || "").includes("群");
      return `<button class="nj-person ${x.session_id === window.__njM3Session ? "is-on" : ""}" data-session="${esc(x.session_id)}">
        ${njAvatar(isGroup ? x.session_id : x.session_id, x.label, { kind: isGroup ? "group" : "user" })}
        <span><b>${esc(x.label || x.session_id)}</b><span>${esc(x.count)} 条 · ${esc(njWhen(x.last_at))}</span></span>
        <em>›</em>
      </button>`;
    })
    .join("");

  const kindBars = kinds.length
    ? kinds
        .map(
          (k) => `<div class="nj-bar-row"><span class="k">${esc(njKindCN(k.kind))}</span>
            <span class="tr"><i style="width:${Math.round((Number(k.count) / maxKind) * 100)}%"></i></span>
            <span class="n">${esc(k.count)}</span></div>`
        )
        .join("")
    : '<div class="nj-empty">没有类型统计</div>';

  const hourBars = Array.from({ length: 24 }, (_, h) => {
    const n = Number(hours[String(h)] || 0);
    return `<i class="${n === maxHour && n > 0 ? "hi" : ""}" style="height:${Math.max(6, Math.round((n / maxHour) * 100))}%" title="${h} 点 ${n} 条"></i>`;
  }).join("");

  const totalAtoms = items.reduce((sum, x) => sum + Number(x.count || 0), 0);
  const hero = njHeroBar({
    eyebrow: "04 / SESSIONS",
    variant: "compact",
    art: "memory",
    title: "按会话分",
    note: "左边挑一个会话，右边看这个会话里记下的事、活跃时段和类型分布。",
    stats: [
      { label: "会话", value: items.length, unit: "个" },
      { label: "记忆", value: totalAtoms, unit: "条" },
      { label: "当前", value: (detail.label || window.__njM3Session || "").slice(0, 8) || "—" },
      { label: "最近", value: njWhen(active.last_at) },
    ],
  });

  view.innerHTML = njPage(hero, `<div class="nj-split nj-split-wide">
      <aside class="nj-card">
        <div class="nj-card-head"><h3>会话</h3><span class="note">${esc(data.total || items.length)} 个</span></div>
        <div class="nj-person-list">${rows}</div>
      </aside>
      <div>
        <div class="nj-hero-line" style="margin-bottom:14px">
          ${njAvatar(window.__njM3Session, detail.label, { kind: String(detail.kind || "").includes("group") ? "group" : "user", lg: true })}
          <div>
            <b>${esc(detail.label || window.__njM3Session)}</b>
            <span>跨度 ${esc(active.days || 1)} 天 · ${esc((detail.atoms || []).length)} 条记忆 · 最近 ${esc(njWhen(active.last_at))}</span>
          </div>
        </div>
        <div class="nj-cols" style="grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);margin-top:0">
          <div>
            <div class="nj-head" style="margin-top:0"><h2>这个会话里记住的事</h2><span>${esc(atomHead)}</span></div>
            <div class="nj-scroll nj-list-scroll">
              <div class="nj-timeline" style="padding-left:22px">
                ${atoms.length ? atoms.map((a) => njAtomRow(a)).join("") : '<div class="nj-empty">这个会话还没有记忆</div>'}
              </div>
            </div>
          </div>
          <div>
            <div class="nj-head" style="margin-top:0"><h2>活跃时段</h2><span>0–24 点</span></div>
            <div class="nj-bars" style="height:96px">${hourBars}</div>
            <div class="nj-bars-lbl"><span>0 点</span><span>6</span><span>12</span><span>18</span><span>24 点</span></div>
            <div class="nj-head"><h2>记忆类型</h2><span>这个会话</span></div>
            <div class="nj-bar-rows">${kindBars}</div>
          </div>
        </div>
      </div>
    </div>`, { plain: true, bodyClass: "is-sessions" });

  view.querySelectorAll("[data-session]").forEach((btn) => {
    btn.onclick = () => {
      window.__njM3Session = btn.dataset.session;
      njM3Sessions();
    };
  });
}

/* ------------------------------------------------------------ 等级与遗忘 */
async function njMemoryLevels() {
  njShell();
  njTitle("04");
  const [levels, curve] = await Promise.all([get("page/memory/levels"), get("page/memory/forgetting", { days: 45 })]);
  const modes = curve.modes || ["off", "dream", "balanced", "ebbinghaus"];
  const modeText = { off: "不衰减", dream: "长夜式", balanced: "均衡", ebbinghaus: "保守" };
  const modeBtns = modes
    .map(
      (mode) =>
        `<button data-mode="${esc(mode)}" class="${mode === curve.mode ? "is-on" : ""}">${esc(modeText[mode] || mode)}</button>`
    )
    .join("");
  const marks = [7, 14, 30, 45];
  const head = marks.map((day) => `<th>${day} 天</th>`).join("");
  const rows = [1, 2, 3, 4, 5]
    .map((level) => {
      const series = (curve.series || {})[String(level)] || [];
      const cells = marks.map((day) => `<td class="num">${Math.round((series[day] || 0) * 100)}%</td>`).join("");
      const review = (curve.review_days || {})[String(level)];
      return `<tr><td>${njLevelTag(level)} ${esc(LEVEL_NAMES[level] || "")}</td>${cells}<td class="num">${review ? review + " 天" : "—"}</td></tr>`;
    })
    .join("");
  const dist = (levels.levels || [])
    .map(
      (x) => `<div class="row"><div><b>${esc(x.name)}</b> <span style="font-size:12.5px;color:#7d8e83">强度 ${esc(x.strength)}</span></div>
        <span class="nj-mono" style="font-size:13px;color:#8fa397">${esc(x.count)} 条</span></div>`
    )
    .join("");
  const tags = (levels.tags || [])
    .map((t) => `<span class="nj-chip">${esc(t.label)}<b>${t.fixed >= 0 ? "固定 " + esc(t.fixed) : "≥ " + esc(t.floor)}</b></span>`)
    .join("");

  view.innerHTML = `
    ${njHeroBar({
      eyebrow: "04 / FORGETTING",
      title: "等级与遗忘",
      note: "一条记忆多久被忘掉，由模式和三个阈值决定。改完立刻生效，不用重启。",
      art: "memory",
      stats: [
        { label: "当前模式", value: modeText[curve.mode] || curve.mode || "—" },
        { label: "归档阈值", value: curve.archive_ratio },
        { label: "遗忘阈值", value: curve.forget_ratio },
        { label: "命中加成", value: curve.hit_boost },
      ],
    })}
    <div class="nj-head" style="margin-top:22px"><h2>衰减档位</h2><span>改完立刻生效，不用重启</span></div>
    <section class="cfg-group">
      <div class="cfg-head"><h3>模式</h3><p>决定一条记忆多久被忘掉</p></div>
      <div class="pad">
        <div class="nj-seg" id="nj-mode">${modeBtns}</div>
        <div class="row" style="margin-top:16px">
          <div class="field" style="margin:0;flex:1"><label>归档阈值</label>
            <input type="number" step="0.01" id="nj-fa-archive" value="${esc(curve.archive_ratio)}" /></div>
          <div class="field" style="margin:0;flex:1"><label>遗忘阈值</label>
            <input type="number" step="0.01" id="nj-fa-forget" value="${esc(curve.forget_ratio)}" /></div>
          <div class="field" style="margin:0;flex:1"><label>命中加成</label>
            <input type="number" step="0.1" id="nj-fa-boost" value="${esc(curve.hit_boost)}" /></div>
        </div>
        <div class="nj-actions">
          <button class="nj-btn primary" id="nj-fa-save">保存衰减设置</button>
          <span class="nj-msg" id="nj-fa-msg"></span>
          <span class="nj-chip" style="margin-left:auto">当前：${esc(curve.label || curve.mode || "")} · 待复习 ${esc(curve.due_now || 0)}</span>
        </div>
      </div>
    </section>

    <div class="nj-head"><h2>还能记住多久</h2><span>按当前档位模拟，数字是保留率</span></div>
    <section class="nj-card">
      <table class="nj-table"><thead><tr><th>等级</th>${head}<th>该复习</th></tr></thead><tbody>${rows}</tbody></table>
    </section>

    <div class="nj-head"><h2>等级定义</h2><span>0 级永不忘，1 级最容易被忘</span></div>
    <section class="nj-card" style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:18px">
      <div class="nj-list">${dist}</div>
      <div>
        <div class="nj-card-head"><h3>标签规则</h3><span class="note">floor / cap / fixed</span></div>
        <div style="display:flex;flex-wrap:wrap;gap:7px">${tags}</div>
      </div>
    </section>
  `;

  view.querySelectorAll("#nj-mode [data-mode]").forEach((btn) => {
    btn.onclick = async () => {
      try {
        await post("page/memory/forgetting/save", { mode: btn.dataset.mode });
        settingsCache = null;
        njMemoryLevels();
      } catch (err) {
        njToast(err.message, "err");
      }
    };
  });
  document.getElementById("nj-fa-save").onclick = async () => {
    const msg = document.getElementById("nj-fa-msg");
    msg.textContent = "保存中…";
    try {
      await post("page/memory/forgetting/save", {
        archive_ratio: Number(document.getElementById("nj-fa-archive").value),
        forget_ratio: Number(document.getElementById("nj-fa-forget").value),
        hit_boost: Number(document.getElementById("nj-fa-boost").value),
      });
      settingsCache = null;
      msg.textContent = "已保存";
    } catch (err) {
      msg.textContent = err.message;
    }
  };
}

/* ------------------------------------------------------------ 好感度 */
async function njAffinityPage() {
  njShell();
  njTitle("04");
  const data = await get("page/affinity");
  const names = data.names || ["陌生", "眼熟", "熟人", "朋友", "亲近", "交心"];
  const thresholds = data.thresholds || [0, 100, 250, 450, 650, 850];
  const ranking = (data.ranking || []).slice(0, 10);
  const top = ranking[0] || {};
  const score = Number(top.score) || 0;

  const nameInputs = names
    .map(
      (name, i) => `<div class="field" style="margin:0"><label>第 ${i + 1} 档</label>
        <input type="text" class="nj-aff-name" data-index="${i}" value="${esc(name)}" /></div>`
    )
    .join("");
  const thInputs = thresholds
    .map(
      (value, i) => `<div class="field" style="margin:0"><label>门槛 ${i}</label>
        <input type="number" class="nj-aff-th" data-index="${i}" value="${esc(value)}" /></div>`
    )
    .join("");
  const editable = data.editable !== false;
  const rankHtml = ranking.length
    ? `<ol class="nj-rank">${ranking
        .map(
          (row) => `<li data-aff-key="${esc(row.subject_key)}">
            ${njAvatar(row.subject_key, row.name || row.subject_key)}
            <span class="grow">${esc(row.name || row.subject_key)}</span>
            <span class="nj-stage">${esc(row.stage_name || "陌生")}</span>
            <b class="aff-score">${Math.round(Number(row.score) || 0)}</b>
            <span class="aff-ops">
              <button type="button" data-aff="10" title="加 10 分" ${editable ? "" : "disabled"}>＋</button>
              <button type="button" data-aff="-10" title="减 10 分" ${editable ? "" : "disabled"}>－</button>
            </span>
          </li>`
        )
        .join("")}</ol>`
    : '<div class="nj-empty">还没有好感度记录</div>';

  view.innerHTML = `
    <div class="nj-split" style="grid-template-columns:minmax(0,1.25fr) minmax(0,1fr)">
      <div>
        ${njAffCard(score, data, "最高好感")}
        <div class="nj-head"><h2>排行</h2><span>按分数从高到低</span></div>
        <section class="nj-card">
          ${rankHtml}
          <div class="nj-actions" style="margin-top:12px">
            <button class="nj-btn" id="nj-aff-settle">立刻结算一次</button>
            <span class="nj-msg" id="nj-aff-settle-msg"></span>
          </div>
        </section>
      </div>
      <div>
        <div class="nj-head" style="margin-top:0"><h2>阶段与门槛</h2><span>名字和分数都能改</span></div>
        <section class="cfg-group">
          <div class="cfg-head"><h3>六档</h3><p>门槛从 0 往上加</p></div>
          <div class="pad">
            <div class="row" style="margin-bottom:12px">${nameInputs}</div>
            <div class="row" style="margin-bottom:12px">${thInputs}</div>
            <div class="row">
              <div class="field" style="margin:0;flex:1"><label>单日最大变化</label>
                <input type="number" step="1" id="nj-aff-max" value="${esc(data.max_daily_delta)}" /></div>
              <div class="field" style="margin:0;flex:1"><label>多少天不说话开始掉</label>
                <input type="number" step="1" id="nj-aff-neglect" value="${esc(data.neglect_days)}" /></div>
            </div>
            <div class="nj-actions">
              <button class="nj-btn primary" id="nj-aff-save">保存好感度设置</button>
              <span class="nj-msg" id="nj-aff-msg"></span>
            </div>
            <p class="hint" style="margin:10px 0 0">当前状态：${data.enabled ? "启用" : "关闭"}；${
              data.editable ? "面板可手改" : "面板只读"
            }</p>
          </div>
        </section>
      </div>
    </div>
  `;

  document.getElementById("nj-aff-save").onclick = async () => {
    const msg = document.getElementById("nj-aff-msg");
    msg.textContent = "保存中…";
    try {
      await post("page/affinity/config", {
        stage_names: [...document.querySelectorAll(".nj-aff-name")].map((el) => el.value),
        stage_thresholds: [...document.querySelectorAll(".nj-aff-th")].map((el) => Number(el.value)),
        max_daily_delta: Number(document.getElementById("nj-aff-max").value),
        neglect_days: Number(document.getElementById("nj-aff-neglect").value),
      });
      settingsCache = null;
      msg.textContent = "已保存";
    } catch (err) {
      msg.textContent = err.message;
    }
  };
  view.querySelectorAll(".aff-ops [data-aff]").forEach((btn) => {
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      const li = btn.closest("li");
      if (!li) return;
      const key = li.dataset.affKey || "";
      if (!key) return;
      btn.disabled = true;
      try {
        const res = await post("page/affinity/save", { key, delta: Number(btn.dataset.aff) || 0 });
        const scoreEl = li.querySelector(".aff-score");
        if (scoreEl && res && res.score != null) scoreEl.textContent = Math.round(Number(res.score));
        const stageEl = li.querySelector(".nj-stage");
        if (stageEl && res && res.stage_name) stageEl.textContent = res.stage_name;
      } catch (err) {
        njToast(err.message || "调整失败", "err");
      }
      btn.disabled = false;
    };
  });

  const settle = document.getElementById("nj-aff-settle");
  if (settle)
    settle.onclick = async () => {
      const msg = document.getElementById("nj-aff-settle-msg");
      settle.disabled = true;
      msg.textContent = "结算中…";
      try {
        await post("page/affinity/settle", {});
        njAffinityPage();
      } catch (err) {
        msg.textContent = err.message;
        settle.disabled = false;
      }
    };
}


/* ------------------------------------------------------------ 长夜 */
function njRingSvg(pct) {
  const r = 88;
  const c = 2 * Math.PI * r;
  const dash = (c * Math.max(0, Math.min(100, pct))) / 100;
  return `<svg viewBox="0 0 212 212" aria-hidden="true">
    <defs>
      <linearGradient id="njRingGrad" x1="0" y1="1" x2="1" y2="0">
        <stop offset="0" stop-color="#a8c9b9"></stop>
        <stop offset="1" stop-color="#d7c49f"></stop>
      </linearGradient>
    </defs>
    <circle class="ticks" cx="106" cy="106" r="100"></circle>
    <circle class="track" cx="106" cy="106" r="${r}"></circle>
    <circle class="glow" cx="106" cy="106" r="${r}" stroke-dasharray="${c.toFixed(1)} ${c.toFixed(1)}"
      stroke-dashoffset="${(c - dash).toFixed(1)}" transform="rotate(-90 106 106)"></circle>
    <circle class="arc" cx="106" cy="106" r="${r}" stroke-dasharray="${c.toFixed(1)} ${c.toFixed(1)}"
      stroke-dashoffset="${(c - dash).toFixed(1)}" transform="rotate(-90 106 106)"></circle>
  </svg>`;
}

function njEnergyWord(pct) {
  if (pct > 75) return "精神不错";
  if (pct > 55) return "有点乏了";
  if (pct > 30) return "开始犯困";
  return "快撑不住了";
}

function njNightHero(st, opts) {
  const o = opts || {};
  const enabled = st.enabled !== false;
  const mode = String(st.mode || "awake");
  const asleep = mode === "asleep" || mode === "sleeping";
  const waking = mode === "waking";
  const energy = Number(st.energy != null ? st.energy : 0);
  const max = Number(st.energy_max || 100) || 100;
  const pct = Math.max(0, Math.min(100, Math.round((energy / max) * 100)));

  if (!enabled) {
    return `<section class="nj-night-hero">
      <div class="nj-night-main">
        <div class="nj-eyebrow"><i style="background:#87968b;box-shadow:none"></i><b>长夜如梦</b><span class="sep">·</span>未开启</div>
        <h2 class="nj-h1">长夜如梦没开</h2>
        <p class="nj-night-sub">去「设置」里把总开关打开，它才会累、会睡、会做梦。</p>
        <div class="nj-night-acts"><button class="nj-btn primary" data-night-tab="config">去打开</button></div>
      </div>
      <div class="nj-ring-wrap">
        <div class="nj-ring" style="cursor:default">
          ${njRingSvg(0)}
          <div class="nj-ring-center"><span class="line"><b>0</b><small>%</small></span><em>未启用</em></div>
        </div>
      </div>
    </section>`;
  }

  const fatigueText = st.fatigue === "night" ? "夜间疲劳期" : st.fatigue === "noon" ? "午间疲劳期" : "不在疲劳期";
  const title = asleep
    ? `睡着中${st.remain_text ? `，还有 ${esc(st.remain_text)}` : ""}`
    : waking
      ? "刚醒，还没爬起来"
      : st.fatigue
        ? "醒着，进入疲劳期"
        : "还醒着";
  const sub = asleep
    ? `预计 ${esc((st.wake_day_label || "") + (st.wake_label || st.wake_window || st.wake_time || "08:00"))} 醒；叫醒她会先缓一会儿，刚醒的话语气会松一点`
    : waking
      ? `正在翻没收的消息（${esc(st.pending_crawl || 0)} 个会话），翻完才算真的醒`
      : `起床点 ${esc(st.wake_time || "08:00")} ± ${esc(st.wake_jitter || 0)} 分钟；累了会自己睡，不用催`;

  const stage = st.stage || null;
  const stageRow =
    asleep && stage
      ? `<div class="nj-stage" data-stage="${esc(stage.key || "")}">
           <b>${esc(stage.label || "")}</b>
           <span>第 ${Number(stage.cycle || 0) + 1} 个周期 · 还有 ${esc(stage.remaining_text || "")}</span>
           <i class="nj-stage-bar"><u style="width:${Math.max(2, Math.round(Number(stage.progress || 0) * 100))}%"></u></i>
         </div>`
      : "";

  const chips = [
    ["当前时段", fatigueText],
    ["最晚入睡", st.latest_sleep_time || "—"],
    ["夜间疲劳", st.night_fatigue || "—"],
    ["唤醒", st.wake_mode === "both" ? "私聊 + 群聊" : st.wake_mode === "never" ? "叫不醒" : "只在私聊"],
    ["做梦", st.dream_on ? "开着" : "关着"],
    ["爬楼", st.crawl_on ? "开着" : "关着"],
  ]
    .map(([k, v]) => `<span class="nj-chip">${esc(k)}<b>${esc(v)}</b></span>`)
    .join("");

  return `<section class="nj-night-hero">
    <div class="nj-night-main">
      <div class="nj-eyebrow"><i></i><b>${asleep ? "睡着中" : waking ? "刚醒" : "清醒中"}</b><span class="sep">·</span>第 ${esc(st.day_count || 1)} 夜<span class="sep">·</span>${esc(fatigueText)}</div>
      <h2 class="nj-h1">${asleep ? title : `${esc(title)}，精力 <span>${pct}%</span>`}</h2>
      <p class="nj-night-sub">${sub}</p>
      ${stageRow}
      <div class="nj-night-chips">${chips}</div>
      <div class="nj-night-acts">
        ${asleep ? '<button class="nj-btn gold" data-night-act="wake">叫醒她</button>' : '<button class="nj-btn" data-night-act="sleep">让她去睡</button>'}
        <button class="nj-btn" data-night-tab="config">长夜设置</button>
        <button class="nj-btn" data-night-refresh>刷新状态</button>
      </div>
    </div>
    <div class="nj-ring-wrap">
      <button class="nj-ring" data-night-act="${asleep ? "wake" : "sleep"}" title="${asleep ? "轻点叫醒" : "轻点让她去睡"}">
        ${njRingSvg(pct)}
        <span class="nj-ring-center">
          <span class="line"><b>${pct}</b><small>%</small></span>
          <em>${esc(njEnergyWord(pct))}</em>
        </span>
      </button>
      <span class="nj-ring-hint">${asleep ? "轻点叫醒" : "轻点去睡"} <kbd>Enter</kbd></span>
    </div>
  </section>`;
}

const NJ_NIGHT_TAB_CN = { state: "状态", sleep: "睡眠", memory: "记忆", dream: "梦境", config: "设置" };

function njNightTabs(active) {
  const TABS = [
    ["state", "状态"],
    ["sleep", "睡眠"],
    ["memory", "记忆"],
    ["dream", "梦境"],
    ["config", "设置"],
  ];
  return `<div class="nj-night-tabs">
    <div class="nj-seg" id="nj-night-tabs">${TABS.map(
      ([key, label]) => `<button data-night-tab="${key}" class="${active === key ? "is-on" : ""}">${esc(label)}</button>`
    ).join("")}</div>
    <span class="status"><i></i>长夜如梦<b style="font-family:var(--nj-mono);font-size:10px;color:#6c7d72">${esc(NJ_NIGHT_TAB_CN[active] || active)}</b></span>
  </div>`;
}

async function njNight() {
  njShell();
  njTitle("05");
  const TABS = ["state", "sleep", "memory", "dream", "config"];
  if (!TABS.includes(nightTab)) nightTab = "state";

  const needState = nightTab === "state" || nightTab === "sleep" || nightTab === "memory";
  const needUnread = nightTab === "state" || nightTab === "memory";
  const [st, unreadData, heatData, dreamData, settings, dreamPeek] = await Promise.all([
    needState ? get("night/state").catch(() => ({})) : Promise.resolve({}),
    needUnread ? get("night/unread", { limit: 40 }).catch(() => ({ items: [] })) : Promise.resolve({ items: [] }),
    nightTab === "state" ? get("page/memory/m3/overview").catch(() => ({})) : Promise.resolve({}),
    nightTab === "dream" ? get("night/dreams", { limit: 60 }).catch(() => ({ items: [], nights: [], stats: {} })) : Promise.resolve(null),
    nightTab === "config" ? loadSettings() : Promise.resolve(null),
    nightTab === "state" ? get("night/dreams", { limit: 3 }).catch(() => ({ items: [] })) : Promise.resolve(null),
  ]);
  const unread = (unreadData.items || []).slice().sort((a, b) => Number(b.unread_since || 0) - Number(a.unread_since || 0));
  const heat = heatData.heatmap || {};

  let body = "";
  if (nightTab === "sleep") body = njNightSleep(st);
  else if (nightTab === "memory") body = njNightMemory(st, unread);
  else if (nightTab === "dream") body = njNightDream(dreamData);
  else if (nightTab === "config") body = njNightSettings(settings);
  else body = njNightState(st, unread, heat, dreamPeek);

  view.innerHTML = `${njNightTabs(nightTab)}${body}`;

  njDiaryBind();

  /* 子标签 / 快速跳转 */
  view.querySelectorAll("[data-night-tab]").forEach((btn) => {
    btn.onclick = () => {
      nightTab = btn.dataset.nightTab;
      njNight();
    };
  });
  view.querySelectorAll("[data-night-refresh]").forEach((btn) => {
    btn.onclick = () => njNight();
  });

  /* 唤醒 / 入睡 */
  view.querySelectorAll("[data-night-act]").forEach((btn) => {
    btn.onclick = async () => {
      const act = btn.dataset.nightAct;
      btn.disabled = true;
      try {
        await post(act === "wake" ? "/night/wake" : "/night/sleep", act === "wake" ? undefined : {});
        settingsCache = null;
        njNight();
      } catch (err) {
        njToast(err.message || "操作失败", "err");
        btn.disabled = false;
      }
    };
  });

  /* 标记已爬 */
  view.querySelectorAll("[data-night-crawl]").forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await post("/night/crawl", {});
        settingsCache = null;
        njNight();
      } catch (err) {
        njToast(err.message || "操作失败", "err");
        btn.disabled = false;
      }
    };
  });

  /* 梦境：手动补一段，用来验证链路 / 立刻看效果 */
  view.querySelectorAll("[data-dream-make]").forEach((btn) => {
    btn.onclick = async () => {
      if (btn.dataset.busy === "1") return;
      btn.dataset.busy = "1";
      const label = btn.textContent;
      btn.textContent = "正在做…";
      btn.disabled = true;
      try {
        await post("/night/dream/make", {});
        njToast("做好了一段梦", "ok");
        njNight();
      } catch (err) {
        njToast(err.message || "没做成", "err");
        btn.textContent = label;
        btn.disabled = false;
        btn.dataset.busy = "";
      }
    };
  });

  /* 梦境：拽住 / 松开 / 删 */
  view.querySelectorAll("[data-dream-pin]").forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await post("/night/dream/pin", { dream_id: btn.dataset.dreamId, pinned: btn.dataset.pinned !== "1" });
        njNight();
      } catch (err) {
        njToast(err.message || "操作失败", "err");
        btn.disabled = false;
      }
    };
  });
  view.querySelectorAll("[data-dream-del]").forEach((btn) => {
    btn.onclick = async () => {
      if (!(await njConfirm("删掉这段梦？"))) return;
      btn.disabled = true;
      try {
        await post("/night/dream/delete", { dream_id: btn.dataset.dreamId });
        njNight();
      } catch (err) {
        njToast(err.message || "删除失败", "err");
        btn.disabled = false;
      }
    };
  });

  /* 设置页的保存 */
  if (nightTab === "config") njBindSettings(view);
}

function njNightState(st, unread, heat, dreams) {
  const bars = njHeatSeries(heat, 14);
  const barMax = Math.max(1, ...bars);
  const rhythm = bars
    .map((n, i) => {
      const d = new Date(Date.now() - (13 - i) * 86400000);
      return `<div class="col${i === bars.length - 1 ? " is-now" : ""}">
        <i style="height:${Math.max(5, Math.round((n / barMax) * 100))}%" title="${n} 条"></i>
        <span>${d.getDate()}</span></div>`;
    })
    .join("");

  const unreadRows = unread.length
    ? unread
        .slice(0, 4)
        .map(
          (x) => `<div class="nj-unread-row"><time>${esc(njUnreadClock(x.unread_since))}</time>
            <div><p>${esc(x.text || "")}</p><em>${esc(x.session_label || "")}</em></div></div>`
        )
        .join("")
    : '<div class="nj-empty">没有没爬的消息</div>';

  const dream = (st.dream && st.dream.stats) || {};
  const latest = ((dreams && dreams.items) || [])[0] || null;
  const dreamLevel = Math.max(1, Math.min(5, Number((latest && latest.level) || 3)));
  const dreamCardBody = !st.dream_on
    ? '<div class="nj-empty">梦境关着，今晚不会做梦</div>'
    : latest
      ? `<div class="nj-rhythm-top"><b>${esc(dream.living || 0)}<small> 段还在</small></b><span>共 ${esc(dream.total || 0)} 段</span></div>
         <div class="nj-dream-stars" style="margin:2px 0 0">${"★".repeat(dreamLevel)}${"☆".repeat(5 - dreamLevel)}</div>
         <p class="nj-dream-quote">${esc(njClip(latest.text, 72))}</p>
         <div class="nj-dream-tags">${(latest.tags || []).map((t) => `<span>${esc(t)}</span>`).join("") || `<span>${esc(njClock(latest.created_at))} 记下</span>`}</div>`
      : `<div class="nj-rhythm-top"><b>${esc(dream.living || 0)}<small> 段还在</small></b><span>共 ${esc(dream.total || 0)} 段</span></div>
         <p class="nj-dream-quote">今晚还没有梦被记下来。睡着满一小时开始做梦，之后每小时一段。</p>`;

  return `
    ${njNightHero(st)}
    <div class="nj-night-bottom">
      <section class="nj-night-card">
        <div class="nj-card-head"><h3>睡眠节律</h3><span class="note">近 14 天记忆条数</span></div>
        <div class="nj-rhythm-top"><b>${esc(bars.reduce((a, b) => a + b, 0))}<small> 条</small></b><span>${bars.filter((n) => n > 0).length} 天有记录</span></div>
        <div class="nj-chart-bars">${rhythm}</div>
        <div class="nj-card-foot"><i></i>14 天前<span>今天</span></div>
      </section>

      <section class="nj-night-card">
        <div class="nj-card-head"><h3>未曾爬楼</h3><span class="note">${unread.length} 条没看</span></div>
        <div class="nj-unread">${unreadRows}</div>
        ${
          unread.length
            ? `<div class="nj-card-foot" style="margin-top:auto;padding-top:10px;justify-content:space-between">
                 <button class="nj-btn tiny" data-night-crawl>标记已爬</button>
                 <button class="nj-btn tiny" data-night-tab="memory">全部 ${unread.length} 条</button>
               </div>`
            : ""
        }
      </section>

      <section class="nj-night-card">
        <div class="nj-card-head"><h3>梦境残片</h3><span class="note">${st.dream_on ? "开着" : "关着"}</span></div>
        ${dreamCardBody}
        <div class="nj-card-foot" style="margin-top:auto;padding-top:10px">
          <button class="nj-btn tiny" data-night-tab="dream">翻全部梦</button>
        </div>
      </section>
    </div>
  `;
}

/** 整夜的睡眠结构：一条彩色甘特 + 四个时长。数据全是后端按 now 现算的 */
function njStageGantt(st) {
  const seq = (st && st.stage_seq) || [];
  if (!seq.length) {
    return `<section class="nj-stage-card">
      <div class="nj-card-head"><h3>这一觉的结构</h3><span class="note">还没睡</span></div>
      <div class="nj-empty">睡着之后这里会显示今晚的睡眠阶段</div>
    </section>`;
  }
  const stats = st.stage_stats || {};
  const start = seq[0].start;
  const end = seq[seq.length - 1].end;
  const span = Math.max(1, end - start);
  const now = Math.floor(Date.now() / 1000);
  const blocks = seq
    .map((seg) => {
      const left = ((seg.start - start) / span) * 100;
      const width = Math.max(0.35, ((seg.end - seg.start) / span) * 100);
      return `<i class="stage-seg" data-stage="${esc(seg.key)}" style="left:${left.toFixed(3)}%;width:${width.toFixed(3)}%"
        title="${esc(seg.label)} ${njClock(seg.start)}–${njClock(seg.end)}"></i>`;
    })
    .join("");
  const cursor = now > start && now < end ? `<b class="now" style="left:${(((now - start) / span) * 100).toFixed(3)}%"></b>` : "";
  const label = (key, cn) => {
    const minutes = Math.max(0, Math.round(stats[key] || 0));
    if (minutes < 60) return `${cn} ${minutes} 分钟`;
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return `${cn} ${h} 小时${m ? ` ${m} 分` : ""}`;
  };
  return `<section class="nj-stage-card">
    <div class="nj-card-head"><h3>这一觉的结构</h3><span class="note">${njClock(start)} – ${njClock(end)}</span></div>
    <div class="nj-stage-gantt">${blocks}${cursor}</div>
    <div class="nj-stage-legend">
      <span data-stage="n1">${label("n1", "刚入睡")}</span>
      <span data-stage="n2">${label("n2", "浅层睡眠")}</span>
      <span data-stage="n3">${label("n3", "深度睡眠")}</span>
      <span data-stage="rem">${label("rem", "快速眼动")}</span>
    </div>
  </section>`;
}

function njNightSleep(st) {
  const rows = [
    ["当前时段", st.fatigue === "night" ? "夜间疲劳期" : st.fatigue === "noon" ? "午间疲劳期" : "不在疲劳期", "疲劳期里精力往下掉"],
    ["夜间疲劳", st.night_fatigue || "—", "到这个时段就开始犯困"],
    ["午间疲劳", st.noon_fatigue || "—", ""],
    ["最晚睡觉", st.latest_sleep_time || "—", "到点无论如何都睡"],
    ["夜间起床点", `${st.wake_time || "08:00"} ± ${st.wake_jitter || 0} 分钟`, "只影响夜里那一觉"],
    ["午睡时长", `${st.noon_nap_minutes || 60} ± ${st.noon_nap_jitter || 0} 分钟`, "午睡按固定时长算"],
    ["唤醒档位", st.wake_mode === "both" ? "私聊和群聊都能叫" : st.wake_mode === "never" ? "叫不醒" : "只在私聊里能叫", st.wake_window ? `叫醒窗口 ${st.wake_window}` : ""],
    ["睡前道别", st.farewell_on ? "会道别" : "不说，直接睡", ""],
    ["真的会睡", st.allow_sleep === false ? "不会，只是累" : "会", ""],
  ];
  const info = rows
    .map(
      ([k, v, hint]) => `<div class="cfg-row"><div class="cfg-label">${esc(k)}${hint ? `<span class="hint">${esc(hint)}</span>` : ""}</div>
        <div><span class="nj-val">${esc(v)}</span></div></div>`
    )
    .join("");
  return `
    ${njNightHero(st)}
    ${njStageGantt(st)}
    <div class="nj-head"><h2>此刻的作息</h2><span>改设置去「设置」那一页</span></div>
    <section class="cfg-group" style="padding-bottom:6px">
      <div class="cfg-head"><h3>时段与开关</h3><p>这些值直接来自长夜状态</p></div>
      ${info}
    </section>
  `;
}

function njNightMemory(st, unread) {
  const rows = unread
    .map(
      (x) => `<div class="nj-ev" data-kind="${esc(x.kind || "fact")}" style="cursor:default">
        <div class="nj-ev-head">
          <span class="nj-kind ${NJ_KIND_CLASS[x.kind] || ""}">${esc(njKindCN(x.kind))}</span>
          <span class="nj-ev-where">${esc(x.session_label || "")}</span>
          <span class="nj-ev-time">${esc(njUnreadClock(x.unread_since))}</span>
        </div>
        <p>${esc(x.text || "")}</p>
      </div>`
    )
    .join("");
  return `
    ${njNightHero(st)}
    <div class="nj-head"><h2>睡着期间记下的</h2><span>${unread.length} 条没爬</span></div>
    <section class="nj-card" style="padding:14px 18px">
      <p class="hint" style="margin:0 0 12px">睡着期间进记忆的话会打上「未曾爬楼」，醒来先翻一遍才算真的看过。</p>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:6px">
        <span class="nj-chip ${st.crawl_on ? "on" : "off"}">爬楼机制 ${st.crawl_on ? "开着" : "关着"}</span>
        ${unread.length ? '<button class="nj-btn sm" data-night-crawl>全部标记已爬</button>' : ""}
      </div>
      <div class="nj-scroll nj-list-scroll nj-night-scroll">
        <div class="nj-timeline" style="padding-left:22px">
          ${rows || '<div class="nj-empty">没有没爬的消息</div>'}
        </div>
      </div>
    </section>
  `;
}

/* 日记页的事件绑定：长夜页和独立板块共用一份 */
function njDiaryBind() {
const writeBtn = document.getElementById("nj-diary-write");
if (writeBtn) {
  writeBtn.onclick = async () => {
    const old = writeBtn.textContent;
    writeBtn.disabled = true;
    writeBtn.textContent = "写着…";
    try {
      await post("diary/generate", {});
      njNight();
    } catch (err) {
      njToast(err.message || "这次没写成", "err");
      writeBtn.disabled = false;
      writeBtn.textContent = old;
    }
  };
}
view.querySelectorAll("[data-diary-fold]").forEach((btn) => {
  btn.onclick = () => {
    const card = btn.closest(".nj-diary");
    if (card) card.classList.toggle("is-open");
  };
});
view.querySelectorAll("[data-diary-pin]").forEach((btn) => {
  btn.onclick = async () => {
    const card = btn.closest(".nj-diary");
    if (!card) return;
    const pinned = !card.classList.contains("is-pinned");
    btn.disabled = true;
    try {
      await post("diary/pin", { id: Number(btn.dataset.diaryPin), pinned });
      njNight();
    } catch (err) {
      njToast(err.message || "置顶失败", "err");
      btn.disabled = false;
    }
  };
});
view.querySelectorAll("[data-diary-del]").forEach((btn) => {
  btn.onclick = async () => {
    if (!(await njConfirm("删掉这篇日记？删了就找不回来了"))) return;
    btn.disabled = true;
    try {
      await post("diary/delete", { id: Number(btn.dataset.diaryDel) });
      njNight();
    } catch (err) {
      njToast(err.message || "删除失败", "err");
      btn.disabled = false;
    }
  };
});
}

/* ============================================================ 状态栏（0.7.0）
   各板块顶部的一条同构状态栏：左标题右指标，底下一层暖光和一枚细线图案。
   图案用 stroke 画，透明度很低——是氛围不是装饰，别抢内容。 */
/** 夜航 UI 统一保存：配置卡交互 + 保存按钮。
 *  原来各页自己写一遍，日记的保存按钮就漏了——点了没反应。
 *  这里统一绑，省得每个新页面再踩一次。 */
function njBindSettings(root, onSaved) {
  const scope = root || view;
  if (typeof bindConfigCards === "function") bindConfigCards(scope);
  const btn = scope.querySelector("#page-save");
  if (!btn) return;
  btn.onclick = async () => {
    const msg = scope.querySelector("#page-msg");
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "保存中…";
    if (msg) msg.textContent = "";
    try {
      const result = await post("page/settings/save", { changes: collectChanges(scope) });
      settingsCache = null;
      if (typeof onSaved === "function") onSaved(result);
      if (msg) msg.textContent = `已保存 ${result.applied} 项${result.saved === false ? "（未写入文件）" : ""}`;
    } catch (err) {
      if (msg) msg.textContent = err.message || "保存失败";
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  };
}

function njHeroArt(kind) {
  const wrap = (inner) =>
    `<svg viewBox="0 0 120 120" fill="none" stroke="currentColor" stroke-width="0.9" stroke-linecap="round">${inner}</svg>`;
  switch (kind) {
    case "diary":
      return wrap(`
        <path d="M78 34a22 22 0 1 0 0 44 26 26 0 0 1 0-44Z"/>
        <path d="M22 74c9-3 16-2 24 3 8-5 15-6 24-3v22c-9-3-16-2-24 3-8-5-15-6-24-3z"/>
        <path d="M46 77v23"/>`);
    case "persona":
      return wrap(`
        <circle cx="60" cy="52" r="20"/>
        <circle cx="60" cy="52" r="33" stroke-dasharray="3 7"/>
        <path d="M60 88v16M40 98c6-6 34-6 40 0"/>`);
    case "behavior":
      return wrap(`
        <path d="M14 44c10-9 20 9 30 0s20 9 30 0 20 9 30 0"/>
        <path d="M14 62c10-9 20 9 30 0s20 9 30 0 20 9 30 0"/>
        <path d="M14 80c10-9 20 9 30 0s20 9 30 0 20 9 30 0"/>`);
    case "proactive":
      return wrap(`
        <path d="M24 58c14-16 58-16 72 0-14 16-58 16-72 0Z"/>
        <circle cx="60" cy="58" r="9"/>
        <path d="M60 18v10M60 92v10M22 34l7 7M98 34l-7 7"/>`);
    case "tools":
      return wrap(`
        <path d="M60 20 86 35v30L60 100 34 65V35z"/>
        <circle cx="60" cy="60" r="11"/>
        <path d="M60 20v22M86 65 68 64M34 65l18-1"/>`);
    case "qq":
      return wrap(`
        <circle cx="60" cy="60" r="12"/>
        <ellipse cx="60" cy="60" rx="40" ry="20" transform="rotate(-22 60 60)"/>
        <ellipse cx="60" cy="60" rx="40" ry="20" transform="rotate(22 60 60)"/>`);
    case "system":
      return wrap(`
        <path d="M26 26h30v30H26zM64 26h30v30H64zM26 64h30v30H26zM64 64h30v30H64z"/>
        <circle cx="80" cy="80" r="4"/>`);
    case "memory":
    default:
      return wrap(`
        <circle cx="44" cy="44" r="7"/><circle cx="80" cy="38" r="5"/><circle cx="66" cy="78" r="6"/>
        <circle cx="34" cy="82" r="4"/>
        <path d="M44 44 66 78M44 44l36-6M66 78l14-40M34 82l32-4"/>`);
  }
}

/** 把状态栏之外的内容收进一个滚动框里（渲染完一帧后跑）。
 *  为什么要这么绕：各页都是一次 innerHTML 拼完，没法在拼的时候就把后半段包起来；
 *  等 DOM 落地再搬一次，事件绑定不受影响（appendChild 不丢监听）。 */
function njWrapBody() {
  if (!njIsNight() || !view) return;
  const kids = Array.from(view.children);
  const sticky = (el) =>
    el.classList && (el.classList.contains("nj-herobar") || el.classList.contains("nj-mem-stats"));
  let index = 0;
  while (index < kids.length && sticky(kids[index])) index += 1;
  if (index === 0) return;
  let box = kids.find((el) => el.classList.contains("nj-page-body"));
  if (!box) {
    box = document.createElement("div");
    box.className = "nj-page-body";
    view.appendChild(box);
  }
  kids.slice(index).forEach((el) => {
    if (el !== box && !el.classList.contains("nj-page-body")) box.appendChild(el);
  });
}

/** 页面骨架：状态栏常驻 + 下方内容独立滚动。
 *  夜航下 #view 是 flex column 且不吃整页滚动，所以把内容包一层自己滚，
 *  这样上面那条状态栏一直在，下面的设置区滚自己的。 */
function njPage(hero, body, opts) {
  const o = opts || {};
  const cls = ["nj-page-body", o.plain ? "is-plain" : "", o.wide ? "is-wide" : "", o.bodyClass || ""]
    .filter(Boolean)
    .join(" ");
  return `${hero}<div class="${cls}">${body}</div>`;
}

function njRing(value, max, label, sub) {
  const v = Number(value) || 0;
  const m = Number(max) || 1;
  const pct = Math.max(0, Math.min(1, v / m));
  const R = 34;
  const C = 2 * Math.PI * R;
  return `<div class="nj-ring">
    <svg viewBox="0 0 88 88" aria-hidden="true">
      <circle class="bg" cx="44" cy="44" r="${R}"></circle>
      <circle class="fg" cx="44" cy="44" r="${R}"
        style="stroke-dasharray:${C.toFixed(1)};stroke-dashoffset:${(C * (1 - pct)).toFixed(1)};transform:rotate(-90deg);transform-origin:44px 44px"></circle>
    </svg>
    <div class="nj-ring-txt"><b>${esc(value == null ? "—" : value)}</b>${sub ? `<small>${esc(sub)}</small>` : ""}</div>
    ${label ? `<span class="nj-ring-label">${esc(label)}</span>` : ""}
  </div>`;
}

function njTape(pct) {
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div class="nj-herobar-tape" aria-hidden="true"><i style="width:${p}%"></i><b style="left:calc(${p}% - 1px)"></b></div>`;
}

function njHeroBar(o) {
  /* 状态栏拼进 innerHTML 后，下一帧把后面的内容收进滚动框（见 njWrapBody） */
  requestAnimationFrame(njWrapBody);
  const stats = (o.stats || [])
    .map(
      (item) => `<div class="${item.warn ? "is-warn" : ""}">
        <span>${esc(item.label || "")}</span>
        <b>${esc(item.value == null ? "—" : item.value)}${item.unit ? `<small>${esc(item.unit)}</small>` : ""}</b>
      </div>`
    )
    .join("");
  const cls = ["nj-herobar", o.variant ? `is-${o.variant}` : "", o.tone ? `is-${o.tone}` : ""]
    .filter(Boolean)
    .join(" ");
  return `<section class="${cls}">
    <div class="nj-herobar-glow" aria-hidden="true"></div>
    <div class="nj-herobar-pattern" aria-hidden="true">${njHeroArt(o.art || "memory")}</div>
    <div class="nj-herobar-main">
      <span class="nj-herobar-eyebrow">${esc(o.eyebrow || "")}</span>
      <h2>${esc(o.title || "")}</h2>
      <p>${esc(o.note || "")}</p>
    </div>
    <div class="nj-herobar-side">
      ${stats ? `<div class="nj-herobar-stats">${stats}</div>` : ""}
      ${o.ring ? njRing(o.ring.value, o.ring.max, o.ring.label, o.ring.sub) : ""}
      ${o.actions || ""}
    </div>
    ${o.tape != null ? njTape(o.tape) : ""}
  </section>`;
}

/* 独立板块：日记 */
async function njDiaryPage() {
  njShell();
  njTitle("06");
  const [data, settings] = await Promise.all([
    get("diary/list", { limit: 60 }).catch(() => ({ items: [], enabled: false })),
    loadSettings().catch(() => null),
  ]);
  const items = data.items || [];
  const today = new Date();
  const todayKey = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(
    today.getDate()
  ).padStart(2, "0")}`;
  const wroteToday = items.some((row) => String(row.day_key || "") === todayKey);
  const totalChars = items.reduce((sum, row) => sum + Number(row.chars || 0), 0);
  /* 连续写了几天：从今天往回数 */
  let streak = 0;
  for (let i = 0; i < 60; i += 1) {
    const d = new Date(today.getTime() - i * 86400000);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    if (items.some((row) => String(row.day_key || "") === key)) streak += 1;
    else if (i > 0) break;
  }
  const latest = items[0] || null;

  view.innerHTML = `
    ${njHeroBar({
      eyebrow: "06 / DIARY",
      title: "日记",
      note: data.enabled
        ? `每天 ${data.hour || 23} 点之后写一篇，素材只来自真实记忆与梦。她写这些不是为了给谁看。`
        : "日记总开关关着，在下面的设置里打开",
      art: "diary",
      stats: [
        { label: "已写", value: items.length, unit: "篇" },
        { label: "连续", value: streak, unit: "天" },
        { label: "总字数", value: totalChars > 999 ? `${(totalChars / 1000).toFixed(1)}k` : totalChars },
        { label: "今天", value: wroteToday ? "写了" : "还没", warn: !wroteToday },
      ],
      actions: `<button class="nj-btn sm primary" id="nj-diary-write" ${data.enabled ? "" : "disabled"}>现在就写一篇</button>
        ${latest ? `<span class="nj-herobar-foot">上次：${esc(latest.day_label || latest.day_key || "")}</span>` : ""}`,
    })}
    <div class="nj-diary-list">${
      items.length
        ? items.map(njDiaryCard).join("")
        : `<div class="nj-empty">她还没开始写${data.enabled ? "" : "（日记开关是关的）"}</div>`
    }</div>
    ${settings ? `<div class="nj-diary-cfg">
      ${configCard(pickGroup(settings, "diary"), { title: "日记设置", desc: "写不写、几点写、写多长、引不引梦、要不要写名字" })}
      ${pageActions("保存日记设置")}
    </div>` : ""}`;
  njDiaryBind();
  if (settings) {
    njBindSettings(view, () => setTimeout(() => njDiaryPage(), 900));
  }
}

/* ============================================================ 主动找人（0.7.0） */
function njProactiveRow(item) {
  const muted = Number(item.muted_until || 0) * 1000 > Date.now();
  const kind = item.session_kind === "group" ? "group" : "user";
  const badge = item.session_kind === "group" ? "群" : "";
  return `<div class="nj-px-row ${muted ? "is-muted" : ""}" data-px-session="${esc(item.session_id)}">
    ${njAvatar(item.session_id, item.label, { kind, sessionId: item.session_id })}
    <div class="nj-px-main">
      <b>${esc(item.label || item.session_id)}${badge ? `<span class="nj-px-badge">${badge}</span>` : ""}</b>
      <span>${esc(item.idle_text || "")}没说话${
        item.unanswered ? ` · 未回复 ${esc(item.unanswered)}` : ""
      }${muted ? " · 静音中" : ""}</span>
    </div>
    <div class="nj-px-ops">
      <button type="button" data-px-send="${esc(item.session_id)}" title="立刻发一条">发</button>
      <button type="button" data-px-mute="${esc(item.session_id)}" title="${muted ? "解除静音" : "静音 6 小时"}">${
        muted ? "解" : "静"
      }</button>
    </div>
  </div>`;
}

function njProactiveLogRow(row) {
  const state = row.replied ? "回了" : "没回";
  return `<div class="nj-px-log ${row.replied ? "is-replied" : ""}">
    <span class="nj-px-time">${esc(row.at_text || "")}</span>
    <span class="nj-px-text">${esc(row.text || "")}</span>
    <span class="nj-px-state">${state}</span>
  </div>`;
}

async function njProactive() {
  njShell();
  njTitle("03");
  const [stateData, sessionsData, logData, settings] = await Promise.all([
    get("proactive/state").catch(() => ({})),
    get("proactive/sessions").catch(() => ({ items: [] })),
    get("proactive/log", { limit: 30 }).catch(() => ({ items: [] })),
    loadSettings().catch(() => null),
  ]);
  const items = sessionsData.items || [];
  const logs = logData.items || [];
  const unanswered = items.reduce((sum, item) => sum + Number(item.unanswered || 0), 0);
  const mutedAt = Number(stateData.muted_until || 0);
  const mutedText = mutedAt * 1000 > Date.now() ? `静默至 ${new Date(mutedAt * 1000).toTimeString().slice(0, 5)}` : "没在静默";

  const left = items.length
    ? items.map(njProactiveRow).join("")
    : `<div class="nj-empty">最近没人说话，她也没处可找</div>`;
  const right = logs.length
    ? logs.map(njProactiveLogRow).join("")
    : `<div class="nj-empty">还没主动说过话</div>`;

  view.innerHTML = `
    ${njHeroBar({
      eyebrow: "03 / PROACTIVE",
      variant: "ring",
      ring: { value: stateData.sent_today || 0, max: stateData.daily_cap || 6, label: "今日额度", sub: `/${stateData.daily_cap || 6}` },
      title: "主动找人",
      note: stateData.enabled
        ? `没人说话的时候她自己会想找人。睡着不发、${stateData.quiet_hours || "01:00-08:00"} 免打扰、连着没被回会退避。`
        : "主动找人是关的，在下面的设置里打开",
      art: "proactive",
      stats: [
        { label: "今日已发", value: `${stateData.sent_today || 0}/${stateData.daily_cap || 6}` },
        { label: "候选会话", value: items.length, unit: "个" },
        { label: "未回复", value: unanswered, warn: unanswered > 0 },
        { label: "状态", value: stateData.enabled ? mutedText : "关着", warn: !stateData.enabled },
      ],
    })}
    <div class="nj-split" style="margin-top:14px">
      <section class="nj-card">
        <div class="nj-px-head"><h3>谁会被找</h3><span>${esc(items.length)}</span></div>
        <div class="nj-px-list nj-scroll">${left}</div>
      </section>
      <section class="nj-card">
        <div class="nj-px-head"><h3>最近主动</h3><span>${esc(logs.length)}</span></div>
        <div class="nj-px-list nj-scroll">${right}</div>
      </section>
    </div>
    <div class="nj-diary-cfg">
      ${settings ? configCard(pickGroup(settings, "proactive"), { title: "主动找人设置", desc: "闲多久开始想人、每天最多几条、什么时段不发" }) : ""}
      ${settings ? pageActions("保存主动找人设置") : ""}
    </div>`;

  if (settings) njBindSettings(view);
  view.querySelectorAll("[data-px-send]").forEach((btn) => {
    btn.onclick = async () => {
      const id = btn.dataset.pxSend;
      if (!(await njConfirm(`现在就让 ${id} 收到一条？`))) return;
      btn.disabled = true;
      btn.textContent = "…";
      try {
        await post("proactive/now", { session_id: id });
        njProactive();
      } catch (err) {
        njToast(err.message || "没发出去", "err");
        btn.disabled = false;
        btn.textContent = "发";
      }
    };
  });
  view.querySelectorAll("[data-px-mute]").forEach((btn) => {
    btn.onclick = async () => {
      const id = btn.dataset.pxMute;
      const row = btn.closest(".nj-px-row");
      const muted = row && row.classList.contains("is-muted");
      btn.disabled = true;
      try {
        await post("proactive/mute", { session_id: id, hours: muted ? 0 : 6 });
        njProactive();
      } catch (err) {
        njToast(err.message || "静音失败", "err");
        btn.disabled = false;
      }
    };
  });
}

/* ============================================================ 日记（0.7.0） */
function njDiaryCard(row) {
  const mood = esc(row.mood || "安静");
  const refs = (row.refs || []).length;
  return `<article class="nj-diary ${row.pinned ? "is-pinned" : ""}" data-diary-id="${esc(row.id)}">
    <header>
      <div class="nj-diary-day">
        <b>${esc(row.day_label || row.day_key || "")}</b>
        <span class="nj-diary-mood">${mood}</span>
        ${refs ? `<span class="nj-diary-refs">引了 ${esc(refs)} 条记忆</span>` : ""}
      </div>
      <div class="nj-diary-ops">
        <button type="button" data-diary-pin="${esc(row.id)}" title="${row.pinned ? "取消置顶" : "置顶"}">${row.pinned ? "★" : "☆"}</button>
        <button type="button" data-diary-fold title="展开 / 收起">⋯</button>
      </div>
    </header>
    ${row.title ? `<h4>${esc(row.title)}</h4>` : ""}
    <p class="nj-diary-text">${esc(row.text || "")}</p>
    <footer>
      <span>${esc(row.created_text || "")}${row.chars ? ` · ${esc(row.chars)} 字` : ""}</span>
      <button type="button" class="nj-diary-del" data-diary-del="${esc(row.id)}">删掉</button>
    </footer>
  </article>`;
}


function njNightDream(dreamData) {
  const nights = (dreamData && dreamData.nights) || [];
  const stats = (dreamData && dreamData.stats) || {};
  const blocks = nights
    .map((night) => {
      const segs = night.segments || [];
      const first = segs[0] || {};
      const last = segs[segs.length - 1] || {};
      const range = segs.length > 1 ? `${njClock(first.created_at)}–${njClock(last.created_at)}` : njClock(first.created_at);
      const parts = segs.map(njDreamSeg).join("");
      return `<section class="nj-dream-night">
        <div class="nj-dn-head">
          <b>${esc(njDayLabel(first.created_at))}</b>
          <span>${segs.length} 段 · ${esc(range)}</span>
          <i class="rule"></i>
        </div>
        ${parts}
      </section>`;
    })
    .join("");
  return `
    <div class="nj-head" style="margin-top:0"><h2>做过的梦</h2>
      <div style="display:flex;align-items:center;gap:10px">
        <span>${esc(stats.living || 0)} 段还在 / 共 ${esc(stats.total || 0)} 段</span>
        <button class="nj-btn tiny" data-dream-make>现在做一段</button>
      </div>
    </div>
    <p class="hint" style="margin:-4px 0 14px">睡着半小时后开始做梦，之后每半小时一段。梦按等级一天天淡掉，拽住了就不会散。想立刻看效果就点右边那个按钮。</p>
    ${blocks || '<div class="nj-empty">还没做过梦</div>'}
  `;
}

function njDreamSeg(seg) {
  const level = Math.max(1, Math.min(5, Number(seg.level) || 3));
  const days = Number(seg.remaining_days || 0);
  const fade = seg.pinned ? "已拽住" : seg.expired ? "已经忘了" : days <= 0 ? "明天就忘" : `还能记 ${days} 天`;
  const tags = (seg.tags || []).map((t) => `<span>${esc(t)}</span>`).join("");
  const lead = String(seg.text || "").split("。")[0];
  const head = lead && lead.length <= 40 ? lead + "。" : "";
  const rest = head && String(seg.text || "").startsWith(head) ? String(seg.text || "").slice(head.length) : "";
  return `<article class="nj-seg-card lv${level}">
    <div class="nj-seg-top">
      <span class="nj-dream-stars">${"★".repeat(level)}${"☆".repeat(5 - level)}</span>
      <time>${esc(njClock(seg.created_at))}</time>
      <span class="left">${esc(fade)}</span>
    </div>
    <p class="nj-seg-lead">${esc(head || seg.text || "")}</p>
    ${rest ? `<p class="nj-seg-text">${esc(rest)}</p>` : ""}
    ${tags ? `<div class="nj-dream-tags" style="margin-top:8px">${tags}</div>` : ""}
    <div class="nj-seg-foot">
      <button class="nj-btn tiny ${seg.pinned ? "gold" : ""}" data-dream-pin data-dream-id="${esc(seg.dream_id)}" data-pinned="${seg.pinned ? "1" : "0"}">${seg.pinned ? "松开" : "拽住"}</button>
      <button class="nj-btn tiny danger" data-dream-del data-dream-id="${esc(seg.dream_id)}">删掉</button>
    </div>
  </article>`;
}

function njNightSettings(settings) {
  const night = pickGroup(settings, "night");
  const close = pickGroup(settings, "close");
  const card = (title, desc, keys) => nightCard(title, desc, sliceItems(night, keys));
  return `
    <div class="nj-head" style="margin-top:0"><h2>长夜设置</h2><span>改完点下面的保存</span></div>
    ${card("总开关", "关掉之后它就不知道累，也不会自己睡", ["enabled"])}
    ${card("疲劳时段", "这两个时段里它会犯困、精力往下掉", ["noon_fatigue", "night_fatigue", "latest_sleep_time"])}
    ${card("起床", "夜里睡醒了在固定点上下浮动，浮动上限 90 分钟", ["wake_time", "wake_jitter_minutes"])}
    ${card("午睡", "午睡按固定时长算，不跟夜里那个起床点走", ["noon_nap_minutes", "noon_nap_jitter_minutes"])}
    ${card("唤醒", "睡着之后有多难叫醒。y 到 y+n 之间概率平分 100%", ["wake_mode", "wake_window_minutes", "wake_start_count", "wake_step_count"])}
    ${card("入睡前的道别", "多个会话一起道别时，要不要说同一句", ["farewell_before_sleep", "farewell_same", "farewell_random", "farewell_use_llm"])}
    ${card("精力", "疲劳期里怎么掉、平时怎么回", [...NIGHT_ENERGY_KEYS, "min_turns_before_sleep"])}
    ${card("爬楼", "关掉之后睡着的记忆就直接可读了", NIGHT_MEMORY_KEYS)}
    ${card("梦境", "关掉就不做梦，开着 token 消耗会明显增加", NIGHT_DREAM_KEYS)}
    ${configCard(close, { title: "收线静默", desc: "聊到晚安、拜拜这种话就真的停下来" })}
    ${pageActions("保存本页设置")}
  `;
}

/* ------------------------------------------------------------ 工具 */
async function njTools() {
  njShell();
  njTitle("07");
  const [settings, data] = await Promise.all([loadSettings(), get("page/tools")]);
  const items = data.items || [];
  const groups = {};
  items.forEach((row) => {
    groups[row.group || "其它"] = (groups[row.group || "其它"] || 0) + 1;
  });
  const rows = items
    .map(
      (row) => `<tr>
        <td class="name">${esc(row.name)}</td>
        <td class="num">${esc(row.group)}</td>
        <td class="txt">${esc(firstSentence(row.description, 46))}</td>
        <td class="num">${esc(row.calls || 0)} / ${esc(row.errors || 0)} · ${esc(Math.round(Number(row.avg_ms) || 0))}ms</td>
        <td style="text-align:right"><button class="nj-btn tiny" data-test="${esc(row.name)}">试跑</button></td>
      </tr>`
    )
    .join("");

  const capGroup = pickGroup(settings, "capability") || { items: [] };
  const capValue = (key, fallback) => {
    const hit = (capGroup.items || []).find((item) => item.key === key);
    return hit && hit.value != null ? hit.value : fallback;
  };
  view.innerHTML = `
    ${njHeroBar({
      eyebrow: "07 / TOOLS",
      variant: "compact",
      title: "工具",
      note: "分组决定开放哪些能力，限流是每会话每分钟的次数上限。已经在跑的能力下面的表里能看到调用次数。",
      art: "tools",
      stats: [
        { label: "能力", value: items.length, unit: "个" },
        { label: "分组", value: Object.keys(groups).length, unit: "组" },
        { label: "限流", value: `${capValue("rate_limit_per_min", "—")}/分` },
        { label: "模块", value: capValue("enabled", true) ? "开着" : "关着", warn: !capValue("enabled", true) },
      ],
    })}
    <div class="nj-head" style="margin-top:22px"><h2>工具设置</h2><span>分组决定开放哪些能力</span></div>
    ${configCard(pickGroup(settings, "capability"), { title: "实用能力", desc: "分组决定开放哪些能力；限流是每会话每分钟的次数上限" })}
    ${pageActions("保存工具设置")}

    <div class="nj-head"><h2>已注册能力</h2><em>${items.length} 个</em><span>${Object.keys(groups).map((k) => `${k} ${groups[k]}`).join(" · ")}</span></div>
    ${
      items.length
        ? `<section class="nj-card" style="padding:14px 18px"><table class="nj-table">
            <thead><tr><th>名称</th><th>分组</th><th>说明</th><th>调用 / 失败 / 耗时</th><th></th></tr></thead>
            <tbody>${rows}</tbody></table></section>`
        : '<div class="nj-empty">能力模块未启用</div>'
    }
  `;

  njBindSettings(view);
  view.querySelectorAll("[data-test]").forEach((btn) => {
    btn.onclick = async () => {
      const raw = prompt(`${btn.dataset.test} 参数（JSON，可留空）`, "{}");
      if (raw == null) return;
      let params = {};
      try {
        params = raw.trim() ? JSON.parse(raw) : {};
      } catch (err) {
        njToast("JSON 格式不对", "err");
        return;
      }
      btn.disabled = true;
      try {
        const result = await post("page/tools/test", { name: btn.dataset.test, params });
        njToast(`${result.ok ? "成功" : "失败"} · ${result.elapsed}s${result.cached ? "（缓存）" : ""}\n\n${result.text || result.error || ""}`, "err");
      } catch (err) {
        njToast(err.message, "err");
      } finally {
        btn.disabled = false;
      }
    };
  });
}

/* ------------------------------------------------------------ QQ */
async function njQQ() {
  njShell();
  njTitle("08");
  const [settings, qzone] = await Promise.all([loadSettings(), get("page/qzone")]);
  const auth = qzone.auth || {};
  const modeText = { auto: "自动", builtin: "内置", off: "关闭" };
  const canWrite = !!qzone.write_allowed;

  const cells = [
    ["模式", esc(modeText[qzone.mode] || qzone.mode || "—")],
    ["登录态", auth.ok ? `<span class="nj-chip on">已就绪 · ${esc(auth.uin)}</span>` : '<span class="nj-chip rose">未获取</span>'],
    ["来源", esc(auth.source || "—")],
    ["获取时间", esc(auth.refreshed_text || "—")],
    ["写权限", canWrite ? '<span class="nj-chip on">已开</span>' : '<span class="nj-chip rose">关闭</span>'],
    ["外部空间插件", qzone.external ? "已装（可共存）" : "未装"],
  ]
    .map(([k, v]) => `<div class="nj-cell"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`)
    .join("");

  view.innerHTML = `
    ${njHeroBar({
      eyebrow: "08 / QQ",
      variant: "compact",
      title: "QQ 扩展",
      note: "签名、名片、空间。发说说要先开写权限；空间访问靠协议端的登录态，不用另外装插件。",
      art: "qq",
      stats: [
        { label: "模式", value: modeText[qzone.mode] || qzone.mode || "—" },
        { label: "登录态", value: auth.ok ? `就绪 · ${auth.uin || ""}` : "未获取", warn: !auth.ok },
        { label: "写权限", value: canWrite ? "已开" : "关闭", warn: !canWrite },
        { label: "外部插件", value: qzone.external ? "已装" : "未装" },
      ],
    })}
    <div class="nj-head" style="margin-top:22px"><h2>QQ 扩展设置</h2><span>签名、名片、空间</span></div>
    ${configCard(pickGroup(settings, "qq_extra"), { title: "QQ 扩展", desc: "发说说需要先打开写权限；空间访问靠协议端的登录态，不用另外装插件" })}
    ${pageActions("保存 QQ 设置")}

    <div class="nj-head"><h2>空间状态</h2><span>${qzone.note ? esc(qzone.note) : "签名 / 名片 / 说说"}</span></div>
    <div class="nj-cells">${cells}</div>

    <div class="nj-head"><h2>看谁的空间</h2><span>留空 = 我自己</span></div>
    <section class="nj-card">
      <div class="nj-toolbar" style="margin:0">
        <label class="nj-search"><svg aria-hidden="true"><use href="#i-search"></use></svg>
          <input type="text" id="nj-qz-target" placeholder="留空 = 我自己；也可以填 QQ 号" /></label>
        <button class="nj-btn sm" id="nj-qz-read">读说说</button>
        <button class="nj-btn sm" id="nj-qz-auth">重新获取登录态</button>
        <span class="nj-msg" id="nj-qz-msg"></span>
      </div>
      <p class="hint" style="margin:10px 0 0">群里让机器人看某人的空间时，它会自己认人（@、QQ 号、群名片）；这里只是手动指定</p>
      <div id="nj-qz-feed" style="margin-top:12px"><div class="nj-empty">还没读</div></div>
    </section>

    <div class="nj-head"><h2>发说说</h2><span>${canWrite ? "写权限已开" : "先去上面打开「允许写入空间」并保存"}</span></div>
    <section class="nj-card">
      <div class="field">
        <textarea id="nj-qz-content" placeholder="写点什么…" style="min-height:120px"></textarea>
        <div class="hint"><span id="nj-qz-count">0</span> / 1000</div>
      </div>
      <div class="nj-actions" style="margin-top:12px">
        <button class="nj-btn primary" id="nj-qz-publish" ${canWrite ? "" : "disabled"}>发出去</button>
        <span class="nj-msg" id="nj-qz-pub-msg">${canWrite ? "" : "先打开写权限"}</span>
      </div>
    </section>
  `;

  njBindSettings(view);

  const msg = document.getElementById("nj-qz-msg");
  const feed = document.getElementById("nj-qz-feed");
  const renderFeed = (items) => {
    if (!items.length) {
      feed.innerHTML = '<div class="nj-empty">最近没什么动静</div>';
      return;
    }
    feed.innerHTML = `<table class="nj-table">
      <thead><tr><th>时间</th><th>谁</th><th>内容</th><th>互动</th></tr></thead>
      <tbody>${items
        .map(
          (item) => `<tr>
            <td class="num">${esc(item.created_text || "—")}</td>
            <td class="txt">${esc(item.nickname || item.uin || "")}</td>
            <td class="txt">${esc(item.content || "")}</td>
            <td class="num">赞 ${esc(item.like_count || 0)} · 评 ${esc(item.comment_count || 0)}</td>
          </tr>`
        )
        .join("")}</tbody></table>`;
  };

  document.getElementById("nj-qz-read").onclick = async () => {
    msg.textContent = "读取中…";
    feed.innerHTML = '<div class="nj-empty">读取中…</div>';
    try {
      const target = document.getElementById("nj-qz-target").value.trim();
      const data = await get("page/qzone/feed", target ? { target } : {});
      renderFeed(data.items || []);
      const who = (data.target || {}).label || "";
      msg.textContent = `${who ? who + " · " : ""}读到 ${(data.items || []).length} 条`;
    } catch (err) {
      feed.innerHTML = `<div class="nj-empty">${esc(err.message)}</div>`;
      msg.textContent = "";
    }
  };
  document.getElementById("nj-qz-auth").onclick = async () => {
    msg.textContent = "获取中…";
    try {
      await post("page/qzone/auth", {});
      msg.textContent = "登录态已更新";
      setTimeout(() => njQQ(), 400);
    } catch (err) {
      msg.textContent = err.message;
    }
  };
  const content = document.getElementById("nj-qz-content");
  const counter = document.getElementById("nj-qz-count");
  content.oninput = () => {
    counter.textContent = String(content.value.length);
  };
  const publish = document.getElementById("nj-qz-publish");
  publish.onclick = async () => {
    const pubMsg = document.getElementById("nj-qz-pub-msg");
    const text = content.value.trim();
    if (!text) {
      pubMsg.textContent = "内容是空的";
      return;
    }
    if (!(await njConfirm(`确认发这条说说？\n\n${text.slice(0, 120)}`))) return;
    pubMsg.textContent = "发送中…";
    publish.disabled = true;
    try {
      await post("page/qzone/publish", { content: text });
      pubMsg.textContent = "发出去了";
      content.value = "";
      counter.textContent = "0";
    } catch (err) {
      pubMsg.textContent = err.message;
    } finally {
      publish.disabled = !canWrite;
    }
  };
}

async function njAvatarPage() {
  njShell();
  njTitle("08");
  const data = await get("page/avatar");
  const items = data.items || [];
  const pending = data.pending || [];
  const last = data.last || {};
  const lastText = last.at ? `${fmtTime(last.at)}　来自 ${esc(last.reason || "user")}` : "还没换过";

  const card = (item, isPending) => `<div class="nj-avatar-card">
      <img src="file://${esc(item.path)}" alt="" onerror="this.style.opacity=.25" />
      <div class="meta">
        <span>${esc(item.source)}</span>
        <span>${esc(item.used_text || "")}</span>
      </div>
      <div class="acts">
        ${isPending ? `<button class="nj-btn tiny" data-trust="${item.id}">允许</button>` : ""}
        <button class="nj-btn tiny" data-use="${item.id}">换成这张</button>
        <button class="nj-btn tiny danger" data-drop="${item.id}">删</button>
      </div>
    </div>`;

  view.innerHTML = `
    ${njHeroBar({
      eyebrow: "08 / AVATAR",
      title: "换头像",
      note: data.available ? "通道可用。库里的图按 sha1 去重，换过的不会被挑第二次。" : "现在拿不到 bot 客户端，等协议端连上再来。",
      art: "persona",
      stats: [
        { label: "通道", value: data.available ? "可用" : "不可用", warn: !data.available },
        { label: "今天已换", value: data.used_today || 0, unit: "次" },
        { label: "每日上限", value: data.daily_limit || 0, unit: "次" },
        { label: "上次", value: lastText || "—" },
      ],
    })}
    <div class="nj-head" style="margin-top:22px"><h2>换头像</h2><span>${data.available ? "通道可用" : "现在拿不到 bot 客户端"}</span></div>
    <section class="nj-card">
      <p class="hint" style="margin:0">上次更换：${lastText}；今天已自动换 ${esc(data.used_today || 0)} / ${esc(data.daily_limit || 0)} 次</p>
      <div class="nj-actions" style="margin-top:12px">
        <button class="nj-btn primary" id="nj-avatar-random">从库里挑一张换</button>
        <span class="nj-msg" id="nj-avatar-msg"></span>
      </div>
      <p class="hint" style="margin:12px 0 0">自动换只用「可信」库里的图：你主动存进来的，或者你点过「允许」的。她自己扒到的图只会待在待审区</p>
    </section>

    <div class="nj-head"><h2>可信库</h2><span>${items.length} 张</span></div>
    <div class="nj-avatar-grid">${items.map((item) => card(item, false)).join("") || '<div class="nj-empty">库里还是空的，先把喜欢的图存进来</div>'}</div>

    <div class="nj-head"><h2>待审</h2><span>${pending.length} 张</span></div>
    <div class="nj-avatar-grid">${pending.map((item) => card(item, true)).join("") || '<div class="nj-empty">没有待审的图</div>'}
  `;

  document.querySelectorAll("[data-use]").forEach((btn) => {
    btn.onclick = async () => {
      const msg = document.getElementById("nj-avatar-msg");
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
      njAvatarPage();
    };
  });
  document.querySelectorAll("[data-drop]").forEach((btn) => {
    btn.onclick = async () => {
      await post("page/avatar/pool", { action: "delete", id: Number(btn.dataset.drop) });
      njAvatarPage();
    };
  });
  const random = document.getElementById("nj-avatar-random");
  if (random)
    random.onclick = async () => {
      const msg = document.getElementById("nj-avatar-msg");
      try {
        await post("page/avatar/set", { id: 0, pool_id: 0 });
        const picked = items[0];
        if (picked) await post("page/avatar/set", { id: picked.id });
        if (msg) msg.textContent = "换好了";
        njAvatarPage();
      } catch (err) {
        if (msg) msg.textContent = err.message;
      }
    };
}

/* ------------------------------------------------------------ 系统 */
async function njSystem() {
  njShell();
  njTitle("09");
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
    ? { ...basicGroup, items: basicGroup.items.filter((item) => !["extra_admins", "include_group_role"].includes(item.key)) }
    : null;
  const astrBotList = admins.astrbot || [];
  const effective = admins.effective || [];

  const compatCells = ["humanize", "memory", "rhythm"]
    .map(
      (key) => `<div class="nj-cell"><div class="k">${esc(key)}</div>
        <div class="v">${compat.yield && compat.yield[key] ? '<span class="nj-chip rose">让位</span>' : '<span class="nj-chip on">本插件负责</span>'}</div></div>`
    )
    .join("");

  const yielding = Object.keys(compat.yield || {}).filter((key) => compat.yield[key]);
  view.innerHTML = `
    ${njHeroBar({
      eyebrow: "09 / SYSTEM",
      variant: "compact",
      title: "系统",
      note: "插件本身怎么跑：多模态备用通道、外部插件探测、谁算管理员。",
      art: "system",
      stats: [
        { label: "多模态", value: mmx && mmx.available ? "可用" : "不可用", warn: !(mmx && mmx.available) },
        { label: "外部插件", value: present.length, unit: "个" },
        { label: "让位", value: yielding.length ? yielding.join("/") : "全部自管" },
        { label: "管理员", value: effective.length, unit: "人" },
      ],
    })}
    <div class="nj-head" style="margin-top:22px"><h2>运行状态</h2><span>插件本身怎么跑</span></div>
    <div class="nj-cells">
      <div class="nj-cell"><div class="k">MiniMax 多模态</div>
        <div class="v">${mmx && mmx.available ? '<span class="nj-chip on">可用</span>' : '<span class="nj-chip rose">不可用</span>'}</div></div>
      <div class="nj-cell"><div class="k">说明</div><div class="v" style="font-size:13.5px;color:#93a398">${esc(
        mmx && mmx.available ? "语音识别、合成、看图缺 provider 时会自动用它" : (mmx && mmx.reason) || "没读到状态"
      )}</div></div>
      <div class="nj-cell"><div class="k">外部插件</div><div class="v" style="font-size:13.5px;color:#93a398">${esc(present.join("、") || "无")}</div></div>
    </div>

    <div class="nj-head"><h2>管理员</h2><span>谁能用管理功能</span></div>
    <section class="cfg-group">
      <div class="cfg-head"><h3>名单</h3><p>发说说、看别人的空间、读调试信息</p></div>
      <div class="pad">
        <div class="field" style="margin-bottom:16px">
          <label>来自 AstrBot 全局配置（只读）</label>
          <div style="display:flex;flex-wrap:wrap;gap:7px">
            ${astrBotList.length ? astrBotList.map((id) => `<span class="nj-chip on">${esc(id)}</span>`).join("") : '<span class="hint">没有配置，去 WebUI 全局配置的 admins_id 里加</span>'}
          </div>
          <div class="hint">这部分跟着 AstrBot 走，避免两套名单打架</div>
        </div>
        <div class="field" style="margin-bottom:16px">
          <label>插件额外管理员（一行一个 QQ 号）</label>
          <textarea id="nj-adm-extra" placeholder="123456789" style="min-height:76px">${esc((admins.extra || []).join("\n"))}</textarea>
          <div class="hint">只补额外的人，AstrBot 名单里的自动算</div>
        </div>
        <label class="switch"><input type="checkbox" id="nj-adm-role" ${admins.include_group_role ? "checked" : ""} /><span>群主 / 群管也算管理员</span></label>
        <div class="hint" style="margin-top:8px">默认不算：群管理员不等于机器人管理员，开了之后他们能发说说</div>
        <div class="nj-actions">
          <button class="nj-btn primary" id="nj-adm-save">保存管理员设置</button>
          <span class="nj-msg" id="nj-adm-msg"></span>
        </div>
        <div class="hint" style="margin-top:6px">现在生效：${effective.length ? esc(effective.join("、")) : "只有群主/群管（如果开了上面那个开关）"}</div>
      </div>
    </section>

    <div class="nj-head"><h2>生效范围</h2><span>哪些平台、私聊还是群聊</span></div>
    ${configCard(basicForCard, { title: "基础", desc: "哪些平台、私聊还是群聊；不加平台名就完全不工作" })}

    <div class="nj-head"><h2>与其他插件共存</h2><span>auto 会检测到别人就自动让位</span></div>
    <div class="nj-cells">${compatCells}</div>
    ${configCard(pickGroup(settings, "compat"), { title: "让位策略", desc: "auto 会检测到别的插件就自动让位，避免双人格和双延时" })}
    ${configCard(pickGroup(settings, "debug"), { title: "调试", desc: "决策日志记录它为什么回、为什么不回" })}
    ${pageActions("保存本页设置")}
  `;

  njBindSettings(view);

  document.getElementById("nj-adm-save").onclick = async () => {
    const msg = document.getElementById("nj-adm-msg");
    const extra = document.getElementById("nj-adm-extra").value.split(/[\n,，\s]+/).map((s) => s.trim()).filter(Boolean);
    const includeRole = document.getElementById("nj-adm-role").checked;
    msg.textContent = "保存中…";
    try {
      await post("page/settings/save", { changes: { "basic.extra_admins": extra, "basic.include_group_role": includeRole } });
      settingsCache = null;
      msg.textContent = "已保存";
      setTimeout(() => njSystem(), 900);
    } catch (err) {
      msg.textContent = err.message;
    }
  };
}

