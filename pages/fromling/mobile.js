/* ============================================================================
 * 从绫开始 · 移动端（mobile.js）
 *
 * 和桌面面板共用同一套 page_api，只是换了一套交互：
 *   - 底部五个页签：概览 / 记忆 / 长夜 / 日记 / 更多
 *   - 子页（人格、设置、工具、空间…）压栈，返回上一层不重新拉数据
 *   - 全部操作走底部弹层，拇指够得着
 *   - 下拉刷新、长按复制、错误就地重试
 *
 * 约定：接口路径都相对 "page/"（见 page_api/apiPath），返回 {status,ok,data}。
 * ==========================================================================*/
(function () {
  "use strict";

  /* ------------------------------------------------------------------ DOM */
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  var dom = {};
  function cacheDom() {
    dom.app = $("#m-app");
    dom.main = $("#m-main");
    dom.tabbar = $("#m-tabbar");
    dom.health = $("#m-health");
    dom.healthText = $("#m-health-text");
    dom.theme = $("#m-theme");
    dom.refresh = $("#m-refresh");
    dom.back = $("#m-back");
    dom.brand = $("#m-brand");
    dom.pageTitle = $("#m-page-title");
    dom.ptr = $("#m-ptr");
    dom.ptrText = $("#m-ptr-text");
    dom.sheetWrap = $("#m-sheet-wrap");
    dom.sheetTitle = $("#m-sheet-title");
    dom.sheetBody = $("#m-sheet-body");
    dom.sheetFoot = $("#m-sheet-foot");
    dom.sheetTools = $("#m-sheet-tools");
    dom.sheetClose = $("#m-sheet-close");
    dom.toastBox = $("#m-toast-box");
    dom.badgeNight = $("#m-badge-night");
    dom.themeColor = $("#m-theme-color");
    dom.savedock = $("#m-savedock");
  }

  /* ------------------------------------------------------------------ 状态 */
  var nav = {
    tab: "",
    stack: [],          // [{name, params, html}]
    scroll: { home: 0, memory: 0, night: 0, diary: 0, more: 0 },
  };

  var st = {
    memory: { items: [], sessions: [], total: 0, unread: 0, tier: "", offset: 0, keyword: "", session: "", search: null },
    night: { state: null, dreams: null, unread: null },
    diary: { items: [], enabled: false, hour: 23 },
    settings: null,
    dirty: {},
    persona: null,
    overview: null,
  };

  var TABS = ["home", "memory", "night", "diary", "more"];
  var TAB_TITLE = { home: "概览", memory: "记忆", night: "长夜如梦", diary: "日记", more: "更多" };

  /* ------------------------------------------------------------------ 工具 */
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function txt(value, fallback) {
    var s = value == null ? "" : String(value);
    return s ? esc(s) : esc(fallback == null ? "—" : fallback);
  }

  function num(value, fallback) {
    var n = Number(value);
    return isFinite(n) ? n : (fallback == null ? 0 : fallback);
  }

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function clockOf(ts) {
    var n = num(ts);
    if (!n) return "—";
    var d = new Date(n * 1000);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  function dateOf(ts) {
    var n = num(ts);
    if (!n) return "—";
    var d = new Date(n * 1000);
    return d.getMonth() + 1 + "月" + d.getDate() + "日";
  }

  function agoOf(ts) {
    var n = num(ts);
    if (!n) return "—";
    var diff = Math.max(0, Math.floor(Date.now() / 1000 - n));
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    if (diff < 86400 * 30) return Math.floor(diff / 86400) + " 天前";
    return dateOf(n);
  }

  function durationText(seconds) {
    var s = Math.max(0, Math.floor(num(seconds)));
    if (s < 60) return s + " 秒";
    if (s < 3600) return Math.floor(s / 60) + " 分钟";
    if (s < 86400) {
      var h = Math.floor(s / 3600);
      var m = Math.floor((s % 3600) / 60);
      return h + " 小时" + (m ? " " + m + " 分" : "");
    }
    var d = Math.floor(s / 86400);
    return d + " 天" + (Math.floor((s % 86400) / 3600) ? " " + Math.floor((s % 86400) / 3600) + " 小时" : "");
  }

  function pct(value, total) {
    var t = num(total);
    if (!t) return 0;
    return Math.max(0, Math.min(100, Math.round((num(value) / t) * 100)));
  }

  function clip(text, max) {
    var s = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
    var m = max || 120;
    return s.length > m ? s.slice(0, m) + "…" : s;
  }

  function shortId(id) {
    var s = String(id == null ? "" : id);
    if (s.length <= 12) return s;
    return s.slice(0, 6) + "…" + s.slice(-4);
  }

  function splitParagraphs(text) {
    var raw = String(text || "").replace(/\r\n/g, "\n").trim();
    if (!raw) return [];
    var byBlank = raw.split(/\n{2,}/);
    if (byBlank.length > 1) return byBlank.map(function (p) { return p.replace(/\n/g, " ").trim(); }).filter(Boolean);
    return raw.split(/\n/).map(function (p) { return p.trim(); }).filter(Boolean);
  }

  /* ------------------------------------------------------------------ 桥接 */
  var bridgePromise = null;

  function ensureBridge() {
    if (bridgePromise) return bridgePromise;
    bridgePromise = new Promise(function (resolve, reject) {
      var started = Date.now();
      (function wait() {
        if (window.AstrBotPluginPage) {
          var api = window.AstrBotPluginPage;
          Promise.resolve(typeof api.ready === "function" ? api.ready() : null)
            .catch(function () {})
            .then(function () { resolve(api); });
          return;
        }
        if (Date.now() - started > 8000) {
          bridgePromise = null;
          reject(new Error("页面桥接没加载上，下拉刷新试试"));
          return;
        }
        setTimeout(wait, 60);
      })();
    });
    return bridgePromise;
  }

  function apiPath(path) {
    var p = String(path == null ? "" : path).replace(/^\/+/, "");
    if (p.indexOf("page/") !== 0) p = "page/" + p;
    return p;
  }

  function unwrap(resp) {
    if (resp == null) return {};
    if (resp.status === "ok" || resp.ok === true) return resp.data;
    if (resp.status === "error" || resp.ok === false) {
      throw new Error(resp.message || resp.error || "请求失败");
    }
    return resp;
  }

  function apiGet(path, params) {
    return ensureBridge().then(function (api) {
      return api.apiGet(apiPath(path), params || {});
    }).then(unwrap);
  }

  function apiPost(path, body) {
    return ensureBridge().then(function (api) {
      return api.apiPost(apiPath(path), body || {});
    }).then(unwrap);
  }

  /* ------------------------------------------------------------------ 提示 / 弹层 */
  function toast(message, kind) {
    var item = document.createElement("div");
    item.className = "m-toast" + (kind === "ok" ? " is-ok" : kind === "err" ? " is-err" : "");
    item.textContent = String(message == null ? "" : message);
    dom.toastBox.appendChild(item);
    setTimeout(function () { item.classList.add("is-out"); }, 2400);
    setTimeout(function () { if (item.parentNode) item.parentNode.removeChild(item); }, 2900);
  }

  var sheetOpts = null;

  function openSheet(opts) {
    sheetOpts = opts || {};
    dom.sheetTitle.textContent = sheetOpts.title || "";
    dom.sheetBody.innerHTML = sheetOpts.body || "";
    dom.sheetTools.innerHTML = sheetOpts.tools || "";
    if (sheetOpts.foot) {
      dom.sheetFoot.innerHTML = sheetOpts.foot;
      dom.sheetFoot.classList.remove("m-hide");
    } else {
      dom.sheetFoot.innerHTML = "";
      dom.sheetFoot.classList.add("m-hide");
    }
    dom.sheetWrap.classList.add("is-open");
    dom.sheetWrap.setAttribute("aria-hidden", "false");
    dom.sheetBody.scrollTop = 0;
    if (typeof sheetOpts.onOpen === "function") sheetOpts.onOpen(dom.sheetBody);
  }

  function closeSheet() {
    dom.sheetWrap.classList.remove("is-open");
    dom.sheetWrap.setAttribute("aria-hidden", "true");
    if (sheetOpts && typeof sheetOpts.onClose === "function") {
      try { sheetOpts.onClose(); } catch (e) { /* 关掉就算了 */ }
    }
    setTimeout(function () {
      if (!dom.sheetWrap.classList.contains("is-open")) {
        dom.sheetBody.innerHTML = "";
        dom.sheetFoot.innerHTML = "";
      }
    }, 320);
  }

  function ask(message, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      openSheet({
        title: opts.title || "确认一下",
        body: '<p style="margin:4px 0 10px;font-size:14px;line-height:1.75;color:var(--m-ink-2)">' + esc(message) + "</p>",
        foot:
          '<button class="m-btn is-ghost" id="m-ask-no" type="button">' + esc(opts.cancel || "算了") + "</button>" +
          '<button class="m-btn ' + (opts.danger ? "is-danger" : "is-primary") + '" id="m-ask-yes" type="button">' +
          esc(opts.ok || "确定") + "</button>",
        onOpen: function () {
          $("#m-ask-no").onclick = function () { closeSheet(); resolve(false); };
          $("#m-ask-yes").onclick = function () { closeSheet(); resolve(true); };
        },
        onClose: function () { resolve(false); },
      });
    });
  }

  /* ------------------------------------------------------------------ 皮肤 */
  function readSkin() {
    try {
      var fromQuery = new URLSearchParams(location.search).get("skin");
      if (fromQuery === "day" || fromQuery === "night") return fromQuery;
      return localStorage.getItem("congling.skin") === "day" ? "day" : "night";
    } catch (e) { return "night"; }
  }

  function paintSkin(skin, persist) {
    document.documentElement.dataset.skin = skin;
    if (dom.theme) {
      dom.theme.innerHTML = '<svg width="17" height="17"><use href="#i-' + (skin === "day" ? "sun" : "moon") + '" /></svg>';
    }
    if (dom.themeColor) dom.themeColor.setAttribute("content", skin === "day" ? "#f4f2ea" : "#070d0a");
    if (persist !== false) {
      try { localStorage.setItem("congling.skin", skin); } catch (e) { /* 忽略 */ }
    }
  }

  function toggleSkin() {
    paintSkin(readSkin() === "day" ? "night" : "day");
  }

  /* ------------------------------------------------------------------ 顶栏 */
  function paintTopbar() {
    var inSub = nav.stack.length > 0;
    if (dom.back) dom.back.classList.toggle("m-hide", !inSub);
    if (dom.brand) dom.brand.classList.toggle("m-hide", inSub);
    if (dom.pageTitle) {
      dom.pageTitle.classList.toggle("m-hide", !inSub);
      if (inSub) dom.pageTitle.textContent = nav.stack[nav.stack.length - 1].title || "";
    }
  }

  function setHealth(health, err) {
    if (!dom.health) return;
    dom.health.classList.remove("is-on", "is-off");
    if (err) {
      dom.health.classList.add("is-off");
      dom.healthText.textContent = "连不上";
      return;
    }
    var modules = (health && health.modules) || {};
    var bootErrors = (health && health.boot_errors) || [];
    var ok = !!modules.memory && !bootErrors.length;
    dom.health.classList.add(ok ? "is-on" : "is-off");
    var name = (health && health.persona && health.persona.name) || "";
    dom.healthText.textContent = ok ? (name || "在跑") : "有问题";
  }

  /* ------------------------------------------------------------------ 骨架 / 错误 */
  function skeletonHTML(count) {
    var body = "";
    for (var i = 0; i < (count || 3); i++) {
      body +=
        '<div class="m-card m-card-pad" style="margin-bottom:12px">' +
        '<div class="m-sk m-sk-line" style="width:46%"></div>' +
        '<div class="m-sk m-sk-line" style="width:88%"></div>' +
        '<div class="m-sk m-sk-line" style="width:64%"></div>' +
        "</div>";
    }
    return '<div class="m-page">' + body + "</div>";
  }

  function errorHTML(err) {
    var message = err && err.message ? err.message : String(err || "出错了");
    return (
      '<div class="m-page"><div class="m-empty"><b>没读出来</b>' +
      esc(message) +
      '<div style="margin-top:16px"><button class="m-btn" type="button" data-act="retry">再试一次</button></div>' +
      "</div></div>"
    );
  }

  /* ------------------------------------------------------------------ 页签切换 */
  function paintTabs() {
    $$(".m-tab", dom.tabbar).forEach(function (btn) {
      btn.classList.toggle("is-on", btn.dataset.tab === nav.tab && nav.stack.length === 0);
    });
  }

  function goTab(tab, force) {
    if (TABS.indexOf(tab) < 0) return;
    if (!force && nav.tab === tab && nav.stack.length === 0) {
      dom.main.scrollTop = 0;
      return;
    }
    nav.scroll[nav.tab] = dom.main.scrollTop;
    nav.tab = tab;
    nav.stack = [];
    st.dirty = {};
    paintDirty();
    paintTabs();
    paintTopbar();
    dom.main.innerHTML = skeletonHTML(tab === "home" ? 2 : 3);
    dom.main.scrollTop = 0;
    var render = VIEWS[tab];
    Promise.resolve()
      .then(function () { return render(); })
      .then(function (html) {
        if (nav.tab !== tab || nav.stack.length) return;
        dom.main.innerHTML = html;
        if (force && nav.scroll[tab]) {
          dom.main.scrollTop = 0;
        }
        paintTabs();
      })
      .catch(function (err) {
        if (nav.tab !== tab || nav.stack.length) return;
        dom.main.innerHTML = errorHTML(err);
      });
  }

  /* ------------------------------------------------------------------ 首页 */
  function homeHTML(data, nightState) {
    var health = data.health || {};
    var stats = data.stats || {};
    var recent = data.recent || [];
    var mmx = data.mmx || {};
    var night = (nightState && nightState.enabled ? nightState : null) || health.night || {};
    var persona = health.persona || {};
    var bootErrors = health.boot_errors || [];
    var modules = health.modules || {};

    var hasEnergy = night.energy != null;
    var energy = num(night.energy);
    var energyMax = num(night.energy_max, 100) || 100;
    var asleep = String(night.mode || "") === "asleep";

    var hero =
      '<section class="m-hero">' +
        '<div class="m-hero-row">' +
          "<div>" +
            '<h1 class="m-hero-name">' + txt(persona.name, "她") + "</h1>" +
            '<p class="m-hero-meta">v' + txt(health.version, "1.1.1") +
              " · 已运行 " + esc(durationText(health.uptime)) +
              " · " + (asleep ? "正在睡" : "醒着") + "</p>" +
          "</div>" +
          '<span class="m-hero-badge' + (bootErrors.length ? " is-warn" : "") + '">' +
            '<span class="m-dot"></span>' + (bootErrors.length ? bootErrors.length + " 个问题" : "正常") +
          "</span>" +
        "</div>" +
        '<div class="m-hero-stats">' +
          '<div class="m-hero-stat"><b>' + esc(String(num(data.memory_active))) + "</b><span>条记忆</span></div>" +
          '<div class="m-hero-stat"><b>' + (hasEnergy ? energy : "—") +
            (hasEnergy ? "<i style=\"font-size:12px;font-style:normal;color:var(--m-ink-4)\">/" + energyMax + "</i>" : "") +
            "</b><span>精力</span></div>" +
          '<div class="m-hero-stat"><b>' + esc(String(num(data.reply_count_recent))) + "</b><span>最近已回</span></div>" +
        "</div>" +
      "</section>";

    var quick =
      '<div class="m-quick">' +
        '<button type="button" data-act="tab" data-tab="memory"><svg><use href="#i-memory"/></svg>记忆库</button>' +
        '<button type="button" data-act="tab" data-tab="night"><svg><use href="#i-moon"/></svg>长夜</button>' +
        '<button type="button" data-act="tab" data-tab="diary"><svg><use href="#i-book"/></svg>日记</button>' +
        '<button type="button" data-act="open" data-page="settings"><svg><use href="#i-sliders"/></svg>设置</button>' +
      "</div>";

    var rows = recent.slice(0, 8).map(function (item) {
      var decided = String(item.decided || "");
      var mark = decided === "sent" ? "is-sent" : decided === "error" ? "is-error" : "is-skip";
      var label = decided === "sent" ? "已回" : decided === "skip" || decided === "silent" ? "没回" : decided || "—";
      return (
        '<div class="m-recent-item">' +
          '<span class="m-recent-time">' + esc(clockOf(item.ts)) + "</span>" +
          '<div class="m-recent-main">' +
            '<div class="m-recent-line"><span class="m-dotmark ' + mark + '"></span>' +
              '<span style="font-size:12px;color:var(--m-ink-2)">' + esc(label) + "</span>" +
              '<span class="m-tag" style="margin-left:auto">' + esc(shortId(item.session_id)) + "</span>" +
            "</div>" +
            '<div class="m-recent-why">' + txt(item.reason, "（没写原因）") + "</div>" +
          "</div>" +
        "</div>"
      );
    }).join("");

    if (!rows) rows = '<div class="m-empty">还没有决策记录，聊两句就有了</div>';

    var modKeys = ["perception", "persona", "memory", "night", "capability", "models", "qqext", "page_api"];
    var modNames = { perception: "感知", persona: "人格", memory: "记忆", night: "长夜", capability: "工具", models: "模型", qqext: "QQ", page_api: "面板" };
    var modChips = modKeys.map(function (key) {
      var on = !!modules[key];
      return '<span class="m-tag' + (on ? " is-accent" : "") + '">' + esc(modNames[key] || key) + (on ? " ✓" : " ✕") + "</span>";
    }).join(" ");

    var mmxLine = mmx.available
      ? '<div class="m-kv"><span class="m-kv-k">模型</span><span class="m-kv-v m-tag is-accent">就绪</span></div>'
      : '<div class="m-kv"><span class="m-kv-k">模型</span><span class="m-kv-v" style="color:var(--m-gold)">' +
        txt(mmx.reason || mmx.state || "未就绪", "未就绪") + "</span></div>";

    var errLine = bootErrors.length
      ? '<div class="m-kv"><span class="m-kv-k">启动问题</span><span class="m-kv-v" style="color:var(--m-danger)">' +
        esc(bootErrors.slice(0, 2).join("；")) + "</span></div>"
      : "";

    return (
      '<div class="m-page">' +
        hero +
        quick +
        '<div class="m-sec-head"><span class="m-sec-title">最近决策</span><span class="m-sec-hint">她刚才为什么回 / 不回</span></div>' +
        '<div class="m-card">' + rows + "</div>" +
        '<div class="m-sec-head"><span class="m-sec-title">运行状态</span></div>' +
        '<div class="m-card">' +
          '<div class="m-kv"><span class="m-kv-k">模块</span><span class="m-kv-v" style="display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end">' + modChips + "</span></div>" +
          mmxLine +
          '<div class="m-kv"><span class="m-kv-k">消息归档</span><span class="m-kv-v m-mono">' + esc(String(num(stats.msg_archive))) + " 条</span></div>" +
          '<div class="m-kv"><span class="m-kv-k">记忆原子</span><span class="m-kv-v m-mono">' + esc(String(num(stats.mem_atom))) + " 条</span></div>" +
          '<div class="m-kv"><span class="m-kv-k">决策日志</span><span class="m-kv-v m-mono">' + esc(String(num(stats.decision_log))) + " 条</span></div>" +
          errLine +
        "</div>" +
        footHTML() +
      "</div>"
    );
  }

  function footHTML() {
    return (
      '<div class="m-foot">从绫开始 · 移动端<br>' +
      '<a href="./index.html?desk=1">打开完整桌面面板</a> · <a href="./mobile.html?skin=' +
      (readSkin() === "day" ? "night" : "day") + '">换个肤色</a></div>'
    );
  }

  var VIEWS = {};

  VIEWS.home = function () {
    return Promise.all([
      apiGet("overview"),
      apiGet("night/state").catch(function () { return null; }),
    ]).then(function (all) {
      var data = all[0];
      st.overview = data;
      setHealth(data.health);
      return homeHTML(data, all[1]);
    }).catch(function (err) {
      setHealth(null, err);
      throw err;
    });
  };

  /* ------------------------------------------------------------------ 记忆 */
  var KIND_NAME = { fact: "事实", preference: "偏好", event: "事件", relation: "关系", emotion: "情绪", promise: "约定", plan: "计划", other: "其它" };

  /* 服务端 GROUP_TITLES 里没有 diary / proactive，会回落成英文 key */
  var GROUP_TITLE_ZH = { diary: "日记", proactive: "主动找人" };

  /* options 是裸英文字符串，界面上翻成人话 */
  var OPTION_ZH = {
    auto: "自动",
    off: "关闭",
    on: "开启",
    all: "全部",
    self: "只看自己",
    none: "不启用",
    builtin: "插件内置",
    astrbot: "跟随 AstrBot",
    force_local: "强制本地",
    force_yield: "强制让位",
    same_user: "同一用户",
    whitelist: "白名单",
    recent: "最近聊过",
    all_private: "所有私聊",
    hybrid: "混合（预生成+实时）",
    precomputed: "全部预生成",
    live: "仅实时",
    session: "按会话",
    global: "全局共享",
    never: "叫不醒",
    private_only: "仅私聊",
    both: "私聊和群聊",
    dream: "跟梦一样快",
    balanced: "平衡",
    ebbinghaus: "艾宾浩斯",
    A: "A 档 · 全功能",
    B: "B 档",
    C: "C 档",
    D: "D 档 · 最省",
    life: "生活",
    entertain: "娱乐",
    knowledge: "知识",
    game: "游戏",
    qzone: "空间",
    aiocqhttp: "aiocqhttp（OneBot v11）",
  };

  var READONLY_PATHS = {
    "persona.mode": "跟随 AstrBot，不可改",
    "persona.persona_id": "内部兼容标识，不可改",
    "persona.astrbot_persona_id": "历史兼容字段，已停用",
  };

  function memoryLoaded() {
    var m = st.memory;
    return m.items.length > 0 || m.total > 0;
  }

  function loadMemory(reset) {
    var m = st.memory;
    if (reset) {
      m.offset = 0;
      m.items = [];
    }
    var params = { limit: 30, offset: m.offset };
    if (m.session) params.session = m.session;
    if (m.keyword) params.keyword = m.keyword;
    return apiGet("memory/list", params).then(function (data) {
      m.total = num(data.total);
      m.unread = num(data.unread);
      m.tier = String(data.tier || "");
      if (reset) m.sessions = data.sessions || [];
      var items = data.items || [];
      m.items = reset ? items : m.items.concat(items);
      m.offset = m.items.length;
      return m;
    });
  }

  function memoryHTML() {
    var m = st.memory;
    var searching = !!m.search;
    var list = searching ? m.search : m.items;

    var sessionChips =
      '<div class="m-chips">' +
        '<button class="m-chip' + (m.session ? "" : " is-on") + '" type="button" data-act="mem-session" data-session="">全部会话</button>' +
        (m.sessions || []).slice(0, 12).map(function (s) {
          var id = String(s.session_id || s.id || "");
          var label = String(s.session_label || s.label || id);
          var count = num(s.count || s.total);
          return '<button class="m-chip' + (m.session === id ? " is-on" : "") + '" type="button" data-act="mem-session" data-session="' +
            esc(id) + '">' + esc(clip(label, 12)) + (count ? " " + count : "") + "</button>";
        }).join("") +
      "</div>";

    var cards = list.map(function (item, index) {
      var kind = String(item.kind || "");
      var level = num(item.mem_level, 3);
      var score = num(item.importance, 0.5);
      var text = item.text || item.content || "";
      return (
        '<button class="m-mem" type="button" data-act="mem-open" data-index="' + index + '">' +
          '<span class="m-mem-top">' +
            '<span class="m-tag is-accent">' + esc(KIND_NAME[kind] || kind || "记忆") + "</span>" +
            '<span class="m-tag' + (level <= 2 ? " is-gold" : "") + '">L' + esc(String(level)) + "</span>" +
            (item.status && item.status !== "active" ? '<span class="m-tag">' + esc(item.status) + "</span>" : "") +
            (num(item.unread) ? '<span class="m-tag is-rose">未看</span>' : "") +
            '<span style="margin-left:auto;font-size:11px;color:var(--m-ink-4)">' + esc(item.created_text || agoOf(item.created_at)) + "</span>" +
          "</span>" +
          '<span class="m-mem-text">' + esc(searching ? (item.text || item.content || "") : text) + "</span>" +
          '<span class="m-mem-foot"><span class="m-spacer"></span>' +
            '<span>强度 ' + esc(String(Math.round(score * 100))) + "%</span>" +
            "<span>命中 " + esc(String(num(item.hit_count))) + "</span>" +
          "</span>" +
        "</button>"
      );
    }).join("");

    var more = "";
    if (!searching) {
      if (m.items.length < m.total) {
        more = '<button class="m-btn is-block" type="button" data-act="mem-more" style="margin-top:10px">再看 ' +
          esc(String(Math.min(30, Math.max(0, m.total - m.items.length)))) + " 条</button>";
      } else if (m.items.length) {
        more = '<div class="m-foot">已经到底了 · 共 ' + esc(String(m.total)) + " 条</div>";
      }
    }

    if (!list.length) {
      cards = '<div class="m-empty"><b>' + (searching ? "没搜到" : "还没有记忆") + "</b>" +
        (searching ? "换个词试试" : "聊几句，她会慢慢记住的") + "</div>";
    }

    return (
      '<div class="m-page">' +
        '<div class="m-searchbar">' +
          '<svg><use href="#i-search"/></svg>' +
          '<input id="m-mem-search" type="search" enterkeyhint="search" placeholder="搜记忆：关键词、人名、事情…" value="' + esc(m.keyword) + '" />' +
          (m.search ? '<button class="m-tag" type="button" data-act="mem-clear-search">清空</button>' : "") +
        "</div>" +
        (searching ? "" : sessionChips) +
        '<div class="m-sec-head"><span class="m-sec-title">' + (searching ? "搜索结果" : "记忆库") + "</span>" +
          '<span class="m-sec-hint">' + (searching ? esc(String(list.length)) + " 条命中" : "共 " + esc(String(m.total)) + " 条") +
          (m.tier ? " · 档位 " + esc(m.tier) : "") + "</span></div>" +
        '<div style="display:flex;flex-direction:column;gap:8px">' + cards + "</div>" +
        more +
        footHTML() +
      "</div>"
    );
  }

  function memorySearch(query) {
    if (!query) {
      st.memory.search = null;
      st.memory.keyword = "";
      return Promise.resolve();
    }
    return apiPost("memory/search", { query: query, k: 12 }).then(function (data) {
      st.memory.search = data.items || [];
      st.memory.keyword = query;
    });
  }

  VIEWS.memory = function () {
    return loadMemory(true).then(memoryHTML);
  };

  function memorySheet(item) {
    var level = num(item.mem_level, 3);
    var levels = [0, 1, 2, 3, 4, 5].map(function (lv) {
      return '<button class="m-chip' + (lv === level ? " is-on" : "") + '" type="button" data-act="mem-level" data-level="' + lv + '">' + lv + "</button>";
    }).join("");

    var body =
      '<div class="m-field" style="padding-top:0">' +
        '<div class="m-field-label">内容</div>' +
        '<textarea class="m-textarea" id="m-mem-text">' + esc(item.text || item.content || "") + "</textarea>" +
      "</div>" +
      '<div class="m-field"><div class="m-field-label">重要性 <span style="margin-left:auto;font-family:var(--m-mono);font-size:12px;color:var(--m-ink-3)">' +
        esc(String(Math.round(num(item.importance, 0.5) * 100))) + "%</span></div>" +
        '<div class="m-bar"><i style="width:' + pct(item.importance, 1) + '%"></i></div></div>' +
      '<div class="m-field"><div class="m-field-label">记忆等级</div>' +
        '<div class="m-chips">' + levels + "</div>" +
        '<div class="m-field-hint">0 级不会被忘掉；等级越高越常被想起</div></div>' +
      '<div class="m-card" style="margin:8px 0 0">' +
        '<div class="m-kv"><span class="m-kv-k">类型</span><span class="m-kv-v">' + esc(KIND_NAME[String(item.kind)] || item.kind || "—") + "</span></div>" +
        '<div class="m-kv"><span class="m-kv-k">会话</span><span class="m-kv-v">' + txt(item.session_label || item.session_id) + "</span></div>" +
        '<div class="m-kv"><span class="m-kv-k">写入</span><span class="m-kv-v m-mono">' + txt(item.created_text || agoOf(item.created_at)) + "</span></div>" +
        '<div class="m-kv"><span class="m-kv-k">命中次数</span><span class="m-kv-v m-mono">' + esc(String(num(item.hit_count))) + "</span></div>" +
        (item.keywords && item.keywords.length ? '<div class="m-kv"><span class="m-kv-k">关键词</span><span class="m-kv-v">' + esc(item.keywords.join(" / ")) + "</span></div>" : "") +
      "</div>";

    openSheet({
      title: "记忆 #" + esc(String(item.id || "?")),
      body: body,
      foot:
        '<button class="m-btn is-danger" type="button" data-act="mem-del" data-id="' + esc(String(item.id)) + '">忘掉它</button>' +
        '<button class="m-btn is-primary" type="button" data-act="mem-save" data-id="' + esc(String(item.id)) + '">保存修改</button>',
      onOpen: function (root) {
        $$("[data-level]", root).forEach(function (btn) {
          btn.addEventListener("click", function () {
            $$("[data-level]", root).forEach(function (b) { b.classList.remove("is-on"); });
            btn.classList.add("is-on");
          });
        });
      },
    });
  }

  /* ------------------------------------------------------------------ 长夜 */
  function nightModeText(snapshot) {
    var mode = String(snapshot.mode || "");
    if (mode === "asleep") return "睡着了";
    if (mode === "waking") return "刚要醒";
    return "醒着";
  }

  function nightHTML() {
    var s = st.night.state || {};
    if (!s.enabled) {
      return (
        '<div class="m-page"><div class="m-empty"><b>长夜如梦没开</b>' +
        "开了之后她会累、会困、会睡，睡着期间的消息要「爬楼」才能看到。<br>梦里只留 REM 期那几段。" +
        '<div style="margin-top:16px"><button class="m-btn is-primary" type="button" data-act="open" data-page="settings-group" data-group="night">去打开</button></div>' +
        "</div>" + footHTML() + "</div>"
      );
    }

    var energy = num(s.energy);
    var max = num(s.energy_max, 100) || 100;
    var p = pct(energy, max);
    var circumference = 2 * Math.PI * 56;
    var dash = circumference * (1 - p / 100);
    var asleep = String(s.mode) === "asleep";
    var desc = asleep
      ? (s.stage_text || "正在睡") + (s.remaining_text ? " · 还有 " + s.remaining_text : "")
      : (s.fatigue === "night" || s.fatigue === "noon" ? "在疲劳时段里，随时可能犯困" : "现在不困");
    var wakeLine = s.wake_label || s.wake_at_clock || s.wake_window || s.wake_time || "—";

    var stagePills = "";
    var plan = s.stage_seq || [];
    if (plan.length) {
      var nowKey = s.stage && s.stage.key;
      stagePills = '<div class="m-stage-list">' + plan.slice(0, 18).map(function (seg) {
        var cls = seg.key === nowKey && !seg.done ? " is-now" : seg.done ? " is-past" : "";
        return '<i class="m-stage-pill' + cls + '" title="' + esc(seg.label || "") + '"></i>';
      }).join("") + "</div>";
    }

    var ring =
      '<div class="m-night-ring' + (p <= 30 ? " is-low" : "") + '">' +
        '<svg viewBox="0 0 132 132">' +
          '<circle class="m-ring-bg" cx="66" cy="66" r="56"></circle>' +
          '<circle class="m-ring-fg" cx="66" cy="66" r="56" stroke-dasharray="' + circumference.toFixed(1) + '" stroke-dashoffset="' + dash.toFixed(1) + '"></circle>' +
        "</svg>" +
        '<div class="m-night-ring-inner"><b>' + esc(String(Math.round(energy))) + "</b><span>精力 / " + esc(String(max)) + "</span></div>" +
      "</div>";

    var cards =
      '<section class="m-card">' +
        '<div class="m-night-body">' + ring +
          '<div class="m-night-info">' +
            '<div class="m-night-mode">' + esc(nightModeText(s)) + "</div>" +
            '<div class="m-night-desc">' + esc(desc) + "</div>" +
            '<div class="m-night-desc">下一次醒：' + esc(String(wakeLine)) + "</div>" +
            stagePills +
          "</div>" +
        "</div>" +
        '<div style="display:flex;gap:9px;padding:0 16px 16px">' +
          '<button class="m-btn" type="button" data-act="night-sleep">' + (asleep ? "继续睡" : "让她睡") + "</button>" +
          '<button class="m-btn" type="button" data-act="night-wake">叫醒</button>' +
          '<button class="m-btn is-primary" type="button" data-act="night-dream">做一段梦</button>' +
        "</div>" +
      "</section>";

    var kv =
      '<div class="m-card">' +
        '<div class="m-kv"><span class="m-kv-k">作息</span><span class="m-kv-v">' + esc(String(s.wake_time || "08:00")) + " 起 · " + esc(String(s.latest_sleep_time || "02:30")) + " 最晚睡</span></div>" +
        '<div class="m-kv"><span class="m-kv-k">疲劳时段</span><span class="m-kv-v">' + esc(String(s.night_fatigue || "—")) + (s.noon_fatigue ? " / " + esc(String(s.noon_fatigue)) : "") + "</span></div>" +
        '<div class="m-kv"><span class="m-kv-k">做梦</span><span class="m-kv-v">' + (s.dream_on ? "开着 · 每 " + esc(String(num(s.dream_interval_minutes, 60))) + " 分钟一段" : "关着") + "</span></div>" +
        '<div class="m-kv"><span class="m-kv-k">爬楼</span><span class="m-kv-v">' + (s.crawl_on ? "开着" : "关着") + (num(s.pending_crawl) ? " · 待爬 " + esc(String(num(s.pending_crawl))) + " 个会话" : "") + "</span></div>" +
        '<div class="m-kv"><span class="m-kv-k">叫醒档位</span><span class="m-kv-v">' + esc(String(s.wake_mode || "—")) + "</span></div>" +
      "</div>";

    var d = st.night.dreams || { nights: [], items: [] };
    var nights = d.nights || [];
    var dreamHTML = nights.length
      ? nights.slice(0, 6).map(function (group) {
          var segs = (group.segments || []).map(function (dream) {
            return (
              '<div class="m-dream">' +
                '<div class="m-dream-text">' + esc(dream.text || "") + "</div>" +
                '<div class="m-dream-foot">' +
                  '<span class="m-tag">' + esc(String(dream.strength_text || Math.round(num(dream.strength, 1) * 100) + "%")) + "</span>" +
                  (dream.pinned ? '<span class="m-tag is-gold">拽住了</span>' : "") +
                  '<span>还剩 ' + esc(String(num(dream.remaining_days))) + " 天</span>" +
                  '<span style="margin-left:auto;display:flex;gap:6px">' +
                    '<button class="m-tag" type="button" data-act="dream-pin" data-id="' + esc(String(dream.dream_id)) + '" data-pinned="' + (dream.pinned ? "1" : "0") + '">' + (dream.pinned ? "放开" : "拽住") + "</button>" +
                    '<button class="m-tag" type="button" data-act="dream-del" data-id="' + esc(String(dream.dream_id)) + '">删</button>' +
                  "</span>" +
                "</div>" +
              "</div>"
            );
          }).join("");
          return (
            '<div class="m-card" style="margin-bottom:10px">' +
              '<div class="m-card-head"><h2>' + esc(group.night_key || "那一夜") + "</h2>" +
                '<span class="m-sub">' + esc(String((group.segments || []).length)) + " 段</span></div>" +
              segs +
            "</div>"
          );
        }).join("")
      : '<div class="m-empty">她还没做过梦<br>梦里只会用到真实记过的片段</div>';

    var u = st.night.unread || { groups: [], total: 0 };
    var unreadHTML = (u.groups || []).length
      ? (u.groups || []).map(function (g) {
          return (
            '<button class="m-item" type="button" data-act="night-crawl" data-session="' + esc(String(g.session_id)) + '">' +
              '<span class="m-item-main">' +
                '<span class="m-item-title">' + esc(String(g.label || g.session_id)) + "</span>" +
                '<span class="m-item-sub">睡着期间攒了 ' + esc(String(num(g.count))) + " 条" +
                  (g.since ? " · 从 " + esc(agoOf(g.since)) : "") + "</span>" +
              "</span>" +
              '<span class="m-tag is-rose">去爬</span>' +
            "</button>"
          );
        }).join("")
      : '<div class="m-empty">没有攒着的消息</div>';

    return (
      '<div class="m-page">' +
        cards +
        kv +
        '<div class="m-sec-head"><span class="m-sec-title">她的梦</span><span class="m-sec-hint">按夜分组 · 睡着后只在 REM 做</span></div>' +
        dreamHTML +
        '<div class="m-sec-head"><span class="m-sec-title">睡着时的消息</span><span class="m-sec-hint">共 ' + esc(String(num(u.total))) + " 条没爬</span></div>" +
        '<div class="m-card">' + unreadHTML + "</div>" +
        footHTML() +
      "</div>"
    );
  }

  VIEWS.night = function () {
    return Promise.all([
      apiGet("night/state").catch(function () { return { enabled: false }; }),
      apiGet("night/dreams", { limit: 20 }).catch(function () { return { nights: [], items: [] }; }),
      apiGet("night/unread", { limit: 20 }).catch(function () { return { groups: [], total: 0 }; }),
    ]).then(function (all) {
      st.night.state = all[0] || {};
      st.night.dreams = all[1] || {};
      st.night.unread = all[2] || {};
      var badge = dom.badgeNight;
      if (badge) {
        var total = num(st.night.unread.total);
        if (total > 0) {
          badge.textContent = total > 99 ? "99+" : String(total);
          badge.classList.remove("m-hide");
        } else {
          badge.classList.add("m-hide");
        }
      }
      return nightHTML();
    });
  };

  /* ------------------------------------------------------------------ 日记 */
  function diaryHTML() {
    var list = st.diary.items || [];
    var items = list.map(function (item) {
      var tags = (item.tags || []).slice(0, 3).map(function (tag) {
        return '<span class="m-tag is-violet">' + esc(tag) + "</span>";
      }).join("");
      return (
        '<button class="m-diary-item" type="button" data-act="diary-open" data-id="' + esc(String(item.id)) + '" data-day="' + esc(String(item.day_key)) + '">' +
          '<span class="m-diary-date">' + esc(String(item.day_label || item.day_key)) +
            (item.pinned ? ' <span class="m-tag is-gold">置顶</span>' : "") +
            '<span style="margin-left:auto">' + esc(String(num(item.chars))) + " 字</span></span>" +
          '<div class="m-diary-title">' + txt(item.title, "无题") + "</div>" +
          '<div class="m-diary-excerpt">' + esc(clip(item.text, 90)) + "</div>" +
          (tags ? '<div class="m-diary-tags">' + tags + "</div>" : "") +
        "</button>"
      );
    }).join("");

    if (!items) {
      items = '<div class="m-empty"><b>还没有日记</b>' +
        (st.diary.enabled ? "到点会自己写，也可以现在让她写一篇" : "日记总开关关着，先去设置里打开") + "</div>";
    }

    return (
      '<div class="m-page">' +
        '<section class="m-card"><div class="m-card-pad" style="display:flex;align-items:center;gap:12px">' +
          "<div style=\"flex:1 1 auto\">" +
            '<div style="font-family:var(--m-serif);font-size:19px;font-weight:600">今天的日记</div>' +
            '<div style="margin-top:4px;font-size:12px;color:var(--m-ink-3)">' +
              (st.diary.enabled ? "每天 " + esc(String(num(st.diary.hour, 23))) + " 点左右自己写" : "总开关关着") +
            "</div>" +
          "</div>" +
          '<button class="m-btn is-primary" type="button" data-act="diary-write">写一篇</button>' +
        "</div></section>" +
        '<div class="m-sec-head"><span class="m-sec-title">日记本</span><span class="m-sec-hint">共 ' + esc(String(list.length)) + " 篇</span></div>" +
        '<div class="m-card">' + items + "</div>" +
        footHTML() +
      "</div>"
    );
  }

  VIEWS.diary = function () {
    return apiGet("diary/list", { limit: 40 }).then(function (data) {
      st.diary.items = data.items || [];
      st.diary.enabled = !!data.enabled;
      st.diary.hour = data.hour;
      return diaryHTML();
    });
  };

  function diaryReader(row) {
    var paragraphs = splitParagraphs(row.text).map(function (p) { return "<p>" + esc(p) + "</p>"; }).join("");
    var meta = [
      row.day_label || row.day_key,
      row.created_text ? "写于 " + row.created_text : "",
      row.mood ? "心情 " + row.mood : "",
      num(row.chars) + " 字",
    ].filter(Boolean).join(" · ");

    openSheet({
      title: row.day_label || row.day_key || "日记",
      body:
        '<div class="m-reader">' +
          '<div class="m-reader-head">' +
            '<div class="m-diary-date">' + esc(String(row.day_key || "")) + "</div>" +
            "<h2>" + txt(row.title, "无题") + "</h2>" +
            '<div class="m-reader-meta">' + esc(meta) + "</div>" +
            ((row.tags || []).length ? '<div class="m-diary-tags">' + row.tags.map(function (t) { return '<span class="m-tag is-violet">' + esc(t) + "</span>"; }).join("") + "</div>" : "") +
          "</div>" +
          paragraphs +
        "</div>",
      foot:
        '<button class="m-btn is-danger" type="button" data-act="diary-del" data-id="' + esc(String(row.id)) + '">删掉</button>' +
        '<button class="m-btn is-primary" type="button" data-act="diary-pin" data-id="' + esc(String(row.id)) + '" data-pinned="' + (row.pinned ? "1" : "0") + '">' +
          (row.pinned ? "取消置顶" : "置顶") + "</button>",
    });
  }

  /* ------------------------------------------------------------------ 更多 */
  function moreHTML() {
    var health = (st.overview && st.overview.health) || {};
    var persona = health.persona || {};

    function row(icon, name, desc, action, extra) {
      return (
        '<button class="m-nav-row" type="button" ' + action + ">" +
          '<span class="m-nav-ico"><svg><use href="#i-' + icon + '"/></svg></span>' +
          '<span class="m-nav-txt"><span class="m-nav-name">' + esc(name) + "</span>" +
            (desc ? '<span class="m-nav-desc">' + esc(desc) + "</span>" : "") + "</span>" +
          (extra ? '<span class="m-nav-val">' + esc(extra) + "</span>" : '<svg class="m-chev" width="15" height="15"><use href="#i-chevron"/></svg>') +
        "</button>"
      );
    }

    return (
      '<div class="m-page">' +
        '<section class="m-card m-card-pad" style="display:flex;align-items:center;gap:12px">' +
          '<span class="m-nav-ico" style="width:40px;height:40px;border-radius:12px"><svg><use href="#i-user"/></svg></span>' +
          '<div style="flex:1 1 auto;min-width:0">' +
            '<div style="font-family:var(--m-serif);font-size:18px;font-weight:600">' + txt(persona.name, "她") + "</div>" +
            '<div style="font-size:11.5px;color:var(--m-ink-4)">v' + txt(health.version, "1.1.1") + " · 已运行 " + esc(durationText(health.uptime)) + "</div>" +
          "</div>" +
          '<button class="m-btn" type="button" data-act="open" data-page="persona">看人格</button>' +
        "</section>" +

        '<div class="m-group-head">调她</div>' +
        '<div class="m-card">' +
          row("user", "人格", "名字、身份设定、风格与禁忌", 'data-act="open" data-page="persona"') +
          row("sliders", "设置", "全部配置项，按组翻", 'data-act="open" data-page="settings"') +
          row("tool", "工具", "实用查询能力与试跑", 'data-act="open" data-page="tools"') +
          row("heart", "好感度", "按记忆和频次结算的关系分数", 'data-act="open" data-page="affinity"') +
          row("mail", "主动找人", "谁会收到、今天发了几条", 'data-act="open" data-page="proactive"') +
        "</div>" +

        '<div class="m-group-head">看状态</div>' +
        '<div class="m-card">' +
          row("qzone", "QQ 空间", "登录态、说说、发一条", 'data-act="open" data-page="qzone"') +
          row("pulse", "诊断", "最近一条决策轨迹，排查不生效", 'data-act="open" data-page="diag"') +
          row("desktop", "桌面面板", "功能更全，适合电脑上用", 'data-act="open-desktop"') +
        "</div>" +

        '<div class="m-group-head">关于</div>' +
        '<div class="m-card">' +
          row("spark", "版本", "从绫开始 v" + txt(health.version, "1.1.1") + " · 测试版", 'data-act="open" data-page="about"') +
        "</div>" +
        footHTML() +
      "</div>"
    );
  }

  VIEWS.more = function () {
    return apiGet("overview").then(function (data) {
      st.overview = data;
      setHealth(data.health);
      return moreHTML();
    });
  };

  /* ------------------------------------------------------------------ 子页：人格 */
  var PAGES = {};

  PAGES.persona = {
    title: "人格",
    render: function () {
      return apiGet("persona").then(function (data) {
        st.persona = data;
        var style = data.style || {};
        var styleText = Object.keys(style).map(function (key) {
          return key + "：" + (typeof style[key] === "object" ? JSON.stringify(style[key]) : style[key]);
        }).join("\n");
        var versions = (data.versions || []).slice(0, 5).map(function (item, index) {
          return (
            '<div class="m-kv"><span class="m-kv-k">v' + esc(String(index + 1)) + "</span>" +
              '<span class="m-kv-v">' + esc(item.at_text || item.name || agoOf(item.ts || item.at)) + " " +
              '<button class="m-tag" type="button" data-act="persona-rollback" data-index="' + index + '">回到这版</button></span></div>'
          );
        }).join("");

        return (
          '<div class="m-page">' +
            '<div class="m-card">' +
              '<div class="m-field"><div class="m-field-label">名字</div>' +
                '<input class="m-input" id="m-persona-name" value="' + esc(data.name || "") + '" placeholder="她叫什么" /></div>' +
              '<div class="m-field"><div class="m-field-label">身份设定</div>' +
                '<textarea class="m-textarea" id="m-persona-identity" style="min-height:220px" placeholder="她是谁、怎么说话">' + esc(data.identity || "") + "</textarea>" +
                '<div class="m-field-hint">实际人设由 AstrBot 当前人格提供；这里改的是本插件保存的兼容设定</div></div>' +
              '<div class="m-field"><div class="m-field-label">活跃时段</div>' +
                '<input class="m-input" id="m-persona-hours" value="' + esc(data.active_hours || "") + '" placeholder="09:00-03:30" /></div>' +
              '<div class="m-field"><div class="m-field-label">风格（只读）</div>' +
                '<div class="m-field-hint" style="white-space:pre-wrap;font-family:var(--m-mono);font-size:11.5px">' + esc(styleText || "（空）") + "</div></div>" +
            "</div>" +
            '<button class="m-btn is-primary is-block" type="button" data-act="persona-save">保存人格</button>' +
            (versions ? '<div class="m-sec-head"><span class="m-sec-title">历史版本</span></div><div class="m-card">' + versions + "</div>" : "") +
          "</div>"
        );
      });
    },
  };

  /* ------------------------------------------------------------------ 子页：设置 */
  PAGES.settings = {
    title: "设置",
    render: function () {
      var cached = st.settings;
      var promise = cached ? Promise.resolve(cached) : apiGet("settings").then(function (data) {
        st.settings = data;
        return data;
      });
      return promise.then(function (data) {
        var groups = (data.groups || []).map(function (group) {
          var items = group.items || [];
          var title = group.title && group.title !== group.name ? group.title : (GROUP_TITLE_ZH[group.name] || group.title || group.name);
          return (
            '<button class="m-nav-row" type="button" data-act="open" data-page="settings-group" data-group="' + esc(group.name) + '">' +
              '<span class="m-nav-ico"><svg><use href="#i-sliders"/></svg></span>' +
              '<span class="m-nav-txt"><span class="m-nav-name">' + esc(title) + "</span>" +
                '<span class="m-nav-desc">' + esc(clip(group.hint || group.desc, 46)) + "</span></span>" +
              '<span class="m-nav-val">' + esc(String(items.length)) + " 项</span>" +
            "</button>"
          );
        }).join("");

        return (
          '<div class="m-page">' +
            '<div class="m-empty" style="padding:10px 4px 0;text-align:left;font-size:12px">改完记得点保存；部分项要重载插件才完全生效</div>' +
            '<div class="m-card">' + groups + "</div>" +
            footHTML() +
          "</div>"
        );
      });
    },
  };

  PAGES["settings-group"] = {
    title: "设置",
    render: function (params) {
      return PAGES.settings.render().then(function () {
        var data = st.settings || {};
        var group = (data.groups || []).filter(function (g) { return g.name === params.group; })[0];
        if (!group) throw new Error("没有这一组配置");
        PAGES["settings-group"].title =
          group.title && group.title !== group.name ? group.title : (GROUP_TITLE_ZH[group.name] || group.name);
        return settingsGroupHTML(group);
      });
    },
  };

  function controlFor(item, path) {
    var type = String(item.type || "string");
    var value = item.value;
    var options = item.options || [];

    if (READONLY_PATHS[path]) {
      return '<span style="font-size:12px;color:var(--m-ink-4)">' + esc(String(value == null || value === "" ? "—" : value)) + "</span>";
    }

    if (type === "bool") {
      return '<button class="m-switch' + (value ? " is-on" : "") + '" type="button" role="switch" aria-checked="' + (value ? "true" : "false") +
        '" data-ctl="bool" data-path="' + esc(path) + '"></button>';
    }
    if (type === "int" || type === "float") {
      var step = type === "float" ? "any" : "1";
      return '<input class="m-set-num" type="number" inputmode="decimal" step="' + step + '" data-ctl="num" data-type="' + type +
        '" data-path="' + esc(path) + '" value="' + esc(String(value == null ? "" : value)) + '" />';
    }
    if (options.length) {
      return '<select class="m-select" style="width:auto;min-width:110px" data-ctl="select" data-path="' + esc(path) + '">' +
        options.map(function (opt) {
          var ov = typeof opt === "object" ? (opt.value != null ? opt.value : opt) : opt;
          var label = typeof opt === "object" ? (opt.label || ov) : opt;
          var zh = OPTION_ZH[String(ov)];
          return '<option value="' + esc(String(ov)) + '"' + (String(ov) === String(value) ? " selected" : "") + ">" +
            esc(zh || String(label)) + "</option>";
        }).join("") + "</select>";
    }
    if (type === "text") {
      return '<textarea class="m-textarea" style="min-height:120px" data-ctl="text" data-path="' + esc(path) + '">' + esc(value == null ? "" : String(value)) + "</textarea>";
    }
    if (type === "list") {
      var lines = Array.isArray(value) ? value.join("\n") : String(value == null ? "" : value);
      return '<textarea class="m-textarea" style="min-height:96px;font-family:var(--m-mono);font-size:13px" data-ctl="list" data-path="' +
        esc(path) + '" placeholder="一行一项">' + esc(lines) + "</textarea>";
    }
    return '<input class="m-input" style="width:auto;min-width:130px" data-ctl="str" data-path="' + esc(path) +
      '" value="' + esc(value == null ? "" : String(value)) + '" />';
  }

  function isWide(item) {
    var type = String(item.type || "");
    if (type === "text" || type === "list") return true;
    if (type === "string" && !(item.options || []).length) return true;
    var options = item.options || [];
    if (options.length) {
      var longest = options.reduce(function (max, opt) {
        var zh = OPTION_ZH[String(opt)] || String(opt);
        return Math.max(max, zh.length);
      }, 0);
      if (longest > 8) return true;
    }
    return false;
  }

  function settingsGroupHTML(group) {
    var rows = (group.items || []).map(function (item) {
      var path = item.path;
      var wide = isWide(item) || !!READONLY_PATHS[path];
      var control = controlFor(item, path);
      var label = txt(item.label, item.key);
      var special = item.special ? ' <span class="m-tag is-violet">' + esc(item.special) + "</span>" : "";
      if (READONLY_PATHS[path]) special += ' <span class="m-tag">只读</span>';
      var hint = READONLY_PATHS[path] || item.hint;
      if (wide) {
        return (
          '<div class="m-field">' +
            '<div class="m-field-label">' + label + special + "</div>" +
            control +
            (hint ? '<div class="m-field-hint">' + esc(hint) + "</div>" : "") +
          "</div>"
        );
      }
      return (
        '<div class="m-set-row">' +
          '<div class="m-set-txt"><div class="m-set-name">' + label + special + "</div>" +
            (hint ? '<div class="m-set-hint">' + esc(hint) + "</div>" : "") + "</div>" +
          '<div class="m-set-control">' + control + "</div>" +
        "</div>"
      );
    }).join("");

    return (
      '<div class="m-page is-form">' +
        (group.hint ? '<div class="m-empty" style="padding:8px 4px 0;text-align:left;font-size:12px;line-height:1.7">' + esc(group.hint) + "</div>" : "") +
        '<div class="m-card">' + rows + "</div>" +
        '<div class="m-foot" style="padding:14px 8px 4px">这一组共 ' + esc(String((group.items || []).length)) + " 项</div>" +
        '<div style="padding:16px 0 6px">' +
          '<button class="m-btn is-primary is-block" type="button" data-act="settings-save">保存这一组' +
            '<span id="m-dirty-count" style="font-size:12px;opacity:.8"></span></button>' +
        "</div>" +
      "</div>"
    );
  }

  function paintDirty() {
    var n = Object.keys(st.dirty).length;
    var count = $("#m-dirty-count");
    if (count) count.textContent = n ? "（" + n + " 项已改）" : "";

    var top = nav.stack[nav.stack.length - 1];
    var inForm = !!top && top.name === "settings-group";
    var dock = $("#m-savedock");
    if (!dock) return;
    var show = n > 0 && inForm;
    dock.classList.toggle("m-hide", !show);
    var dc = $("#m-dock-count");
    if (dc) dc.textContent = n + " 项";
  }

  function saveSettings() {
    var changes = st.dirty;
    if (!Object.keys(changes).length) {
      toast("没有改动");
      return;
    }
    apiPost("settings/save", { changes: changes }).then(function (data) {
      st.dirty = {};
      paintDirty();
      st.settings = null;
      toast((data.note || "已保存") + "（" + num(data.applied) + " 项）", "ok");
    }).catch(function (err) {
      toast(err.message || "保存失败", "err");
    });
  }

  /* ------------------------------------------------------------------ 子页：工具 */
  PAGES.tools = {
    title: "工具",
    render: function () {
      return apiGet("tools").then(function (data) {
        var items = data.items || [];
        if (!items.length) return '<div class="m-page"><div class="m-empty">工具模块没就绪</div></div>';
        var rows = items.map(function (item) {
          var name = String(item.name || item.id || "");
          var enabled = item.enabled !== false;
          return (
            '<div class="m-item">' +
              '<span class="m-item-main">' +
                '<span class="m-item-title">' + esc(item.title || item.description || name) +
                  (enabled ? "" : ' <span class="m-tag">关</span>') + "</span>" +
                '<span class="m-item-sub">' + esc(String(name)) + (item.group ? " · " + esc(String(item.group)) : "") + "</span>" +
              "</span>" +
              '<button class="m-tag" type="button" data-act="tool-run" data-name="' + esc(name) + '">试跑</button>' +
            "</div>"
          );
        }).join("");
        return '<div class="m-page"><div class="m-card">' + rows + "</div></div>";
      });
    },
  };

  /* ------------------------------------------------------------------ 子页：好感度 */
  PAGES.affinity = {
    title: "好感度",
    render: function () {
      return apiGet("affinity").then(function (data) {
        var names = data.names || [];
        var ranking = data.ranking || [];
        var rows = ranking.map(function (item) {
          var stage = num(item.stage);
          return (
            '<div class="m-item">' +
              '<span class="m-item-main"><span class="m-item-title">' + esc(String(item.name || item.subject_key)) +
                ' <span class="m-tag is-accent">' + esc(String(item.stage_name || (names[stage] || ""))) + "</span></span>" +
                '<span class="m-item-sub">累计 ' + esc(String(num(item.score))) + " 分</span></span>" +
              '<span class="m-item-side" style="align-self:center">' + esc(String(names[stage] || "")) + "</span>" +
            "</div>"
          );
        }).join("");
        return (
          '<div class="m-page">' +
            '<div class="m-empty" style="padding:6px 4px 0;text-align:left;font-size:12px">' +
              "阶段：" + esc((names || []).join(" → ")) + "<br>只统计已确认的主体；手改是否冻结看设置里 memory.affinity 那组</div>" +
            '<div class="m-card">' + (rows || '<div class="m-empty">还没有人上榜</div>') + "</div>" +
          "</div>"
        );
      });
    },
  };

  /* ------------------------------------------------------------------ 子页：主动找人 */
  PAGES.proactive = {
    title: "主动找人",
    render: function () {
      return Promise.all([
        apiGet("proactive/state").catch(function () { return {}; }),
        apiGet("proactive/sessions").catch(function () { return { items: [] }; }),
        apiGet("proactive/log", { limit: 20 }).catch(function () { return { items: [] }; }),
      ]).then(function (all) {
        var state = all[0] || {};
        var sessions = (all[1] && all[1].items) || [];
        var logs = (all[2] && all[2].items) || [];

        var sRows = sessions.slice(0, 20).map(function (item) {
          var sid = String(item.session_id || item.id || "");
          return (
            '<div class="m-item">' +
              '<span class="m-item-main"><span class="m-item-title">' + esc(String(item.label || sid)) + "</span>" +
                '<span class="m-item-sub">' + esc(clip(item.reason || item.note || "", 60) || "可以主动") + "</span></span>" +
              '<button class="m-tag" type="button" data-act="proactive-now" data-session="' + esc(sid) + '">现在发</button>' +
            "</div>"
          );
        }).join("");

        var lRows = logs.slice(0, 12).map(function (item) {
          return (
            '<div class="m-recent-item">' +
              '<span class="m-recent-time">' + esc(clockOf(item.ts || item.at)) + "</span>" +
              '<div class="m-recent-main"><div class="m-recent-line">' +
                '<span style="font-size:12px;color:var(--m-ink-2)">' + esc(String(item.action || item.status || "发")) + "</span>" +
                '<span class="m-tag" style="margin-left:auto">' + esc(shortId(item.session_id)) + "</span></div>" +
                '<div class="m-recent-why">' + txt(item.text || item.reason, "") + "</div></div>" +
            "</div>"
          );
        }).join("");

        return (
          '<div class="m-page">' +
            '<div class="m-card"><div class="m-kv"><span class="m-kv-k">总开关</span><span class="m-kv-v">' +
              (state.enabled ? "开着" : "关着") + "</span></div>" +
              '<div class="m-kv"><span class="m-kv-k">今天已发</span><span class="m-kv-v m-mono">' + esc(String(num(state.sent_today))) + "</span></div>" +
              '<div class="m-kv"><span class="m-kv-k">每日上限</span><span class="m-kv-v m-mono">' + esc(String(num(state.daily_cap))) + "</span></div>" +
            "</div>" +
            '<div class="m-sec-head"><span class="m-sec-title">会收到的人</span></div>' +
            '<div class="m-card">' + (sRows || '<div class="m-empty">没有可主动的会话</div>') + "</div>" +
            '<div class="m-sec-head"><span class="m-sec-title">最近记录</span></div>' +
            '<div class="m-card">' + (lRows || '<div class="m-empty">还没有记录</div>') + "</div>" +
          "</div>"
        );
      });
    },
  };

  /* ------------------------------------------------------------------ 子页：空间 */
  PAGES.qzone = {
    title: "QQ 空间",
    render: function () {
      return apiGet("qzone").catch(function (err) { return { error: err.message }; }).then(function (data) {
        if (data.error) {
          return '<div class="m-page"><div class="m-empty"><b>空间没接上</b>' + esc(data.error) +
            '<div style="margin-top:14px"><button class="m-btn" type="button" data-act="qzone-auth">试着登录</button></div></div></div>';
        }
        var ok = data.ok !== false && (data.usable !== false);
        var feed = data.feed || data.items || [];
        var feedRows = (Array.isArray(feed) ? feed : []).slice(0, 10).map(function (item) {
          var text = item.content || item.text || "";
          return '<div class="m-dream"><div class="m-dream-text" style="-webkit-line-clamp:3">' + esc(clip(text, 160)) + "</div>" +
            '<div class="m-dream-foot"><span>' + esc(item.createTime || item.time || "") + "</span></div></div>";
        }).join("");
        return (
          '<div class="m-page">' +
            '<div class="m-card">' +
              '<div class="m-kv"><span class="m-kv-k">登录态</span><span class="m-kv-v">' + (ok ? '<span class="m-tag is-accent">可用</span>' : '<span class="m-tag is-gold">不可用</span>') + "</span></div>" +
              '<div class="m-kv"><span class="m-kv-k">账号</span><span class="m-kv-v m-mono">' + txt(data.uin, "—") + "</span></div>" +
              '<div class="m-kv"><span class="m-kv-k">来源</span><span class="m-kv-v">' + txt(data.source, "—") + "</span></div>" +
            "</div>" +
            '<div class="m-btn-row"><button class="m-btn" type="button" data-act="qzone-auth">重新登录</button>' +
              '<button class="m-btn" type="button" data-act="qzone-feed">读动态</button></div>' +
            (feedRows ? '<div class="m-card">' + feedRows + "</div>" : "") +
          "</div>"
        );
      });
    },
  };

  /* ------------------------------------------------------------------ 子页：诊断 */
  PAGES.diag = {
    title: "诊断",
    render: function () {
      return apiGet("diagnostics").then(function (data) {
        var items = data.items || [];
        if (!items.length) return '<div class="m-page"><div class="m-empty">还没有诊断记录</div></div>';
        var rows = items.slice().reverse().map(function (item) {
          return (
            '<div class="m-recent-item">' +
              '<span class="m-recent-time">' + esc(clockOf(item.ts || item.at)) + "</span>" +
              '<div class="m-recent-main"><div class="m-recent-line">' + esc(String(item.kind || item.stage || "tick")) + "</div>" +
                '<div class="m-recent-why" style="white-space:normal">' + esc(clip(JSON.stringify(item.data || item.payload || item), 160)) + "</div></div>" +
            "</div>"
          );
        }).join("");
        return '<div class="m-page"><div class="m-card">' + rows + "</div></div>";
      });
    },
  };

  PAGES.about = {
    title: "关于",
    render: function () {
      return apiGet("overview").then(function (data) {
        var health = data.health || {};
        var admins = health;
        return (
          '<div class="m-page">' +
            '<section class="m-card m-card-pad" style="text-align:center;padding:26px 18px">' +
              '<img src="./logo.png" alt="" style="width:58px;height:58px;border-radius:16px;margin:0 auto 12px" />' +
              '<div style="font-family:var(--m-serif);font-size:21px;font-weight:600">从绫开始</div>' +
              '<div style="margin-top:6px;font-size:12px;color:var(--m-ink-3)">把「会聊天」做到像人 · v' + txt(health.version, "1.1.1") + " 测试版</div>" +
              '<div style="margin-top:12px;font-size:11.5px;color:var(--m-ink-4)">洛水 &amp; 望山 一起写的</div>' +
            "</section>" +
            '<div class="m-card">' +
              '<div class="m-kv"><span class="m-kv-k">官方群</span><span class="m-kv-v">577084969 / 951213204</span></div>' +
              '<div class="m-kv"><span class="m-kv-k">仓库</span><span class="m-kv-v"><a href="https://github.com/LUOSHUI319/astrbot_plugin_congling" target="_blank" rel="noreferrer" style="color:var(--m-accent)">github.com/LUOSHUI319</a></span></div>' +
              '<div class="m-kv"><span class="m-kv-k">移动端</span><span class="m-kv-v">mobile.html · 独立界面</span></div>' +
              '<div class="m-kv"><span class="m-kv-k">桌面端</span><span class="m-kv-v"><button class="m-tag" type="button" data-act="open-desktop">打开</button></span></div>' +
            "</div>" +
            '<div class="m-foot">这个界面只调 page_api，不改任何插件逻辑</div>' +
          "</div>"
        );
      });
    },
  };

  /* ------------------------------------------------------------------ 路由 */
  function openPage(name, params) {
    if (!PAGES[name]) return;
    var entry = { name: name, params: params || {}, title: PAGES[name].title || "" };
    nav.stack.push(entry);
    paintTopbar();
    paintTabs();
    paintDirty();
    dom.main.innerHTML = skeletonHTML(3);
    dom.main.scrollTop = 0;
    Promise.resolve()
      .then(function () { return PAGES[name].render(entry.params); })
      .then(function (html) {
        if (nav.stack[nav.stack.length - 1] !== entry) return;
        entry.html = html;
        entry.title = PAGES[name].title || entry.title;
        paintTopbar();
        dom.main.innerHTML = html;
      })
      .catch(function (err) {
        if (nav.stack[nav.stack.length - 1] !== entry) return;
        dom.main.innerHTML = errorHTML(err);
      });
  }

  function goBack() {
    if (!nav.stack.length) return;
    var top = nav.stack[nav.stack.length - 1];
    if (top.name === "settings-group" && Object.keys(st.dirty).length) {
      ask("这一组改了几项还没保存，确定走？", { ok: "走", cancel: "继续改" }).then(function (yes) {
        if (!yes) return;
        st.dirty = {};
        paintDirty();
        doBack();
      });
      return;
    }
    doBack();
  }

  function doBack() {
    nav.stack.pop();
    paintTopbar();
    paintTabs();
    paintDirty();
    var top = nav.stack[nav.stack.length - 1];
    if (top && top.html) {
      dom.main.innerHTML = top.html;
      dom.main.scrollTop = 0;
      return;
    }
    if (!nav.stack.length) {
      goTab(nav.tab, true);
    }
  }

  /* ------------------------------------------------------------------ 事件 */
  function onClick(event) {
    var tabBtn = event.target.closest ? event.target.closest(".m-tab") : null;
    if (tabBtn && dom.tabbar.contains(tabBtn)) {
      var nextTab = tabBtn.dataset.tab;
      if (nav.stack.length && Object.keys(st.dirty).length) {
        ask("这一组改了几项还没保存，确定走？", { ok: "走", cancel: "继续改" }).then(function (yes) {
          if (!yes) return;
          goTab(nextTab);
        });
        return;
      }
      goTab(nextTab);
      return;
    }

    var node = event.target.closest ? event.target.closest("[data-act]") : null;
    if (!node) return;
    var act = node.dataset.act;

    switch (act) {
      case "tab":
        goTab(node.dataset.tab);
        return;
      case "open":
        openPage(node.dataset.page, { group: node.dataset.group });
        return;
      case "open-desktop":
        /* 带 ?desk=1：桌面版看到它就不再把人送回手机版，两边不会来回弹 */
        window.location.href = "./index.html?desk=1";
        return;
      case "retry":
        if (nav.stack.length) openPage(nav.stack[0].name, nav.stack[0].params);
        else goTab(nav.tab, true);
        return;

      /* 记忆 */
      case "mem-open": {
        var list = st.memory.search && st.memory.search.length ? st.memory.search : st.memory.items;
        var picked = list[num(node.dataset.index)];
        if (picked) memorySheet(picked);
        return;
      }
      case "mem-session":
        st.memory.session = node.dataset.session || "";
        st.memory.search = null;
        goTab("memory", true);
        return;
      case "mem-more":
        node.classList.add("is-loading");
        loadMemory(false).then(function () {
          dom.main.innerHTML = memoryHTML();
        }).catch(function (err) {
          node.classList.remove("is-loading");
          toast(err.message, "err");
        });
        return;
      case "mem-clear-search":
        st.memory.search = null;
        st.memory.keyword = "";
        dom.main.innerHTML = memoryHTML();
        return;
      case "mem-save": {
        var id = num(node.dataset.id);
        var textEl = $("#m-mem-text");
        var levelEl = $("[data-level].is-on");
        var patch = {};
        if (textEl) patch.text = textEl.value;
        var tasks = [];
        if (Object.keys(patch).length) tasks.push(apiPost("memory/update", { id: id, text: patch.text }));
        if (levelEl) tasks.push(apiPost("memory/level/set", { atom_id: id, level: num(levelEl.dataset.level) }));
        Promise.all(tasks).then(function () {
          closeSheet();
          toast("改好了", "ok");
          goTab("memory", true);
        }).catch(function (err) { toast(err.message || "没改成", "err"); });
        return;
      }
      case "mem-del":
        ask("这条记忆删掉就找不回来了，确定？", { danger: true, ok: "删掉" }).then(function (yes) {
          if (!yes) return;
          apiPost("memory/delete", { id: num(node.dataset.id), status: "deleted" }).then(function () {
            closeSheet();
            toast("删掉了", "ok");
            goTab("memory", true);
          }).catch(function (err) { toast(err.message, "err"); });
        });
        return;

      /* 长夜 */
      case "night-sleep":
        apiPost("night/sleep", {}).then(function () {
          toast("让她去睡了", "ok");
          goTab("night", true);
        }).catch(function (err) { toast(err.message, "err"); });
        return;
      case "night-wake":
        ask("现在叫醒她？", { ok: "叫醒" }).then(function (yes) {
          if (!yes) return;
          apiPost("night/wake", {}).then(function () {
            toast("叫醒了", "ok");
            goTab("night", true);
          }).catch(function (err) { toast(err.message, "err"); });
        });
        return;
      case "night-dream":
        ask("现在补做一段梦？会调用一次模型。", { ok: "做一段" }).then(function (yes) {
          if (!yes) return;
          node.classList.add("is-loading");
          apiPost("night/dream/make", {}).then(function (data) {
            closeSheetSafe();
            toast("梦记下了", "ok");
            var dream = data && data.dream;
            if (dream && dream.text) {
              openSheet({ title: "刚做的梦", body: '<div class="m-reader"><p>' + esc(dream.text) + "</p></div>" });
            }
            goTab("night", true);
          }).catch(function (err) {
            node.classList.remove("is-loading");
            toast(err.message || "没做成", "err");
          });
        });
        return;
      case "dream-pin":
        apiPost("night/dream/pin", {
          dream_id: node.dataset.id,
          pinned: node.dataset.pinned !== "1",
        }).then(function () { goTab("night", true); }).catch(function (err) { toast(err.message, "err"); });
        return;
      case "dream-del":
        ask("这段梦要忘掉？", { danger: true, ok: "忘掉" }).then(function (yes) {
          if (!yes) return;
          apiPost("night/dream/delete", { dream_id: node.dataset.id }).then(function () {
            toast("忘了", "ok");
            goTab("night", true);
          }).catch(function (err) { toast(err.message, "err"); });
        });
        return;
      case "night-crawl":
        apiPost("night/crawl", { session_id: node.dataset.session }).then(function (data) {
          toast("爬完了，清掉 " + num(data.removed) + " 条", "ok");
          goTab("night", true);
        }).catch(function (err) { toast(err.message, "err"); });
        return;

      /* 日记 */
      case "diary-open":
        apiGet("diary/get", { day: node.dataset.day }).then(function (row) {
          diaryReader(row);
        }).catch(function (err) { toast(err.message, "err"); });
        return;
      case "diary-write":
        ask("现在让她写一篇今天的日记？会调用模型，可能几秒。", { ok: "写吧" }).then(function (yes) {
          if (!yes) return;
          node.classList.add("is-loading");
          apiPost("diary/generate", {}).then(function (row) {
            node.classList.remove("is-loading");
            toast("写完了", "ok");
            if (row) diaryReader(row);
            goTab("diary", true);
          }).catch(function (err) {
            node.classList.remove("is-loading");
            toast(err.message || "没写成", "err");
          });
        });
        return;
      case "diary-del":
        ask("这篇日记删掉？", { danger: true, ok: "删掉" }).then(function (yes) {
          if (!yes) return;
          apiPost("diary/delete", { id: num(node.dataset.id) }).then(function () {
            closeSheet();
            toast("删掉了", "ok");
            goTab("diary", true);
          }).catch(function (err) { toast(err.message, "err"); });
        });
        return;
      case "diary-pin":
        apiPost("diary/pin", { id: num(node.dataset.id), pinned: node.dataset.pinned !== "1" }).then(function () {
          closeSheet();
          toast("改好了", "ok");
          goTab("diary", true);
        }).catch(function (err) { toast(err.message, "err"); });
        return;

      /* 人格 */
      case "persona-save": {
        var payload = {
          name: ($("#m-persona-name") || {}).value || "",
          identity: ($("#m-persona-identity") || {}).value || "",
          active_hours: ($("#m-persona-hours") || {}).value || "",
        };
        node.classList.add("is-loading");
        apiPost("persona/save", payload).then(function () {
          node.classList.remove("is-loading");
          st.persona = null;
          toast("人格已保存", "ok");
        }).catch(function (err) {
          node.classList.remove("is-loading");
          toast(err.message || "没存上", "err");
        });
        return;
      }
      case "persona-rollback":
        ask("回到这一版人格？", { ok: "回去" }).then(function (yes) {
          if (!yes) return;
          apiPost("persona/rollback", { index: num(node.dataset.index) }).then(function () {
            toast("回去了", "ok");
            openPage("persona");
          }).catch(function (err) { toast(err.message, "err"); });
        });
        return;

      /* 设置 */
      case "settings-save":
        saveSettings();
        return;

      /* 工具 */
      case "tool-run":
        node.classList.add("is-loading");
        apiPost("tools/test", { name: node.dataset.name }).then(function (data) {
          node.classList.remove("is-loading");
          openSheet({
            title: "试跑 · " + node.dataset.name,
            body: '<pre class="m-mono" style="white-space:pre-wrap;word-break:break-word;margin:0;font-size:12.5px;line-height:1.7">' +
              esc(data.ok ? (data.text || "（没输出）") : (data.error || "失败")) + "</pre>" +
              (data.ms ? '<div class="m-field-hint" style="margin-top:10px">耗时 ' + esc(Number(data.ms).toFixed(2)) + " 秒</div>" : ""),
          });
        }).catch(function (err) {
          node.classList.remove("is-loading");
          toast(err.message || "跑失败", "err");
        });
        return;

      /* 主动找人 */
      case "proactive-now":
        ask("现在让她主动发一条？", { ok: "发" }).then(function (yes) {
          if (!yes) return;
          apiPost("proactive/now", { session_id: node.dataset.session }).then(function () {
            toast("发出去了", "ok");
            openPage("proactive");
          }).catch(function (err) { toast(err.message, "err"); });
        });
        return;

      /* 空间 */
      case "qzone-auth":
        node.classList.add("is-loading");
        apiPost("qzone/auth", { force: true }).then(function (data) {
          node.classList.remove("is-loading");
          toast(data.ok ? "登录上了" : "还是不行", data.ok ? "ok" : "err");
          openPage("qzone");
        }).catch(function (err) {
          node.classList.remove("is-loading");
          toast(err.message || "登录失败", "err");
        });
        return;
      case "qzone-feed":
        node.classList.add("is-loading");
        apiGet("qzone/feed", { limit: 5 }).then(function (data) {
          node.classList.remove("is-loading");
          var items = data.items || [];
          openSheet({
            title: "空间动态",
            body: items.length
              ? items.map(function (item) {
                  return '<div class="m-dream"><div class="m-dream-text">' + esc(clip(item.content || item.text || "", 200)) + "</div></div>";
                }).join("")
              : '<div class="m-empty">没读到动态</div>',
          });
        }).catch(function (err) {
          node.classList.remove("is-loading");
          toast(err.message || "读不到", "err");
        });
        return;
      default:
        return;
    }
  }

  function closeSheetSafe() {
    if (dom.sheetWrap.classList.contains("is-open")) closeSheet();
  }

  /* 设置项：开关 / 数字 / 选择 / 文本 → 收集改动 */
  function onControl(event) {
    var node = event.target.closest ? event.target.closest("[data-ctl]") : null;
    if (node) {
      var kind = node.dataset.ctl;
      var path = node.dataset.path;
      if (!path) return;
      if (kind === "bool" && event.type === "click") {
        var on = !node.classList.contains("is-on");
        node.classList.toggle("is-on", on);
        node.setAttribute("aria-checked", on ? "true" : "false");
        st.dirty[path] = on;
        paintDirty();
        return;
      }
      if (kind === "num") {
        st.dirty[path] = num(node.value);
        paintDirty();
        return;
      }
      if (kind === "list") {
        var lines = String(node.value || "").split(/\n/).map(function (s) { return s.trim(); }).filter(Boolean);
        st.dirty[path] = lines.map(function (line) {
          return /^-?\d+(\.\d+)?$/.test(line) ? Number(line) : line;
        });
        paintDirty();
        return;
      }
      if (kind === "select") {
        var opt = String(node.value);
        var sample = null;
        var list = (st.settings && st.settings.groups) || [];
        for (var i = 0; i < list.length; i++) {
          var items = list[i].items || [];
          for (var j = 0; j < items.length; j++) {
            if (items[j].path === path) sample = items[j];
          }
        }
        st.dirty[path] = sample && /^\d+$/.test(opt) ? Number(opt) : opt;
        paintDirty();
        return;
      }
      st.dirty[path] = node.value;
      paintDirty();
      return;
    }

    /* 记忆搜索：输入防抖 */
    if (event.target.id === "m-mem-search" && event.type === "input") {
      var query = event.target.value.trim();
      clearTimeout(onControl._timer);
      onControl._timer = setTimeout(function () {
        if (!query) {
          st.memory.search = null;
          st.memory.keyword = "";
          dom.main.innerHTML = memoryHTML();
          return;
        }
        memorySearch(query).then(function () {
          dom.main.innerHTML = memoryHTML();
        }).catch(function (err) { toast(err.message, "err"); });
      }, 360);
    }
  }

  /* 下拉刷新 */
  var ptr = { startY: null, active: false, busy: false };

  function bindPullToRefresh() {
    dom.main.addEventListener("touchstart", function (event) {
      if (nav.stack.length || ptr.busy) return;
      if (dom.main.scrollTop <= 0) {
        ptr.startY = event.touches[0].clientY;
        ptr.active = true;
      }
    }, { passive: true });

    dom.main.addEventListener("touchmove", function (event) {
      if (!ptr.active || ptr.startY == null) return;
      var dy = event.touches[0].clientY - ptr.startY;
      if (dy <= 0) return;
      var limit = Math.min(dy, 96);
      dom.ptr.classList.add("is-show");
      dom.ptr.style.transform = "translate(-50%, " + (limit - 34) + "px)";
      dom.ptrText.textContent = limit > 64 ? "松开刷新" : "下拉刷新";
    }, { passive: true });

    dom.main.addEventListener("touchend", function () {
      if (!ptr.active) return;
      var dy = 0;
      try {
        var m = /translate\(-50%,\s*(-?[\d.]+)px\)/.exec(dom.ptr.style.transform || "");
        if (m) dy = Number(m[1]) + 34;
      } catch (e) { dy = 0; }
      ptr.active = false;
      ptr.startY = null;
      dom.ptr.classList.remove("is-show");
      dom.ptr.style.transform = "";
      if (dy > 64) {
        ptr.busy = true;
        dom.refresh.classList.add("is-spin");
        st.settings = null;
        st.persona = null;
        goTab(nav.tab, true);
        setTimeout(function () {
          ptr.busy = false;
          dom.refresh.classList.remove("is-spin");
        }, 900);
      }
    });
  }

  /* ------------------------------------------------------------------ 启动 */
  function bind() {
    dom.tabbar.addEventListener("click", onClick);
    dom.main.addEventListener("click", onClick);
    /* 开关是 <button>，只发 click；输入框/选择器发 input/change */
    dom.main.addEventListener("click", onControl);
    dom.main.addEventListener("input", onControl);
    dom.main.addEventListener("change", onControl);
    dom.sheetBody.addEventListener("click", onClick);
    dom.sheetFoot.addEventListener("click", onClick);
    if (dom.savedock) dom.savedock.addEventListener("click", onClick);
    dom.sheetWrap.addEventListener("click", function (event) {
      if (event.target === dom.sheetWrap) closeSheet();
    });
    dom.sheetClose.addEventListener("click", closeSheet);

    if (dom.theme) dom.theme.addEventListener("click", toggleSkin);
    if (dom.refresh) {
      dom.refresh.addEventListener("click", function () {
        st.settings = null;
        st.persona = null;
        dom.refresh.classList.add("is-spin");
        if (nav.stack.length) {
          var top = nav.stack[nav.stack.length - 1];
          nav.stack.pop();
          st.dirty = {};
          paintDirty();
          openPage(top.name, top.params);
        } else {
          goTab(nav.tab, true);
        }
        setTimeout(function () { dom.refresh.classList.remove("is-spin"); }, 800);
      });
    }
    if (dom.back) dom.back.addEventListener("click", goBack);

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        if (dom.sheetWrap.classList.contains("is-open")) closeSheet();
        else goBack();
      }
    });

    bindPullToRefresh();
  }

  function boot() {
    cacheDom();
    paintSkin(readSkin(), !/[?&]skin=/.test(location.search));
    bind();
    paintTabs();
    paintTopbar();
    var q = {};
    (location.search || "").replace(/^\?/, "").split("&").forEach(function (pair) {
      if (!pair) return;
      var kv = pair.split("=");
      q[decodeURIComponent(kv[0])] = decodeURIComponent(kv[1] || "");
    });
    var tab = TABS.indexOf(q.tab) >= 0 ? q.tab : "home";
    goTab(tab);
    /* 深链：mobile.html?page=settings-group&group=night（也方便本地预览） */
    if (q.page && PAGES[q.page]) {
      setTimeout(function () { openPage(q.page, { group: q.group }); }, 0);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
