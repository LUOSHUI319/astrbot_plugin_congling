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

  /* 数据缓存：面板是 iframe，关掉内存就没了，所以放 localStorage。
     3 分钟内直接用上次的，页面立刻出来；后台请求回来的新数据下次进页面就生效。 */
  var DATA_TTL_MS = 180000;
  var DATA_MAX_KEYS = 60;

  function dataKeyOf(path, params) {
    var suffix = "";
    try {
      var keys = Object.keys(params || {}).sort();
      suffix = keys.length
        ? "?" + keys.map(function (k) { return k + "=" + params[k]; }).join("&")
        : "";
    } catch (err) {
      suffix = "";
    }
    return "congling.data." + apiPath(path) + suffix;
  }

  function cacheRead(key) {
    try {
      var raw = localStorage.getItem(key);
      if (!raw) return null;
      var box = JSON.parse(raw);
      if (!box || typeof box.at !== "number") return null;
      if (Date.now() - box.at > DATA_TTL_MS) return null;
      return box.data;
    } catch (err) {
      return null;
    }
  }

  function cacheWrite(key, data) {
    try {
      var keys = Object.keys(localStorage).filter(function (k) {
        return k.indexOf("congling.data.") === 0;
      });
      if (keys.length > DATA_MAX_KEYS) {
        keys.slice(0, keys.length - DATA_MAX_KEYS).forEach(function (k) {
          localStorage.removeItem(k);
        });
      }
      localStorage.setItem(key, JSON.stringify({ at: Date.now(), data: data }));
    } catch (err) {}
  }

  function clearDataCache() {
    try {
      Object.keys(localStorage)
        .filter(function (k) { return k.indexOf("congling.data.") === 0; })
        .forEach(function (k) { localStorage.removeItem(k); });
    } catch (err) {}
  }

  function apiGet(path, params) {
    var p = apiPath(path);
    if (/^page\/(settings|providers)(\/|$)/.test(p)) {
      return ensureBridge()
        .then(function (api) { return api.apiGet(p, params || {}); })
        .then(unwrap);
    }
    var key = dataKeyOf(path, params);
    var hit = cacheRead(key);
    var request = ensureBridge()
      .then(function (api) { return api.apiGet(p, params || {}); })
      .then(function (resp) {
        var data = unwrap(resp);
        cacheWrite(key, data);
        return data;
      });
    if (hit !== null) {
      request.catch(function () {});   // 后台刷新失败不影响旧数据
      return Promise.resolve(hit);
    }
    return request;
  }

  function apiPost(path, body) {
    return ensureBridge()
      .then(function (api) { return api.apiPost(apiPath(path), body || {}); })
      .then(function (resp) {
        clearDataCache();   // 写操作之后旧缓存全部作废
        return unwrap(resp);
      });
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
  /* 读取中：桥是 RPC，没有真实进度可言，只显示已经等了多久 */
  var elapsedTimer = 0;
  var elapsedStart = 0;

  function elapsedPaint() {
    var text = dom.main.querySelector(".m-load-text");
    if (!text) return;
    var sec = Math.max(0, (Date.now() - elapsedStart) / 1000);
    text.textContent = "正在读取 · 已等 " + sec.toFixed(1) + "s";
  }

  function startProgress() {
    elapsedStart = Date.now();
    elapsedPaint();
    if (elapsedTimer) clearInterval(elapsedTimer);
    elapsedTimer = setInterval(elapsedPaint, 100);
  }

  function stopProgress() {
    if (elapsedTimer) {
      clearInterval(elapsedTimer);
      elapsedTimer = 0;
    }
  }

  function skeletonHTML(count) {
    var body =
      '<div class="m-card m-card-pad" style="margin-bottom:12px">' +
      '<div class="m-load"><span class="m-load-spin"></span>' +
      '<span class="m-load-text">正在读取 · 已等 0.0s</span></div>' +
      "</div>";
    for (var i = 0; i < Math.max(0, (count || 3) - 1); i++) {
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

  var TAB_HTML_CACHE = {};

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
    /* 切回看过的页签时先放上次的画面，别又闪一遍骨架。
       force（主动刷新）才强制从骨架开始。 */
    var cachedPage = TAB_HTML_CACHE[tab];
    var freshPage = !force && cachedPage && Date.now() - cachedPage.at < 120000 ? cachedPage.html : "";
    dom.main.innerHTML = freshPage || skeletonHTML(tab === "home" ? 2 : 3);
    if (freshPage) stopProgress();
    else startProgress();
    dom.main.scrollTop = 0;
    var render = VIEWS[tab];
    if (typeof render !== "function") {
      stopProgress();
      dom.main.innerHTML = errorHTML(new Error("这个页签没有渲染函数：" + tab));
      return;
    }
    /* 兜底：接口卡死时不要让骨架一直转。
       15 秒还没结果就换成错误卡片，至少能看出是出了问题。 */
    var watchdog = setTimeout(function () {
      if (nav.tab === tab && !nav.stack.length) {
        stopProgress();
        dom.main.innerHTML = errorHTML(new Error("读取超时，检查一下连接再下拉刷新"));
      }
    }, 15000);
    Promise.resolve()
      .then(function () { return render(); })
      .then(function (html) {
        clearTimeout(watchdog);
        if (nav.tab !== tab || nav.stack.length) return;
        dom.main.innerHTML = html;
        stopProgress();
        TAB_HTML_CACHE[tab] = { html: html, at: Date.now() };
        if (force && nav.scroll[tab]) {
          dom.main.scrollTop = 0;
        }
        paintTabs();
      })
      .catch(function (err) {
        clearTimeout(watchdog);
        stopProgress();
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
            '<p class="m-hero-meta">v' + txt(health.version, "1.3.0") +
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
    if (snapshot.wake_recovering) return "醒着，精力在回来";
    return "醒着";
  }

  function nightHTML() {
    var s = st.night.state || {};
    if (!s.enabled) {
      return (
        '<div class="m-page"><div class="m-empty"><b>长夜如梦没开</b>' +
        "开了之后她会累、会困、会睡。睡着期间攒下的记忆，醒来会在后台按十分钟的进度慢慢放行，不用先说话。<br>梦里只留 REM 期那几段。" +
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
    /* 安静是「醒着但不接话」，和睡着不是一回事：不做梦、不回精力，
       面板上必须分开显示，否则会以为它睡着了。 */
    var quiet = s.quiet === true;
    var desc = asleep
      ? (s.stage_text || "正在睡") + (s.remaining_text ? " · 还有 " + s.remaining_text : "")
      : quiet
        ? "安静中（说了「" + (s.quiet_word || "晚安") + "」），到 " + (s.quiet_clock || "—") +
          "，还有 " + num(s.quiet_left_minutes) + " 分钟"
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

    var quietCard = quiet
      ? '<div class="m-card" style="border-color:rgba(200,168,106,.45)">' +
          '<div class="m-kv"><span class="m-kv-k">安静中</span><span class="m-kv-v">' +
            '<span class="m-tag is-gold">醒着 · 不接话</span></span></div>' +
          '<div class="m-kv"><span class="m-kv-k">到</span><span class="m-kv-v m-mono">' +
            esc(s.quiet_clock || "—") + "</span></div>" +
          '<div class="m-kv"><span class="m-kv-k">还剩</span><span class="m-kv-v">' +
            esc(String(num(s.quiet_left_minutes))) + " 分钟</span></div>" +
          '<div class="m-hint" style="margin-top:8px">想让她重新接话，发两条消息就行（群里要 @ 或引用）。</div>' +
        "</div>"
      : "";

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
        '<div class="m-kv"><span class="m-kv-k">补读</span><span class="m-kv-v">' + (s.catchup_on === false ? "关着" : "开着") + "</span></div>" +
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
              '<span class="m-tag is-rose">补完</span>' +
            "</button>"
          );
        }).join("")
      : '<div class="m-empty">没有攒着的消息</div>';

    return (
      '<div class="m-page">' +
        cards +
        quietCard +
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
            '<div style="font-size:11.5px;color:var(--m-ink-4)">v' + txt(health.version, "1.3.0") + " · 已运行 " + esc(durationText(health.uptime)) + "</div>" +
          "</div>" +
          '<button class="m-btn" type="button" data-act="open" data-page="persona">看人格</button>' +
        "</section>" +

        '<div class="m-group-head">调她</div>' +
        '<div class="m-card">' +
          row("user", "人格", "名字、身份设定、风格与禁忌", 'data-act="open" data-page="persona"') +
          row("sliders", "设置", "全部配置项，按组翻", 'data-act="open" data-page="settings"') +
          row("memory", "永久记忆", "不会淡忘的那一档，单独看", 'data-act="open" data-page="permanent"') +
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
          row("spark", "更新日志", "这一版改了什么、修了什么", 'data-act="open" data-page="changelog"') +
          row("spark", "版本", "从绫开始 v" + txt(health.version, "1.3.0"), 'data-act="open" data-page="about"') +
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
            '<div class="m-btn-row">' +
              '<button class="m-btn" type="button" data-act="open" data-page="settings-all">总览全部配置</button>' +
              '<button class="m-btn" type="button" data-act="open" data-page="settings-read">只读快照</button>' +
            '</div>' +
            '<div class="m-card">' + groups + "</div>" +
            footHTML() +
          "</div>"
        );
      });
    },
  };

  /* 设置总览：一页把所有配置平铺出来，按板块分区。
     手机上一层层点进去太慢，想核对"到底配了什么"时需要一眼看完。 */
  PAGES["settings-all"] = {
    title: "设置总览",
    render: function () {
      return apiGet("settings").then(function (data) {
        st.settings = data;
        var groups = data.groups || [];
        if (!groups.length) return '<div class="m-page"><div class="m-empty">没有读到配置</div></div>';
        var blocks = groups.map(function (group) {
          var title = group.title && group.title !== group.name
            ? group.title
            : (GROUP_TITLE_ZH[group.name] || group.title || group.name);
          var items = group.items || [];
          var rows = items.map(function (item) {
            var label = txt(item.label, item.key);
            return '<div class="m-kv"><span class="m-kv-k">' + esc(label) + "</span>" +
              '<span class="m-kv-v m-mono">' + esc(settingValue(item)) + "</span></div>";
          }).join("");
          return '<div style="margin:18px 4px 6px;font-size:13px;font-weight:600;opacity:.85">' +
              esc(title) + ' <span style="font-weight:400;opacity:.6">· ' + items.length + " 项</span></div>" +
            '<div class="m-card">' + (rows || '<div class="m-empty">这组没有配置项</div>') + "</div>";
        }).join("");
        return '<div class="m-page">' +
          '<div class="m-empty" style="padding:6px 4px 0;text-align:left;font-size:12px">只读总览，要改去对应的分组里改</div>' +
          blocks + "</div>";
      });
    },
  };

  function settingValue(item) {
    var v = item.value;
    var type = String(item.type || "string");
    /* 后端一般是真 bool，但旧配置里可能是 "False" 这种字符串——
       直接 `v ?` 会把它们全判成"开"，所以显式比对。 */
    if (type === "bool") {
      var on = v === true || v === 1 || v === "1" || String(v).toLowerCase() === "true";
      return on ? "开" : "关";
    }
    if (v === null || v === undefined || v === "") return "—";
    if (Array.isArray(v)) {
      if (!v.length) return "（空）";
      var head = v.slice(0, 3).join("、");
      return v.length > 3 ? head + " 等 " + v.length + " 项" : head;
    }
    var text = String(v);
    return text.length > 30 ? text.slice(0, 30) + "…" : text;
  }


  /* 设置快照：和「设置总览」共用一份数据，但一个控件都没有。
     手机屏幕小，误触代价大；想核对现状时用这一页更安全。 */
  PAGES["settings-read"] = {
    title: "设置快照",
    render: function () {
      return apiGet("settings").then(function (data) {
        st.settings = data;
        var groups = data.groups || [];
        if (!groups.length) return '<div class="m-page"><div class="m-empty">没有读到配置</div></div>';
        var blocks = groups.map(function (group) {
          var title = group.title && group.title !== group.name
            ? group.title
            : (GROUP_TITLE_ZH[group.name] || group.title || group.name);
          var items = group.items || [];
          var rows = items.map(function (item) {
            var label = txt(item.label, item.key);
            var value = settingValue(item);
            var long = String(value).length > 34;
            return '<div class="m-kv"' + (long ? ' style="align-items:flex-start"' : "") + ">" +
              '<span class="m-kv-k">' + esc(label) + "</span>" +
              '<span class="m-kv-v' + (long ? "" : " m-mono") + '"' +
                (long ? ' style="text-align:right;white-space:normal;line-height:1.55"' : "") + ">" +
                esc(value) + "</span></div>";
          }).join("");
          return '<div style="margin:16px 4px 6px;font-size:13px;font-weight:600;opacity:.85">' +
              esc(title) + ' <span style="font-weight:400;opacity:.6">· ' + items.length + " 项</span></div>" +
            '<div class="m-card">' + (rows || '<div class="m-empty">这组没有配置项</div>') + "</div>";
        }).join("");
        return '<div class="m-page">' +
          '<div class="m-empty" style="padding:6px 4px 0;text-align:left;font-size:12px">只读快照，一个都不能点。要改请回「设置」。</div>' +
          blocks + "</div>";
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
      /* 状态和动态是两个接口：
         /qzone      只给状态（auth.ok / write_allowed / mode…），不含动态
         /qzone/feed 才给 items
         以前这里只读 /qzone，还去读 data.ok / data.usable / data.feed，
         三个字段后端都不返回 —— 结果未登录也显示「可用」，动态永远空白。 */
      return Promise.all([
        apiGet("qzone").catch(function (err) { return { error: err.message }; }),
        apiGet("qzone/feed", { limit: 10 }).catch(function () { return { items: [] }; }),
      ]).then(function (res) {
        var data = res[0] || {};
        var feedData = res[1] || {};
        if (data.error) {
          return '<div class="m-page"><div class="m-empty"><b>空间没接上</b>' + esc(data.error) +
            '<div style="margin-top:14px"><button class="m-btn" type="button" data-act="qzone-auth">试着登录</button></div></div></div>';
        }
        var auth = data.auth || {};
        var ok = auth.ok === true;
        var canWrite = data.write_allowed === true;
        var items = feedData.items || [];
        var feedRows = items.slice(0, 10).map(function (item) {
          var text = item.content || item.text || "";
          var shots = (item.images || []).length;
          return '<div class="m-dream"><div class="m-dream-text" style="-webkit-line-clamp:3">' +
              (text ? esc(clip(text, 160)) : '<span style="opacity:.6">（一组图，' + shots + ' 张）</span>') +
            "</div>" +
            '<div class="m-dream-foot"><span>' + esc(item.nickname || "") + "</span>" +
              "<span>" + esc(item.created_text || item.createTime || item.time || "") + "</span></div></div>";
        }).join("");

        return (
          '<div class="m-page">' +
            '<div class="m-card">' +
              '<div class="m-kv"><span class="m-kv-k">登录态</span><span class="m-kv-v">' +
                (ok ? '<span class="m-tag is-accent">已就绪</span>' : '<span class="m-tag is-gold">未获取</span>') + "</span></div>" +
              '<div class="m-kv"><span class="m-kv-k">账号</span><span class="m-kv-v m-mono">' + txt(auth.uin, "—") + "</span></div>" +
              '<div class="m-kv"><span class="m-kv-k">来源</span><span class="m-kv-v">' + txt(auth.source, "—") + "</span></div>" +
              '<div class="m-kv"><span class="m-kv-k">写权限</span><span class="m-kv-v">' +
                (canWrite ? '<span class="m-tag is-accent">已开</span>' : '<span class="m-tag is-gold">关闭</span>') + "</span></div>" +
              '<div class="m-kv"><span class="m-kv-k">主动发布</span><span class="m-kv-v">' +
                esc(data.behavior_text || "—") + "</span></div>" +
            "</div>" +
            '<div class="m-btn-row">' +
              '<button class="m-btn" type="button" data-act="qzone-auth">更新登录态</button>' +
              (canWrite
                ? '<button class="m-btn is-primary" type="button" data-act="qzone-publish">发一条</button>'
                : '<button class="m-btn" type="button" disabled>发说说（写权限关着）</button>') +
            "</div>" +
            '<div class="m-card-title">最近动态</div>' +
            (feedRows ? '<div class="m-card">' + feedRows + "</div>"
                      : '<div class="m-card"><div class="m-empty">' + (ok ? "最近没动静" : "还没拿到登录态") + "</div></div>") +
          "</div>"
        );
      });
    },
  };

  /* ------------------------------------------------------------------ 子页：诊断 */

  /* ------------------------------------------------------------------ 子页：更新日志 */
  var CHANGELOG_HTML = "<section class=\"m-cl-ver\"><div class=\"m-cl-head\"><span class=\"m-cl-ver-no\">1.3.0</span><span class=\"m-cl-date\">2026-11-15</span></div><div class=\"m-cl-tag\">空间、安静与手机端</div><p class=\"m-cl-note\">已公布版本是 1.2.0，这一版把原准备作为 1.2.1 的内容一并收了进来。</p><div class=\"cl-sub\">新增</div><div class=\"m-cl-list\"><div class=\"m-cl-item\"><b>QQ 空间能力全打通</b><span>三个工具四个接口：read（动态 / 某人空间 / 单条全文 / 评论 / 访客）、post（发 / 删）、interact（点赞 / 评论 / 回评）。配套四条边界：删说说限 6 小时内、访客每天一次、评论 id 不外泄给模型、点赞评论只</span></div><div class=\"m-cl-item\"><b>说说自动配图</b><span>正文抽情绪词 → 表情包库检索 → 上传图床 → 带图发布。没有情绪词就不配图——配图是情绪的标点，不是装饰。任何一步失败都退回纯文本，不会因为配不上图把说说卡死。</span></div><div class=\"m-cl-item\"><b>空间行为模式</b><span>off 只在明确要求时动手；intent 可以有念头，带「最近发过什么」防重复和 6 小时冷却。两档都在 prompt 层注入，不动 system prompt。</span></div><div class=\"m-cl-item\"><b>时间来源可切</b><span>clock.source：beijing 固定 UTC+8（等价 Asia/Shanghai，但不用装 tzdata）、local 跟本机时区。作息窗口、起床点、最晚睡觉、日记按天归档、面板钟点全部走同一套钟。</span></div><div class=\"m-cl-item\"><b>收线改为「安静」</b><span>以前收线要么真睡、要么什么都不做，现在多了一层中间态：强档安静到下次起床点，拜拜 30 分钟，午安 75 分钟。mode 保持 awake——不做梦、不按睡眠阶段回精力、醒来也不说道别。</span></div><div class=\"m-cl-item\"><b>安静的解除规则</b><span>只有管理员的消息计数；群聊要 @ 或引用；默认 5 分钟内 2 条解除，到数就醒，不掷骰。</span></div><div class=\"m-cl-item\"><b>设置总览</b><span>15 个板块 241 项配置一页摊开。电脑端按「常改 → 少改」排序、冷门项收进折叠，改完一起保存；手机端在设置页顶部一个按钮直达。</span></div><div class=\"m-cl-item\"><b>设置快照</b><span>纯只读，一个可点控件都没有。用来核对现状、截图发人、排查「改了怎么没生效」。</span></div><div class=\"m-cl-item\"><b>日记签名可配</b><span>diary.signature，1-3 个字。留空则跟随当前人格名。</span></div><div class=\"m-cl-item\"><b>手机端补齐</b><span>空间页重写（登录态、账号、来源、写权限、动态一次看全）、新增发说说、新增更新日志页。</span></div><div class=\"m-cl-item\"><b>永久记忆单独一页</b><span>0 级那一档不衰减、不会被淡忘。混在几万条里翻找不着，现在单独一页：条数、占比、命中次数，可降成普通或忘掉。</span></div><div class=\"m-cl-item\"><b>时间提示挂到最后</b><span>时间每轮都变，单独抽出来追加在注入链最末尾。前面的记忆、人格、场景不用重算，前缀缓存照样命中。</span></div></div><div class=\"cl-sub\">修复</div><div class=\"m-cl-list\"><div class=\"m-cl-item\"><b>永久记忆等级 0 被读成 3</b><span>int(x or 3) 遇上 mem_level=0（永久不可忘）会返回 3。全插件 8 处都这么写，实库 157 条永久记忆在展示、权重、统计里全掉回普通档，禁区保护形同虚设。现在统一走 level_of()，只把 None、空串、解析失</span></div><div class=\"m-cl-item\"><b>「重写日记」是假按钮</b><span>compose() 发现当天已有日记就直接返回旧文；就算绕过它，_insert 用的是 INSERT OR IGNORE，新内容也会被静默丢弃。两道门都得开：加 force 跳过短路，新增 _replace() 用 UPDATE 覆盖，同时</span></div><div class=\"m-cl-item\"><b>主动找人后认不出自己说的话</b><span>bot 主动问「你吃饭了么」，用户回「没有」，它反问「什么没有」。根因是三轮叠加：框架的 send_message 不写对话历史、归档里确实有那条、但注入时只按名字渲染成「绫: …」，模型认不出是自己说的。现在按 sender_id 判角色</span></div><div class=\"m-cl-item\"><b>人格名残留「绫」</b><span>配置里 persona.name 还写着旧名，系统提示跟随 AstrBot 人格（洛水），归档却读配置——两条路分叉，于是模型把「绫」当成别人。已统一：配置改名、归档改用 display_name()、并过滤掉拿不到时返回的占位串。前端日记</span></div><div class=\"m-cl-item\"><b>手机端空间页字段层级读错</b><span>读 data.ok、data.usable、data.feed，三个字段后端都不返回。结果未登录也显示「可用」，动态永远空白。现在读 auth.ok 与 write_allowed，动态另调 qzone/feed。</span></div><div class=\"m-cl-item\"><b>收线词表被配置整表覆盖</b><span>config.list(...) or list(STRONG)：配置只要非空，代码默认词全作废，源码里的「睡觉去」就是这么无声失效的。改成默认词加配置词合并，新增 close.removed_words 用来删默认词。</span></div><div class=\"m-cl-item\"><b>「睡觉吧」不在任何词表</b><span>补进强档：睡觉吧、去睡觉、睡吧、我去睡了、该睡了。叙述句（他说睡觉吧）和问句（睡觉吧？）仍被句式过滤挡住。</span></div><div class=\"m-cl-item\"><b>「安静一会儿」是句空话</b><span>close_minutes() 以前除了测试没有任何调用点，命中后只改了一句提示。现在真的落成 quiet_until：写进 kv、重启有效、面板能看到。</span></div><div class=\"m-cl-item\"><b>主动找人不受安静期约束</b><span>说了晚安还反过来主动戳人，比不回更怪。现在会跳过安静期。</span></div><div class=\"m-cl-item\"><b>日记里写精力数值</b><span>素材里直接喂了「精力 100」，模型照抄成「精力是满的，100」，读起来像仪表盘读数。改成感觉描述（精神是满的 / 有点乏 / 快撑不住了），并在 prompt 里禁掉数值型状态。</span></div><div class=\"m-cl-item\"><b>版本号显示过期</b><span>手机端兜底写死 1.1.1（4 处）、关于页写死「测试版」、左上角写死「1.1.1 · 测试版」。现在统一从 health.version 读。</span></div><div class=\"m-cl-item\"><b>人格状态和作息提示打架</b><span>人格状态按小时切四档，18:00 后一律说「你精神最好」，但它不读精力。23:40、精力 18% 时会同时注入「精神最好」和「已经困了」，语气就飘了。现在人格只描述此刻在做什么。</span></div><div class=\"m-cl-item\"><b>疲劳提示在报电量</b><span>「精力还剩 38%」这类句子模型只会照着复述。现在只在精力低于 30 时说一句「心里在准备休息」，其余时候不吭声。</span></div></div></section><section class=\"m-cl-ver\"><div class=\"m-cl-head\"><span class=\"m-cl-ver-no\">1.2.1</span><span class=\"m-cl-date\">2026-11-11</span></div><div class=\"m-cl-tag\">收线静默认人了</div><p class=\"m-cl-note\">谁能让它停下这件事，现在要过名单了（并入 1.3.0）。</p><div class=\"cl-sub\">新增</div><div class=\"m-cl-list\"><div class=\"m-cl-item\"><b>收线静默要认人了</b><span>以前任何人一句晚安都能让它躺下，群里随便一个路人就能把整只 bot 按睡过去。新增 trigger_mode：whitelist 只认白名单（管理员始终算在内，默认）、admin 只认管理员、all 谁都能触发（旧行为）。名单填 QQ 号，</span></div><div class=\"m-cl-item\"><b>静默是全局的，没有分会话这一档</b><span>试过做「只停当前会话」，但那样等于私聊躺下了群里还能聊。既然睡了就是睡了，就只有一个开关：所有会话一起停，醒来一起醒。</span></div></div><div class=\"cl-sub\">修复</div><div class=\"m-cl-list\"><div class=\"m-cl-item\"><b>私聊安静了，群聊还在回</b><span>state.on_message 开头有一句「未启用就直接放行」，写在状态判断之前，而入睡本身并不看长夜开关。于是「长夜如梦没开、只开了收线静默」的机器上，私聊里说完晚安它真躺下了，群聊那边却因为提前返回继续有说有笑。现在只有确实没在睡才提</span></div></div></section><section class=\"m-cl-ver\"><div class=\"m-cl-head\"><span class=\"m-cl-ver-no\">1.2.0</span><span class=\"m-cl-date\">2026-11-07</span></div><div class=\"m-cl-tag\">空间与星图</div><p class=\"m-cl-note\">空间从只能发纯文字补到能带图、能读别人的、能互动。</p><div class=\"cl-sub\">新增</div><div class=\"m-cl-list\"><div class=\"m-cl-item\"><b>QQ 空间发说说可以带图</b><span>先传到空间图床拿回 richval 与 pic_bo，再随正文一起提交，单条最多九张。参数全部来自实测：上传走 base64 加 urlencoded（不是 multipart）、richval 固定十段且首尾为空、多张之间 richval</span></div><div class=\"m-cl-item\"><b>好友动态重新可读</b><span>正文、时间、配图、来源会话都能取到，纯图说说也会保留。顺手把访客上限压到每天一次 —— 刷了谁的动态，对方在最近访客里是看得见的。</span></div><div class=\"m-cl-item\"><b>空间评论</b><span>支持在说说下留言。返回被 frameElement.callback 包裹，解析器已经能剥这种外壳。</span></div><div class=\"m-cl-item\"><b>刷到的动态会写进记忆</b><span>按「看到了但没参与」的权重入库：重要度 0.42、等级 2、不计作自身经历。短于八个字又没有配图的直接跳过，避免稀释真正重要的记忆。</span></div><div class=\"m-cl-item\"><b>空间互动三个开关</b><span>主动浏览、随机评论、随机点赞各自独立，全部默认关闭，并且各有每日上限。评论比点赞克制，点赞比浏览克制 —— 越公开的行为，配额越紧。</span></div><div class=\"m-cl-item\"><b>记忆星图</b><span>把每一段记忆画成一颗星：同会话、同实体、文字相近的会连成星座，越重要的星越大越亮。可搜索、可按分类和时间筛选，点开一颗星能看到来源会话、重要度、衰减曲线和关联记忆。只读直连数据库，不写不改。</span></div><div class=\"m-cl-item\"><b>记忆星图已上架，但要进群拿</b><span>它是一个独立插件，不随本体分发。面板侧边栏新增「记忆星图」一页：预览图、功能说明、一键打开、以及复制记忆库路径。安装包与后续版本都在官方群里。</span></div></div><div class=\"cl-sub\">修复</div><div class=\"m-cl-list\"><div class=\"m-cl-item\"><b>好感度页卡住九秒</b><span>排行榜的过滤条件要对四百多个主体逐个反查「这个 id 是不是群号」，而 msg_archive 有五万三千行却没有对应索引，等于四百多次全表扫。补上 (session_kind, session_id) 索引后，同一条查询 9000ms →</span></div><div class=\"m-cl-item\"><b>记忆总览打开要等</b><span>统计、未读数、会话列表、注入档位是四条独立查询串行执行。合并成一条聚合查询，并给 (status, created_at) 建索引，单次 22ms → 0ms。</span></div><div class=\"m-cl-item\"><b>好友动态一直读不到内容</b><span>该接口返回的不是 JSON：键没有引号、字符串用单引号、HTML 里的尖括号写成 \\\\x3C，标准解析必然失败，异常又被上层兜住，表现成「最近没什么动静」。改为字符串感知的逐字符扫描，能正确切出每条动态。</span></div><div class=\"m-cl-item\"><b>动态正文混着昵称、浏览数、点赞人</b><span>原来整段 HTML 去标签，把渲染层的所有文字都卷了进来。改为只取正文容器 f-info，图片只从 img-box 里找，并按 appid、typeid、图床域名过滤广点通广告。</span></div><div class=\"m-cl-item\"><b>头像库存不进图片</b><span>接口只有信任、取消信任、删除、改标签四个动作，压根没有入库分支，add_bytes 从上线起没被调用过一次。补上上传动作，顺带把两套皮肤都加上「存一张」按钮。</span></div><div class=\"m-cl-item\"><b>空间点赞接口返回 500</b><span>常见实现里的 emotion_cgi_like 已经不可用。真正可用的是 internal_dolike_app，参数还要求 unikey 与 curkey —— 它们不在任何 JSON 字段里，藏在动态 HTML 的 data 属性上。</span></div><div class=\"m-cl-item\"><b>文章里说点赞能用，但拿不到老说说的 unikey</b><span>unikey 只在好友动态流里给。指定某人的历史说说走的是另一条接口，不返回这个字段，所以「给某条老说说点赞」目前做不到，已把点赞开关默认关闭。</span></div><div class=\"m-cl-item\"><b>面板接口出问题只能靠猜</b><span>新增计时中间件，每个接口的耗时写进 diag:timing。这次的九秒就是靠它直接定位到具体接口，而不是逐个怀疑缓存、桥接、资源体积。</span></div></div></section><section class=\"m-cl-ver\"><div class=\"m-cl-head\"><span class=\"m-cl-ver-no\">1.1.1</span><span class=\"m-cl-date\">2026-11-03</span></div><div class=\"m-cl-tag\">移动端</div><p class=\"m-cl-note\">给手机做一套真正的界面。</p><div class=\"cl-sub\">新增</div><div class=\"m-cl-list\"><div class=\"m-cl-item\"><b>手机端五个页签</b><span>概览（人格名 / 精力 / 记忆数 / 最近决策）、记忆（检索、改等级、删）、长夜（精力环、让她睡、叫醒、补梦、去爬）、日记（写一篇、衬线体阅读页、置顶）、更多（人格、设置、工具、好感度、主动找人、QQ 空间、诊断）。</span></div><div class=\"m-cl-item\"><b>手机端专门处理的事</b><span>可点区域 ≥ 44px、100dvh 加安全区、底部弹层、自绘下拉刷新、输入框字号 ≥ 16px；避开 color-mix 和可选链，X5 与微信内置浏览器也能开。</span></div></div><div class=\"cl-sub\">修复</div><div class=\"m-cl-list\"><div class=\"m-cl-item\"><b>移动端换成一整套独立界面</b><span>原来手机上看桌面面板只能整页缩放，字小、按钮难点。这一版新增 mobile.html / mobile.css / mobile.js：底部五个页签、卡片流、底部弹层，设置页按 schema 动态渲染，14 组两百多项都能在手机上改。</span></div><div class=\"m-cl-item\"><b>摘掉原来的移动端适配</b><span>responsive.css（响应式规则全部挂在 html[data-layout=\\\"mobile\\\"] 下）与「桌面 / 手机视图」切换按钮一并删除，1280 整页缩放也去掉，桌面面板回到纯桌面布局。</span></div><div class=\"m-cl-item\"><b>修掉「从手机版回桌面版会被弹回去」的隐患</b><span>AstrBot 给插件页发的响应带 Referrer-Policy: no-referrer，document.referrer 拿不到值，靠 referrer 判断会失效。改成显式开关：手机版点「桌面面板」走 ?desk=1，桌面版看到它</span></div><div class=\"m-cl-item\"><b>修掉「日记和梦的素材把昵称当第三人称」的问题</b><span>素材行只带了 sender_name，模型照着写成「某某一早发了好几条」。改成用 sender_id 判主语：自己写过的话记作「我」，别人的一律写「对方」，群聊也不再拼昵称。</span></div></div></section><section class=\"m-cl-ver\"><div class=\"m-cl-head\"><span class=\"m-cl-ver-no\">1.1.0</span><span class=\"m-cl-date\">2026-11-02</span></div><div class=\"m-cl-tag\">稳定性</div><p class=\"m-cl-note\">把「能用」变成「稳」。</p><div class=\"cl-sub\">新增</div><div class=\"m-cl-list\"><div class=\"m-cl-item\"><b>三套稳定标识</b><span>用户、会话、说话人各有一套固定键。名字永远不参与标识，缺失身份时明确标为待补，而不是猜一个。</span></div><div class=\"m-cl-item\"><b>三层召回与弱相关兜底</b><span>会话层、个人层、公共层分开检索，另有全库层供跨群查找。</span></div><div class=\"m-cl-item\"><b>自称识别与别名</b><span>从「我叫 X / 叫我 X / 我是 X」里抽出称呼写进主体库，抽取时能用别名反查。规则刻意收紧：按停顿词截断、拒绝「我是群主」这类、单字不认。</span></div><div class=\"m-cl-item\"><b>事件账本</b><span>新增四状态的自身行为账本（已计划、已成功、已失败、已过期），同一终态重试幂等，不同终态互转直接拒绝。与记忆写在同一事务，失败一起回滚。</span></div><div class=\"m-cl-item\"><b>梦境排产与补发</b><span>入睡时算出整晚 REM 段，默认预生成前三段，进程在线时后续段实时生成。启动时补发 24 小时内到期的梦，不逐条播报。</span></div><div class=\"m-cl-item\"><b>梦境检索工具</b><span>任何等级都能查，但只在对方明确问起时调用。工具描述里写死了「梦想 / 白日梦 / 别做梦了 / 中国梦」不触发，不靠关键词拦截。</span></div><div class=\"m-cl-item\"><b>素材标签与类型互斥</b><span>主体是我、主体未确认、他人三档明确；历史正文永不改写。同一段经历不会在日记、说说、梦里反复出现。</span></div><div class=\"m-cl-item\"><b>删除所有记忆</b><span>四重确认：条数清单、输入固定短语、勾选并确认已备份、按钮倒计时十秒。服务端强制导出备份、单事务删除、删完回收空间、写修复日志。身份只来自框架认证，不信任请求体。</span></div></div><div class=\"cl-sub\">修复</div><div class=\"m-cl-list\"><div class=\"m-cl-item\"><b>发现并修复「插件重载后有一半任务不再运行」的问题</b><span>所有后台任务都是裸 create_task，没有持有引用。重载或退出时既不会取消也不会等待，旧任务和新任务并存或直接消失。修复后统一走 spawn_background 托管，加 _stopping 闸门，退出时按 cancel → gat</span></div><div class=\"m-cl-item\"><b>发现并修复「精力值一直停着不动」的问题</b><span>作息心跳挂在每小时一次的清理循环里，一天只跑 24 次，白天没人说话时精力冻住，也不会自然醒。修复后拆出独立心跳（默认 60 秒），内部按 last_tick 补算，漏拍和重启都能补齐。</span></div><div class=\"m-cl-item\"><b>发现并修复「状态只在内存里，重启就回退」的问题</b><span>night.state 只在有事件时才落盘。修复后每拍保存，疲劳轮次和起点一并持久化；醒来那一刻的睡眠阶段也从内存变量改成写 kv，重启后语气不会丢。</span></div><div class=\"m-cl-item\"><b>发现并修复「在别的群问起同一个人，她说记不得」的问题</b><span>召回只有「当前会话」和「公共」两层，没有「关于这个人」这一层。修复后引入三套稳定标识（user / session / actor），召回改成会话层、个人层、公共层三层。</span></div><div class=\"m-cl-item\"><b>发现并修复「问她『你还记得我吗』答不上来」的问题</b><span>这类问句里没有可用于关键词匹配的实词。修复后加元问题特判，按 about_ref 直接取高等级记忆；三层全空时再按 importance × decay 兜底，标记为弱相关。</span></div><div class=\"m-cl-item\"><b>发现并修复「记忆搜索工具永远返回没找到」的问题</b><span>工具走的是空主体检索，在候选层里等于「只查公共记忆」，而全库只有一条。修复后工具接 takes_event，用当前会话加发言人构造标识；拿不到事件时退成全库，私聊一律不碰。</span></div><div class=\"m-cl-item\"><b>发现并修复「跨会话开了 all 档也不生效」的问题</b><span>只做了三层候选，all 没有对应实现。修复后补全库层，并用 LIKE 粗筛——否则几千条里按重要度取前八百会把目标挤出去。</span></div><div class=\"m-cl-item\"><b>发现并修复「补完候选还是排不出来」的问题</b><span>候选池同质导致词频权重被拉平，含「你 / 还 / 吗」的条目反而排前。修复后按实词命中加权，排除虚词和「记得 / 知道」这类元词。</span></div><div class=\"m-cl-item\"><b>发现并修复「每 15 秒两条数据库报错，记忆抽取停摆」的问题</b><span>代码查的列在真实库里不存在，而重建迁移包在版本号判断里——真实库的版本号比代码里的高，迁移永远不执行。修复后迁移改成看实际表结构：列不在就补，不再比版本号。</span></div><div class=\"m-cl-item\"><b>发现并修复「迁移期间心跳和抽取全部 database is locked」的问题</b><span>迁移整段包了排他事务，连上库那一瞬间就抢写锁，而迁移要扫三万多行归档。修复后回到不包大事务的做法：结构变更先提交，回填每五百条分批提交，失败只记警告不阻塞启动。</span></div><div class=\"m-cl-item\"><b>发现并修复「another row available 之后连接状态错乱」的问题</b><span>读取查询从来没加过锁，只有写入加了。sqlite3 的连接不是线程安全的，一边取结果一边执行就会抛这个错，紧接着所有写操作都变成 locked。修复后读写共用同一把锁。</span></div><div class=\"m-cl-item\"><b>发现并修复「回填六千多条记忆全部判成 legacy」的问题</b><span>归档表的平台字段里混着三种值（正式平台名、默认名、空串），而判据要求平台集合唯一，永远不成立。修复后空串当没记录、默认名当默认值，实测解析成功从 0 变成 4906。</span></div></div></section>";

  PAGES.changelog = {
    title: "更新日志",
    render: function () {
      return Promise.resolve("<div class=\"m-page\">" + CHANGELOG_HTML + "</div>");
    },
  };


  /* 永久记忆：mem_level=0 那一档，不衰减也不会被淡忘。
     混在几万条里翻根本找不着，所以单独一页。 */
  PAGES.permanent = {
    title: "永久记忆",
    render: function () {
      return apiGet("memory/list", { level: 0, limit: 200 }).then(function (data) {
        var items = data.items || [];
        var total = num(data.total);
        if (!items.length) {
          return '<div class="m-page"><div class="m-empty"><b>还没有永久记忆</b>' +
            '这一档不衰减、不会被淡忘，检索时永远排最前，只该放「绝不能忘」的事。<br>' +
            '想留住某条，去记忆列表里把它改成 0 级。</div></div>';
        }
        var rows = items.map(function (row) {
          var kw = (row.keywords || []).slice(0, 3).map(function (k) {
            return '<span class="m-tag">' + esc(k) + '</span>';
          }).join(" ");
          return '<section class="m-card" style="margin-bottom:10px">' +
            '<div style="font-size:13.5px;line-height:1.7">' + esc(row.text || "") + '</div>' +
            '<div style="margin-top:8px;font-size:11.5px;color:var(--m-ink-4)">' +
              txt(row.created_text, "") +
              (num(row.hit_count) ? " · 命中 " + num(row.hit_count) + " 次" : "") +
            "</div>" +
            '<div class="m-btn-row" style="margin-top:10px">' + kw +
              '<button class="m-btn" type="button" data-act="perm-unlock" data-id="' + esc(String(row.id)) + '">降成普通</button>' +
            "</div>" +
          "</section>";
        }).join("");
        return '<div class="m-page">' +
          '<div class="m-empty" style="padding:6px 4px 12px;text-align:left;font-size:12px">' +
            "共 " + items.length + " 条永久记忆" + (total ? " · 占记忆总数 " + (items.length / total * 100).toFixed(1) + "%" : "") +
            "<br>这一档不会随时间和使用次数淡忘。" +
          "</div>" + rows + "</div>";
      });
    },
  };

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
              '<div style="margin-top:6px;font-size:12px;color:var(--m-ink-3)">把「会聊天」做到像人 · v' + txt(health.version, "1.3.0") + " 测试版</div>" +
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
    if (!PAGES[name]) {
      /* 静默 return 的后果：页面停在骨架，看起来就是"一直读取中"。
         实际原因多半是浏览器还在用旧的 mobile.js（缓存没有版本参数），
         那个版本里没有这个页面。把话说清楚，别再让人猜。 */
      dom.main.innerHTML =
        '<div class="m-page"><div class="m-empty"><b>这个页面没加载上</b>' +
        "浏览器可能还在用旧版本的脚本（" + esc(String(name)) + "）。<br>" +
        "下拉刷新试试；还不行就清一下这个页面的缓存。</div></div>";
      stopProgress();
      return;
    }
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
          toast("补完了，放行 " + num(data.removed) + " 条", "ok");
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

      /* 记忆等级：视觉切换由 openSheet 的 onOpen 接管，
         这里补一个空 case，免得点击落到 default 被当成未接入。 */
      case "mem-level":
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

      /* 发说说 */
      case "perm-unlock": {
        var pid = num(node.dataset.id);
        if (!pid) return;
        node.classList.add("is-loading");
        apiPost("memory/level/set", { atom_id: pid, level: 3, reason: "从永久记忆页降级" }).then(function () {
          node.classList.remove("is-loading");
          toast("已降成「平常」", "ok");
          openPage("permanent");
        }).catch(function (err) {
          node.classList.remove("is-loading");
          toast(err.message || "没改成功", "err");
        });
        return;
      }

      case "qzone-publish": {
        openSheet({
          title: "发一条说说",
          body:
            '<textarea class="m-textarea" id="m-qz-text" style="min-height:160px" ' +
            'placeholder="想说什么就写，别写成小作文"></textarea>' +
            '<div class="m-hint" style="margin-top:8px">配图由她自己挑，不用在这里选。</div>',
          foot: '<button class="m-btn is-primary" type="button" data-act="qzone-publish-do">发出去</button>',
        });
        return;
      }
      case "qzone-publish-do": {
        var box = document.getElementById("m-qz-text");
        var text = box ? String(box.value || "").trim() : "";
        if (!text) {
          toast("还没写内容", "err");
          return;
        }
        node.classList.add("is-loading");
        apiPost("qzone/publish", { content: text }).then(function () {
          node.classList.remove("is-loading");
          closeSheet();
          toast("发出去了", "ok");
          openPage("qzone");
        }).catch(function (err) {
          node.classList.remove("is-loading");
          toast(err.message || "没发出去", "err");
        });
        return;
      }
      default:
        /* 走到这里说明这个按钮没有对应的处理分支——以前是静默 return，
           表现就是「点了没反应」，而且根本不知道是哪一个。
           现在直接把动作名报出来，谁的按钮一眼就能查到。 */
        toast("这个按钮还没接上：" + String(node.dataset.act || ""), "err");
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
    /* 弹层里也可能出现开关/输入框/下拉（比如空间页的设置项）。
       只绑 onClick 的话，这些控件在弹层里点了、改了都不会被收集。 */
    dom.sheetBody.addEventListener("click", onControl);
    dom.sheetBody.addEventListener("input", onControl);
    dom.sheetBody.addEventListener("change", onControl);
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
