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

async function get(path, params) {
  const api = await ensureBridge();
  return unwrap(await api.apiGet(path, params || {}));
}

async function post(path, body) {
  const api = await ensureBridge();
  return unwrap(await api.apiPost(path, body || {}));
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

/* ------------------------------------------------------------------ 路由 */
const VIEWS = {
  overview: { title: "概览", render: renderOverview },
  persona: { title: "人格", render: renderPersona },
  behavior: { title: "行为", render: renderBehavior },
  memory: { title: "记忆", render: renderMemory },
  tools: { title: "工具", render: renderTools },
  qq: { title: "QQ", render: renderQQ },
  system: { title: "系统", render: renderSystem },
};

let currentView = "overview";
let state = { persona: null, compat: null, health: null };
let settingsCache = null;

function setView(name) {
  if (!VIEWS[name]) return;
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
    else changes[path] = el.value;
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
      if (msg) msg.textContent = `已保存 ${result.applied} 项${result.saved ? "" : "（未写入文件）"}`;
      settingsCache = null;
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

(async function boot() {
  try {
    await ensureBridge();
  } catch (err) {
    view.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
    return;
  }
  await render();
})();
