/* 从绫开始 · 日记双面书页（v6 移植版）
   挂在面板「日记」板块里，数据全部走面板已有的 get / post，不自己打接口。 */
(function () {
  "use strict";
  let rootEl = null;
  let bridge = { get: null, post: null };
  const DIARY_SIGNATURE = "绫";
  function countCharacters(text) { return Array.from(String(text == null ? "" : text)).length; }
  const LIST_LIMIT = 60;
  const $ = (id) => (rootEl ? (rootEl.id === id ? rootEl : rootEl.querySelector("#" + id)) : null);
  const API = {
    async list() {
      const resp = await bridge.get("diary/list", { limit: LIST_LIMIT });
      return { items: (resp && resp.items) || [], enabled: Boolean(!resp || resp.enabled !== false) };
    },
    rowOf(dayKey) {
      return state.items.find((item) => item.day_key === dayKey) || null;
    },
    setDiaryPinned(dayKey, pinned) {
      const row = API.rowOf(dayKey);
      if (!row || !row.id) return Promise.resolve(false);
      return bridge.post("diary/pin", { id: Number(row.id), pinned: Boolean(pinned) });
    },
    deleteDiary(dayKey) {
      const row = API.rowOf(dayKey);
      if (!row || !row.id) return Promise.resolve(false);
      return bridge.post("diary/delete", { id: Number(row.id) });
    },
    rewriteDiary(dayKey) { return bridge.post("diary/generate", { day: dayKey }); },
    writeDiary() { return bridge.post("diary/generate", {}); },
  };

  /* 组件骨架：id 与原型一一对应，全部落在 .cbk-root 里，不跟面板其它板块抢。 */
  const TEMPLATE = `
  <div class="cbk-root" id="diaryWorkspace" aria-busy="true">
    <div class="cbk-bar">
      <p class="cbk-count" id="totalCount">正在整理书页</p>
      <div class="cbk-nav">
        <button id="previousButton" class="cbk-nav-btn" type="button" aria-label="翻上一页" disabled>←</button>
        <span id="navigationPosition" aria-hidden="true">— / —</span>
        <button id="nextButton" class="cbk-nav-btn" type="button" aria-label="翻下一页" disabled>→</button>
      </div>
      <button id="motionButton" class="cbk-motion" type="button" aria-pressed="false" title="切换翻页速度">正常速度</button>
      <button id="bookOnlyButton" class="cbk-full-btn" type="button" title="收掉面板，只留一本书">
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>
        <span>全屏看</span>
      </button>
      <button id="writeButton" class="cbk-write" type="button">＋ 写一篇</button>
    </div>
    <p id="disabledNotice" class="cbk-notice" hidden>日记功能当前已关闭，仍可翻阅已有书页。</p>
    <div class="cbk-float">
      <button id="floatBackButton" class="cbk-float-btn" type="button" aria-label="退出全屏">‹ 返回</button>
    </div>
    <div class="book-stage">
      <button id="directoryButton" class="directory-tab" type="button" aria-expanded="false" aria-controls="directoryDrawer"><span>目 录</span><span aria-hidden="true">☰</span></button>
      <aside id="directoryDrawer" class="directory-drawer" aria-labelledby="directoryTitle" hidden>
        <header><div><p class="eyebrow">THE COLLECTED DAYS</p><h2 id="directoryTitle">日子索引</h2></div><button id="closeDirectory" type="button" aria-label="收起目录">×</button></header>
        <nav id="diaryToc" aria-label="按年月排列的日记目录"></nav>
        <p class="drawer-foot">每一篇，都从右手边重新开始。</p>
      </aside>
      <div class="book" id="diaryBook" aria-label="双面翻页日记本" aria-busy="false">
        <div class="spread" id="spread">
          <article class="book-page page-left" id="leftPage"></article>
          <article class="book-page page-right" id="rightPage"><p class="loading-message">正在把日子装订成册……</p></article>
        </div>
        <div class="turn-shadow" aria-hidden="true"></div>
        <div class="flip-leaf" id="flipLeaf" aria-hidden="true" inert hidden>
          <div class="face front"></div>
          <div class="face back"></div>
        </div>
        <button class="edge-zone edge-previous" id="previousEdge" type="button" aria-label="左边缘：点击或向右拖动翻上一页" disabled><span aria-hidden="true">‹</span></button>
        <button class="edge-zone edge-next" id="nextEdge" type="button" aria-label="右边缘：点击或向左拖动翻下一页" disabled><span aria-hidden="true">›</span></button>
      </div>
    </div>
    <div class="cbk-foot">
      <p class="reading-hint"><kbd>←</kbd><kbd>→</kbd><span>轻点页边，或拖着纸角慢慢翻</span></p>
      <div class="writing-tools" aria-label="日记操作">
        <button id="pinButton" type="button" aria-pressed="false">夹入书签</button>
        <button id="rewriteButton" type="button">重写</button>
        <button id="deleteButton" type="button">删除</button>
      </div>
    </div>
    <p id="sourceNote" class="cbk-note"></p>
    <div id="measurementHost" class="measurement-host" aria-hidden="true" inert></div>
    <dialog id="deleteDialog" class="delete-dialog" aria-labelledby="deleteTitle">
      <form method="dialog">
        <p class="eyebrow">A LITTLE SPACE</p>
        <h2 id="deleteTitle">取下这一篇手记？</h2>
        <p>删掉之后这一篇就没了，想留就点「先留着」。</p>
        <div><button value="cancel" autofocus>先留着</button><button value="confirm">确认取下</button></div>
      </form>
    </dialog>
    <p id="toast" class="toast" role="status" hidden></p>
    <p id="liveAnnouncement" class="cbk-sr" aria-live="polite" aria-atomic="true"></p>
  </div>`;

let el = {};
function bindDom() {
  el = {
    workspace: $("diaryWorkspace"), book: $("diaryBook"), spread: $("spread"),
    left: $("leftPage"), right: $("rightPage"), leaf: $("flipLeaf"),
    previous: $("previousButton"), next: $("nextButton"),
    previousEdge: $("previousEdge"), nextEdge: $("nextEdge"),
    drawerButton: $("directoryButton"), drawer: $("directoryDrawer"),
    toc: $("diaryToc"), measurement: $("measurementHost"),
  };
}
const state = {
  items: [], spreads: [], mobilePages: [], firstSpread: new Map(), firstMobile: new Map(),
  index: 0, mobile: false, isFlipping: false, loading: true, enabled: true,
  error: null, activeTurn: null, drag: null, pendingLayout: false,
  debug: false,
};
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
const narrowScreen = matchMedia("(max-width: 899px)");
const FLIP_SLICE_COUNT = 28;
const monthNames = ["一月","二月","三月","四月","五月","六月","七月","八月","九月","十月","十一月","十二月"];
const segmenter = new Intl.Segmenter("zh-CN", { granularity: "grapheme" });
let resizeTimer, toastTimer, lastSize = "", suppressEdgeClick = false;
function makeElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function cssNumber(name) { return parseFloat(getComputedStyle(rootEl).getPropertyValue(name)); }
/* 慢动作：只缩放弹簧的刚度与阻尼，跟手拖动仍然是 1:1，松手后纸会慢慢转过去。
   刚度乘 scale²、阻尼乘 scale，动画周期大致按 1/scale 拉长，形态不变。 */
const MOTION_SCALES = [1, .5, .25];
const MOTION_LABELS = ["正常速度", "慢动作 ×0.5", "慢动作 ×0.25"];
let motionIndex = 0;
  /* 面板页面跑在 sandbox iframe 里，localStorage 会抛 SecurityError，读不到就用默认速度 */
  try { motionIndex = Number.parseInt(localStorage.getItem("congling.motionIndex") ?? "0", 10); } catch (e) { motionIndex = 0; }
if (!Number.isFinite(motionIndex) || motionIndex < 0 || motionIndex >= MOTION_SCALES.length) motionIndex = 0;
function motionScale() { return MOTION_SCALES[motionIndex]; }
/* 目录按日子正序排：最早的在前，最近写的在最后面，跟真书一样往后翻。
   夹了书签的那几篇仍旧排在最前，方便随手翻回去。 */
function sortEntries(items) { return [...items].sort((a,b) => (b.pinned - a.pinned) || a.day_key.localeCompare(b.day_key)); }
function normalizeEntries(items) {
  const seen = new Set();
  return sortEntries(items.map((item) => {
    if (!item || typeof item.day_key !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(item.day_key)) {
      throw new Error("日记条目缺少有效的 day_key。");
    }
    const date = new Date(`${item.day_key}T12:00:00Z`);
    if (Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== item.day_key) {
      throw new Error("日记日期格式不正确。");
    }
    if (seen.has(item.day_key)) throw new Error(`日记日期重复：${item.day_key}`);
    seen.add(item.day_key);
    const text = typeof item.text === "string" ? item.text : "";
    return {
      day_key: item.day_key,
      day_label: typeof item.day_label === "string" && item.day_label ? item.day_label : item.day_key,
      title: typeof item.title === "string" ? item.title : "",
      text,
      mood: typeof item.mood === "string" ? item.mood : "",
      tags: Array.isArray(item.tags) ? item.tags.filter((tag) => typeof tag === "string").slice(0, 5) : [],
      refs: Array.isArray(item.refs) ? item.refs.filter((ref) => Number.isFinite(ref)) : [],
      pinned: item.pinned === 1 || item.pinned === true ? 1 : 0,
      chars: Number.isInteger(item.chars) && item.chars >= 0 ? item.chars : countCharacters(text),
      created_text: typeof item.created_text === "string" ? item.created_text : "",
    };
  }));
}


function entryPreview(entry) { return entry.title.trim() || entry.text.replace(/\s+/gu," ").trim().slice(0,20) || "未题名的一天"; }
function visibleCount() { return state.mobile ? state.mobilePages.length : state.spreads.length; }
function currentEntry() {
  return (state.mobile ? state.mobilePages[state.index]?.entry : state.spreads[state.index]?.entry) || null;
}
function readingAnchor() {
  const page = state.mobile ? state.mobilePages[state.index] : [state.spreads[state.index]?.left,state.spreads[state.index]?.right].find((p) => p?.kind === "body");
  return { day: currentEntry()?.day_key, offset: page?.offset || 0 };
}
function setDrawer(open, restoreFocus = false) {
  if (open && (state.isFlipping || state.loading || state.debug)) return;
  el.drawer.hidden = !open;
  el.drawerButton.setAttribute("aria-expanded", String(open));
  if (open) (el.toc.querySelector(".is-current") || $("closeDirectory")).focus({preventScroll:true});
  if (!open && restoreFocus) el.drawerButton.focus({preventScroll:true});
}
function renderDirectory() {
  const fragment = document.createDocumentFragment();
  let group = "", list;
  for (const entry of state.items) {
    const month = entry.day_key.slice(0,7);
    /* 夹了书签的单独成一栏，这样同一个月份不会因为置顶被拆成两段 */
    const key = entry.pinned ? "pinned" : month;
    if (key !== group) {
      const section = makeElement("section","toc-group");
      const label = entry.pinned ? "夹了书签" : `${month.slice(0,4)} · ${monthNames[+month.slice(5)-1]}`;
      section.append(makeElement("h3","toc-month",label));
      list = makeElement("ol","toc-list"); section.append(list); fragment.append(section); group = key;
    }
    const li = makeElement("li"), button = makeElement("button","toc-entry");
    button.type = "button"; button.dataset.dayKey = entry.day_key;
    button.append(makeElement("span","toc-date",entry.day_key.slice(5)), makeElement("span",entry.pinned ? "toc-bookmark" : ""), makeElement("span","toc-title",entryPreview(entry)));
    button.setAttribute("aria-label",`${entry.day_label}，${entryPreview(entry)}${entry.pinned ? "，已置顶" : ""}`);
    li.append(button); list.append(li);
  }
  el.toc.replaceChildren(fragment);
}

/* 单一页工厂：底层、正面、背面和测量页共用，排版不会漂移。
   动态内容一律 textContent，不把接口正文当 HTML。 */
function createPage(model, side = "right", measuring = false) {
  const entry = model.entry, page = makeElement("article",`book-page page-${side}`);
  page.dataset.kind = model.kind; page.dataset.day = entry.day_key;
  if (model.kind === "chapter") {
    page.classList.add("chapter-page");
    page.append(makeElement("p","page-running","从绫开始  /  私人手记"));
    const center = makeElement("div","chapter-center");
    center.append(makeElement("span","chapter-number",String(state.items.indexOf(entry)+1).padStart(2,"0")));
    center.append(makeElement("p","chapter-label",`${entry.day_key.replaceAll("-"," / ")}  ·  页边摘句`));
    center.append(makeElement("h2","chapter-title",entryPreview(entry)));
    center.append(makeElement("span","chapter-rule"));
    const firstSentence = entry.text.trim().match(/^[\s\S]*?[。！？]/u)?.[0] || entry.text.trim().slice(0,64);
    center.append(makeElement("p","chapter-excerpt",firstSentence));
    center.append(makeElement("p","chapter-tags",entry.tags.map((tag) => `# ${tag}`).join("   ")));
    page.append(center);
    const bottom = makeElement("div","chapter-bottom");
    bottom.append(makeElement("span","","扉页 · 正文从右页起"),makeElement("span","",`${String(state.items.indexOf(entry)+1).padStart(2,"0")} / ${String(state.items.length).padStart(2,"0")}`));
    page.append(bottom);
    return page;
  }
  if (model.kind === "closing") {
    page.append(makeElement("p","page-running",entry.day_label));
    const note = makeElement("div","closing-note","把这一页，留给回味。");
    note.append(makeElement("span","","这一篇到这里，下一篇另起右页。")); page.append(note);
  } else {
    const header = makeElement("header","entry-header");
    const overline = makeElement("div","entry-overline");
    overline.append(makeElement("span","",entry.day_key.slice(0,7).replace("-"," / ")),makeElement("span","",model.part ? "私人手记 · 续页" : "私人手记"));
    const row = makeElement("div","entry-heading-row");
    row.append(makeElement("h2","entry-date",entry.day_label));
    if (entry.mood) row.append(makeElement("span","entry-mood",`· ${entry.mood}`));
    header.append(overline,row,makeElement("p","entry-meta",`${entry.created_text || "时间未记录"}   ·   ${entry.chars.toLocaleString("zh-CN")} 字${model.part ? "   ·   （续）" : ""}`));
    page.append(header);
    const title = entry.title.trim() || (model.part ? "未题名的一天" : "");
    if (title) page.append(makeElement("h3","entry-title",`${title}${model.part ? "（续）" : ""}`));
    const body = makeElement("div","diary-body diary-paragraphs");
    for (const piece of model.pieces || []) {
      const paragraph = makeElement("p",piece.dropCap ? "drop-cap" : "",piece.text);
      paragraph.dataset.paragraph = String(piece.paragraph); body.append(paragraph);
    }
    page.append(body);
  }
  if (entry.pinned && side === "right") {
    const ribbon = makeElement("span","bookmark-ribbon","签"); ribbon.setAttribute("aria-label","已置顶"); page.append(ribbon);
  }
  const footer = makeElement("footer","entry-footer");
  const folio = measuring ? "第 00 / 00 页" : `第 ${model.folio} / ${state.mobile ? state.mobilePages.length : state.spreads.length * 2} 页`;
  footer.append(makeElement("span","page-number",folio),makeElement("span","diary-signature",DIARY_SIGNATURE)); page.append(footer);
  return page;
}

/* 真实 DOM 分页：完整字体/标题/首字下沉 + 可用高度实测。
   二分搜索最后一个能装下的 grapheme，浏览器负责实际换行；
   Range 校验最后字的真实行盒，最大前缀自然落在最后一行末尾。
   片段带源段号和字符偏移，续页不重复 drop cap，不裁字、不丢字。 */
function paginateEntry(entry, width, height) {
  const paragraphs = entry.text.replace(/\r\n?/g,"\n").trim().split(/\n[ \t]*\n+/).filter((p) => p.trim()).map((p) => p.trim());
  if (!paragraphs.length) paragraphs.push("这一天还没有留下正文。");
  const source = paragraphs.map((p) => Array.from(segmenter.segment(p),(part) => part.segment));
  const result = [];
  let paragraphIndex = 0, cursor = 0, sourceOffset = 0, part = 0;
  while (paragraphIndex < source.length) {
    const model = {kind:"body", entry, part, offset:sourceOffset, pieces:[]};
    const page = createPage(model,"right",true);
    page.style.width = `${width}px`; page.style.height = `${height}px`;
    el.measurement.replaceChildren(page);
    const body = page.querySelector(".diary-body");
    const availableBottom = body.getBoundingClientRect().bottom - 1;
    if (body.clientHeight < cssNumber("--size-body") * 2 + 2) throw new Error("当前书页没有足够的正文空间，请增大窗口或缩短标题。");
    let stop = false;
    while (paragraphIndex < source.length && !stop) {
      const chars = source[paragraphIndex], remaining = chars.slice(cursor);
      const dropCap = paragraphIndex === 0 && cursor === 0;
      const p = makeElement("p",dropCap ? "drop-cap" : "");
      body.append(p);
      const fits = (count) => {
        p.textContent = remaining.slice(0,count).join("");
        if (p.getBoundingClientRect().bottom > availableBottom) return false;
        if (!p.firstChild) return true;
        const range = document.createRange();
        const end = p.firstChild.length;
        range.setStart(p.firstChild,Math.max(0,end-remaining[count-1].length));
        range.setEnd(p.firstChild,end);
        return Array.from(range.getClientRects()).every((rect) => rect.bottom <= availableBottom + 1);
      };
      let length = remaining.length;
      if (!fits(length)) {
        let low = 0, high = length;
        while (low < high) {
          const mid = Math.ceil((low+high)/2);
          if (fits(mid)) low = mid; else high = mid-1;
        }
        length = low;
      }
      if (!length) {
        p.remove();
        if (!model.pieces.length) throw new Error("正文无法排入书页，请检查字体与页高。");
        break;
      }
      const text = remaining.slice(0,length).join("");
      p.textContent = text;
      model.pieces.push({text, paragraph:paragraphIndex, start:cursor, dropCap});
      cursor += length; sourceOffset += length;
      if (cursor === chars.length) { paragraphIndex++; cursor = 0; }
      else stop = true;
    }
    result.push(model); part++;
  }
  el.measurement.replaceChildren();
  return result;
}
function rebuildBook(anchor = readingAnchor()) {
  if (state.isFlipping) { state.pendingLayout = true; return; }
  state.mobile = narrowScreen.matches;
  const width = state.mobile ? el.book.clientWidth : el.book.clientWidth / 2;
  const height = el.book.clientHeight;
  if (!width || !height) return;
  state.spreads = []; state.mobilePages = [];
  state.firstSpread.clear(); state.firstMobile.clear();
  for (const entry of state.items) {
    const chunks = paginateEntry(entry,width,height);
    state.firstSpread.set(entry.day_key,state.spreads.length);
    state.firstMobile.set(entry.day_key,state.mobilePages.length);
    state.mobilePages.push(...chunks.map((chunk) => ({...chunk})));
    state.spreads.push({entry,left:{kind:"chapter",entry},right:chunks[0]});
    for (let i=1; i<chunks.length; i+=2) {
      state.spreads.push({entry,left:chunks[i],right:chunks[i+1] || {kind:"closing",entry}});
    }
  }
  state.spreads.forEach((spread,i) => { spread.left.folio=i*2+1; spread.right.folio=i*2+2; });
  state.mobilePages.forEach((page,i) => { page.folio=i+1; });
  let index = (state.mobile ? state.firstMobile : state.firstSpread).get(anchor?.day) || 0;
  if (anchor?.day && anchor.offset) {
    if (state.mobile) {
      for (let i=index;i<state.mobilePages.length && state.mobilePages[i].entry.day_key===anchor.day;i++) {
        if (state.mobilePages[i].offset <= anchor.offset) index=i;
      }
    } else {
      for (let i=index;i<state.spreads.length && state.spreads[i].entry.day_key===anchor.day;i++) {
        if ([state.spreads[i].left,state.spreads[i].right].some((p) => p.kind==="body" && p.offset<=anchor.offset)) index=i;
      }
    }
  }
  state.index = Math.min(index,Math.max(0,visibleCount()-1));
  lastSize = `${width}:${height}:${state.mobile}`;
  state.pendingLayout = false;
  renderSpread(); updateControls();
  if (state.debug && visibleCount()>1) setupDebug();
  rootEl.dataset.ready = "true";
}
function mountPage(target, model, side) {
  const page = createPage(model,side);
  page.id = target.id; target.replaceWith(page);
  if (side==="left") el.left=page; else el.right=page;
}
function renderSpread() {
  if (!visibleCount()) {
    el.left.replaceChildren();
    const box = makeElement("div","page-error");
    box.append(makeElement("h2","",state.error ? "书页暂未打开" : "今天还没写"));
    box.append(makeElement("p","",state.error || "故事会从第一笔开始。"));
    const retry = makeElement("button","",state.error ? "重新打开" : "写第一篇");
    retry.type="button"; retry.addEventListener("click",state.error ? loadDiaries : requestWrite);
    box.append(retry); el.right.replaceChildren(box); return;
  }
  if (state.mobile) {
    el.left.replaceChildren();
    mountPage(el.right,state.mobilePages[state.index],"right");
  } else {
    const spread=state.spreads[state.index];
    mountPage(el.left,spread.left,"left"); mountPage(el.right,spread.right,"right");
  }
}
function updateControls() {
  const blocked=state.loading || state.isFlipping || state.debug || Boolean(state.error);
  const entry=currentEntry();
  for (const button of [el.previous,el.previousEdge]) button.disabled=blocked || state.index<=0;
  for (const button of [el.next,el.nextEdge]) button.disabled=blocked || state.index>=visibleCount()-1;
  el.drawerButton.disabled=blocked;
  for (const button of el.toc.querySelectorAll("button[data-day-key]")) {
    button.disabled=blocked;
    const selected=button.dataset.dayKey===entry?.day_key;
    button.classList.toggle("is-current",selected);
    if (selected) button.setAttribute("aria-current","page"); else button.removeAttribute("aria-current");
  }
  for (const id of ["pinButton","rewriteButton","deleteButton","writeButton"]) $(id).disabled=blocked || !state.enabled || (id!=="writeButton" && !entry);
  $("pinButton").textContent=entry?.pinned ? "取下书签" : "夹入书签";
  $("pinButton").setAttribute("aria-pressed",String(Boolean(entry?.pinned)));
  $("navigationPosition").textContent=visibleCount() ? `${state.index+1} / ${visibleCount()}` : "— / —";
  $("totalCount").textContent=state.loading ? "正在整理书页" : state.error ? "书页暂未载入" : `${state.items.length} 则手记 · ${visibleCount()} ${state.mobile ? "页" : "跨页"}`;
  $("disabledNotice").hidden=state.enabled;
  $("sourceNote").textContent=`共 ${state.items.length} 篇手记 · 翻页看内容`;
  el.workspace.setAttribute("aria-busy",String(state.loading || state.isFlipping));
  el.book.setAttribute("aria-busy",String(state.isFlipping));
}
function announce() {
  $("liveAnnouncement").textContent=currentEntry() ? `${currentEntry().day_label}，${entryPreview(currentEntry())}，第 ${state.index+1} / ${visibleCount()} ${state.mobile ? "页" : "跨页"}。` : state.error || "今天还没写。";
}
function buildLeafSlices() {
  el.leaf.querySelectorAll(".flip-slice").forEach((slice) => slice.remove());
  const front=el.leaf.querySelector(".front"), back=el.leaf.querySelector(".back");
  front.hidden=true; back.hidden=true;
  const bookStyle=getComputedStyle(el.book);
  const width=parseFloat(bookStyle.width)/(state.mobile ? 1 : 2), height=parseFloat(bookStyle.height);
  const sliceWidth=width/FLIP_SLICE_COUNT, slices=[];
  const fragment=document.createDocumentFragment();
  for (let i=0;i<FLIP_SLICE_COUNT;i++) {
    const slice=makeElement("div","flip-slice");
    const offset=i*sliceWidth, viewportWidth=sliceWidth+(i<FLIP_SLICE_COUNT-1 ? 1 : 0);
    slice.dataset.slice=String(i);
    slice.style.width=`${viewportWidth}px`;
    for (const template of [front,back]) {
      const isBack=template===back, face=makeElement("div",`slice-face ${isBack ? "back" : "front"}`);
      const inner=makeElement("div","slice-inner");
      // 两面各由 prepareTurn 构造一次；所有取景框克隆同一整页，1px 重叠不参与排版宽度。
      inner.style.width=`${width}px`;
      inner.style.transform=`translateX(${-(isBack ? width-offset-viewportWidth : offset)}px)`;
      inner.append(template.firstElementChild.cloneNode(true));
      face.append(inner);
      slice.append(face);
    }
    slices.push({node:slice,front:slice.firstElementChild,back:slice.lastElementChild});
    fragment.append(slice);
  }
  el.leaf.sliceSurface={width,height,sliceWidth,slices};
  el.leaf.append(fragment);
}
function renderTurn(turn, angle) {
  const progress=Math.min(1,Math.max(0,Math.abs(angle)/180));
  const curve=Math.sin(Math.PI*progress);
  const {width,height,sliceWidth,slices}=el.leaf.sliceSurface;
  // 弹簧角度拆成整页旋转和弯曲；弯曲最多 16°，起落仍归平。
  const baseAngle=Math.sign(angle)*Math.min(Math.abs(angle),80)*curve;
  const rigidAngle=angle-baseAngle;
  const angles=slices.map((_,i) => {
    const t=(i+.5)/FLIP_SLICE_COUNT;
    const w=1-.2*Math.sin(Math.PI*t);
    return (rigidAngle+baseAngle*w)*Math.PI/180;
  });
  const vertices=Array.from({length:FLIP_SLICE_COUNT+1},() => ({x:0,z:0}));
  // 积分相邻切片的切线，每片从前一片的末端起步，不能各绕书脊独立旋转。
  if (turn.direction>0) {
    for (let i=0;i<FLIP_SLICE_COUNT;i++) {
      vertices[i+1]={x:vertices[i].x+sliceWidth*Math.cos(angles[i]),z:vertices[i].z-sliceWidth*Math.sin(angles[i])};
    }
  } else {
    vertices[FLIP_SLICE_COUNT].x=width;
    for (let i=FLIP_SLICE_COUNT-1;i>=0;i--) {
      vertices[i]={x:vertices[i+1].x-sliceWidth*Math.cos(angles[i]),z:vertices[i+1].z+sliceWidth*Math.sin(angles[i])};
    }
  }
  if (state.mobile) {
    const {x,z}=vertices[FLIP_SLICE_COUNT/2];
    vertices.forEach((point) => { point.x+=width/2-x; point.z-=z; });
  }
  const bookStyle=getComputedStyle(el.book), perspective=parseFloat(bookStyle.perspective);
  const camera=bookStyle.perspectiveOrigin.split(" ").map(parseFloat);
  const cameraX=camera[0]-(state.mobile || turn.direction<0 ? 0 : width), cameraY=camera[1];
  const project=(point,y) => {
    const scale=1/(1-point.z/perspective);
    return {x:cameraX+(point.x-cameraX)*scale,y:cameraY+(y-cameraY)*scale};
  };
  let backCount=0;
  slices.forEach(({node,front,back},i) => {
    const {x,z}=vertices[i], cosine=Math.cos(angles[i]), sine=Math.sin(angles[i]);
    const a=cosine+cameraX*sine/perspective, b=cameraY*sine/perspective;
    const c=x-cameraX*z/perspective, d=-cameraY*z/perspective;
    const g=sine/perspective, w=1-z/perspective;
    // 保留统一投影与平面取景框；每片单独旋转，背面沿用同一曲面并镜像。
    node.style.transform=`matrix3d(1,0,0,0,0,1,0,0,${-cameraX/perspective},${-cameraY/perspective},1,${-1/perspective},${c},${d},0,${w}) rotateY(${angles[i]}rad) translateZ(0.02px)`;
    const showBack=a*w-c*g<0;
    front.hidden=showBack; back.hidden=!showBack;
    if (showBack) backCount++;
  });
  slices.map((slice,i) => ({slice,depth:(vertices[i].z+vertices[i+1].z)/2}))
    .sort((a,b) => a.depth-b.depth)
    .forEach(({slice},i) => { slice.node.style.zIndex=String(i+1); });
  // 光影仅绘制一次；轮廓由同一组顶点投影，覆盖完整弧面而不重复到每片。
  const outline=[...vertices.map((point) => project(point,0)),...vertices.toReversed().map((point) => project(point,height))];
  const left=Math.min(...outline.map(p=>p.x)), top=Math.min(...outline.map(p=>p.y));
  const lightWidth=Math.max(.001,Math.max(...outline.map(p=>p.x))-left);
  const lightHeight=Math.max(...outline.map(p=>p.y))-top;
  const light=el.leaf.style;
  light.setProperty("--leaf-light-left",`${left}px`);
  light.setProperty("--leaf-light-top",`${top}px`);
  light.setProperty("--leaf-light-width",`${lightWidth}px`);
  light.setProperty("--leaf-light-height",`${lightHeight}px`);
  light.setProperty("--leaf-light-clip",`polygon(${outline.map(p=>`${(p.x-left)/lightWidth*100}% ${(p.y-top)/lightHeight*100}%`).join(",")})`);
  light.setProperty("--leaf-light-position",`${(1-progress)*100}%`);
  light.setProperty("--leaf-light-opacity",String(curve*(.42+.18*backCount/FLIP_SLICE_COUNT)));
  el.book.style.setProperty("--shadow-opacity",String(curve*.48));
  el.book.style.setProperty("--shadow-scale",String(.78+curve*.46));
  el.book.style.setProperty("--shadow-blur",`${12+curve*18}px`);
  turn.angle=angle; turn.progress=progress;
}
function prepareTurn(target) {
  const direction=target>state.index ? 1 : -1;
  state.isFlipping=true; setDrawer(false);
  const from=state.index;
  let frontModel,backModel;
  if (state.mobile) {
    frontModel=state.mobilePages[from]; backModel=state.mobilePages[target];
  } else {
    frontModel=state.spreads[from][direction>0 ? "right" : "left"];
    backModel=state.spreads[target][direction>0 ? "left" : "right"];
  }
  const frontSide=state.mobile || direction>0 ? "right" : "left";
  const backSide=state.mobile || direction<0 ? "right" : "left";
  el.leaf.querySelector(".front").replaceChildren(createPage(frontModel,frontSide));
  el.leaf.querySelector(".back").replaceChildren(createPage(backModel,backSide));
  buildLeafSlices();
  el.leaf.classList.toggle("is-backward",direction<0);
  el.leaf.style.transition="none"; el.leaf.style.transform="none";
  el.leaf.hidden=false; el.book.classList.add("is-flipping");
  // 动纸挡住旧面，下面只预铺将被揭开的那半页；另一半到结束才换。
  if (state.mobile) mountPage(el.right,backModel,"right");
  else if (direction>0) mountPage(el.right,state.spreads[target].right,"right");
  else mountPage(el.left,state.spreads[target].left,"left");
  const turn={from,target,direction,progress:0,angle:0,settled:false,timer:0,frame:0,finish:null,releaseVelocity:0,samples:[]};
  state.activeTurn=turn; updateControls();
  renderTurn(turn,0);
  return turn;
}
function resetLeaf() {
  el.leaf.style.transition="none"; el.leaf.hidden=true;
  el.leaf.style.transform="none"; el.leaf.classList.remove("is-backward");
  el.leaf.querySelectorAll(".flip-slice").forEach((slice) => slice.remove());
  el.leaf.sliceSurface=null;
  el.leaf.querySelector(".front").hidden=false; el.leaf.querySelector(".back").hidden=false;
  el.leaf.querySelector(".front").replaceChildren(); el.leaf.querySelector(".back").replaceChildren();
  el.book.classList.remove("is-flipping");
  el.book.style.setProperty("--shadow-opacity","0");
  el.book.style.setProperty("--shadow-scale","1");
}
function settleTurn(turn,commit) {
  if (turn.settled || state.activeTurn!==turn) return;
  turn.settled=true; clearTimeout(turn.timer); cancelAnimationFrame(turn.frame);
  state.index=commit ? turn.target : turn.from;
  // 同一 JS 任务内先完整换底层，再隐藏并无过渡复位；两次操作之间无渲染帧。
  renderSpread(); resetLeaf();
  state.activeTurn=null; state.isFlipping=false; state.drag=null;
  if (commit) {
    el.book.classList.remove("page-land");
    void el.book.offsetWidth;
    el.book.classList.add("page-land");
    window.setTimeout(() => el.book.classList.remove("page-land"),90);
  }
  updateControls(); announce();
  if (state.pendingLayout) safeRebuild();
}
function animateTurn(turn,commit) {
  turn.finish=() => settleTurn(turn,commit);
  if (reducedMotion.matches) { turn.finish(); return; }
  const target=commit ? -turn.direction*180 : 0;
  const start=performance.now();
  let previous=start;
  const ms = motionScale();
  const stiffness=cssNumber(commit ? "--spring-stiffness" : "--spring-rebound-stiffness")*ms*ms;
  const damping=cssNumber(commit ? "--spring-damping" : "--spring-rebound-damping")*ms;
  let angle=turn.angle, velocity=turn.releaseVelocity || 0;
  const settleAt=1000/ms, deadline=1500/ms;
  const step=(now) => {
    if (turn.settled) return;
    const elapsed=now-start, dt=Math.min(.032,Math.max(.001,(now-previous)/1000)); previous=now;
    const lift=commit ? Math.min(1,elapsed/(180/ms)) : 1;
    const k=stiffness*(commit ? .34+.66*lift : 1);
    const acceleration=(target-angle)*k-damping*velocity;
    velocity+=acceleration*dt; angle+=velocity*dt;
    if (commit && Math.abs(angle-target)>3 && Math.sign(angle-target)===Math.sign(velocity)) {
      angle=target+Math.sign(angle-target)*3; velocity*=.72;
    }
    renderTurn(turn,angle);
    if ((elapsed>settleAt && Math.abs(target-angle)<.28 && Math.abs(velocity)<.8) || elapsed>deadline) {
      renderTurn(turn,target); turn.finish(); return;
    }
    turn.frame=requestAnimationFrame(step);
  };
  turn.frame=requestAnimationFrame(step);
  turn.timer=setTimeout(turn.finish,1560/ms);
}
function navigateTo(target) {
  if (state.loading || state.isFlipping || state.debug || state.error || $("deleteDialog").open || target<0 || target>=visibleCount() || target===state.index) return;
  setDrawer(false);
  if (reducedMotion.matches) {
    state.index=target; renderSpread(); updateControls(); announce(); return;
  }
  animateTurn(prepareTurn(target),true);
}
function navigate(direction) { navigateTo(state.index+direction); }
function onPointerDown(event,direction) {
  if (event.button!==0 || !event.isPrimary || state.isFlipping || state.loading || state.debug || state.error || $("deleteDialog").open) return;
  const target=state.index+direction;
  if (target<0 || target>=visibleCount()) return;
  suppressEdgeClick=false;
  // 按下先记录；真正水平拖动后才挂纸，避免普通点击出现一帧后回弹。
  state.drag={id:event.pointerId,startX:event.clientX,startY:event.clientY,lastX:event.clientX,direction,target,active:false,zone:event.currentTarget,samples:[]};
  event.currentTarget.setPointerCapture(event.pointerId);
}
function onPointerMove(event) {
  const drag=state.drag;
  if (!drag || event.pointerId!==drag.id) return;
  const horizontal=(drag.startX-event.clientX)*drag.direction, vertical=Math.abs(event.clientY-drag.startY);
  drag.lastX=event.clientX;
  if (!drag.active && vertical>Math.abs(horizontal) && vertical>8) { state.drag=null; return; }
  if (!drag.active && horizontal>4) {
    drag.active=true;
    if (!reducedMotion.matches) prepareTurn(drag.target);
    else { state.isFlipping=true; updateControls(); }
  }
  if (!drag.active) return;
  event.preventDefault();
  if (!reducedMotion.matches && state.activeTurn) {
    const width=state.mobile ? el.book.clientWidth : el.book.clientWidth/2;
    const progress=Math.min(1,Math.max(0,horizontal/width));
    const now=performance.now(); drag.samples.push({t:now,x:horizontal});
    while (drag.samples.length && now-drag.samples[0].t>105) drag.samples.shift();
    state.activeTurn.progress=progress;
    renderTurn(state.activeTurn,-drag.direction*180*progress);
  }
}
function endPointer(event,cancel=false) {
  const drag=state.drag;
  if (!drag || drag.id!==event.pointerId) return;
  const distance=(drag.startX-(event.clientX ?? drag.lastX))*drag.direction;
  state.drag=null;
  if (drag.zone.hasPointerCapture(drag.id)) drag.zone.releasePointerCapture(drag.id);
  if (!drag.active) return;
  suppressEdgeClick=true;
  const width=state.mobile ? el.book.clientWidth : el.book.clientWidth/2;
  const now=performance.now();
  drag.samples.push({t:now,x:Math.max(0,distance)});
  while (drag.samples.length && now-drag.samples[0].t>105) drag.samples.shift();
  const first=drag.samples[0], last=drag.samples[drag.samples.length-1];
  const velocityPxMs=first && last && last.t>first.t ? (last.x-first.x)/(last.t-first.t) : 0;
  const velocityDegS=velocityPxMs/Math.max(1,width)*180*1000;
  const commit=!cancel && (distance>=cssNumber("--drag-threshold") || (distance>=width*.08 && velocityDegS>420) || (distance>=width*.22 && velocityDegS>150));
  if (reducedMotion.matches) {
    state.isFlipping=false;
    if (commit) { state.index=drag.target; renderSpread(); }
    updateControls(); announce();
    if (state.pendingLayout) safeRebuild();
  } else {
    if (state.activeTurn) state.activeTurn.releaseVelocity=-drag.direction*velocityDegS;
    animateTurn(state.activeTurn,commit);
  }
}
function cancelActiveDrag() {
  if (!state.drag) return;
  const drag=state.drag; state.drag=null;
  if (drag.zone.hasPointerCapture(drag.id)) drag.zone.releasePointerCapture(drag.id);
  if (!drag.active) return;
  suppressEdgeClick=true;
  if (state.activeTurn) { state.activeTurn.releaseVelocity=0; animateTurn(state.activeTurn,false); }
  else {
    state.isFlipping=false; updateControls();
    if (state.pendingLayout) safeRebuild();
  }
}
function setupDebug() {
  // 精确 -90° 的纸在正中相机下只能看见一条线。
  // 仅把 perspective-origin 移到右侧，以同一曲面渲染器观察 −90° 时的真实背面。
  if (state.activeTurn) {
    const old=state.activeTurn; old.settled=true; clearTimeout(old.timer); cancelAnimationFrame(old.frame);
    state.activeTurn=null; state.isFlipping=false; resetLeaf();
  }
  const secondEntry=state.items[1];
  const nextStart=(state.mobile ? state.firstMobile : state.firstSpread).get(secondEntry?.day_key);
  state.index=Math.max(0,(nextStart || 1)-1);
  renderSpread();
  const turn=prepareTurn(state.index+1);
  el.book.classList.add("debug-flip"); document.body.classList.add("is-debug");
  renderTurn(turn,-90);
  $("debugNote").hidden=false;
  $("debugNote").textContent=`验收视角 · 纸张固定 −90°，相机偏右看真实背面；背面印着「${entryPreview(state.items[1] || currentEntry())}」。退出请移除 ?debug=flip。`;
  updateControls();
}
function safeRebuild() {
  try { rebuildBook(); }
  catch (error) { state.error=error.message; state.spreads=[]; state.mobilePages=[]; renderSpread(); updateControls(); }
}
function requestLayout() {
  clearTimeout(resizeTimer);
  resizeTimer=setTimeout(() => {
    const width=narrowScreen.matches ? el.book.clientWidth : el.book.clientWidth/2;
    const size=`${width}:${el.book.clientHeight}:${narrowScreen.matches}`;
    if (size===lastSize || state.loading || state.error) return;
    if (state.debug && state.activeTurn) {
      state.activeTurn.settled=true; state.activeTurn=null; state.isFlipping=false; resetLeaf();
    }
    safeRebuild();
  },cssNumber("--resize-delay"));
}
function showToast(message) {
  clearTimeout(toastTimer); $("toast").textContent=message; $("toast").hidden=false;
  toastTimer=setTimeout(() => { $("toast").hidden=true; },cssNumber("--toast-duration"));
}
function canWrite() { return !state.loading && !state.isFlipping && !state.debug && !state.error && state.enabled && !$("deleteDialog").open; }
function requestWrite() {
  if (!canWrite()) return;
  API.writeDiary(); showToast("已记录 writeDiary；原型不会生成或提交日记。");
}
async function loadDiaries() {
  state.loading=true; state.error=null; updateControls();
  try {
    const firstLoad = !state.items.length;
    const result=await API.list();
    state.items=normalizeEntries(result.items); state.enabled=result.enabled;
    await document.fonts.ready;
    state.loading=false; renderDirectory();
    /* 头一次打开直接落到最近写的那一篇（日子最大那篇），不用从最早的一页翻起 */
    if (firstLoad && state.items.length) {
      const newest = state.items.reduce((a, b) => (!a || a.day_key < b.day_key ? b : a), null);
      rebuildBook({ day: newest.day_key, offset: 0 });
    } else rebuildBook();
  } catch (error) {
    state.error=error.name==="AbortError" ? "等得有些久了，请检查连接后再试。" : error.message || "暂时无法读取日记。";
    state.items=[]; state.spreads=[]; state.mobilePages=[]; state.loading=false;
    renderDirectory(); renderSpread(); updateControls();
  }
  announce();
}
let wireAbort = null;
function wireEvents() {
  /* 每次 mount 都可能重来一遍，document / window 级监听必须能一次性摘掉 */
  if (wireAbort) wireAbort.abort();
  wireAbort = new AbortController();
  const on = { signal: wireAbort.signal };
for (const [zone,direction] of [[el.previousEdge,-1],[el.nextEdge,1]]) {
  zone.addEventListener("pointerdown",(event) => onPointerDown(event,direction));
  zone.addEventListener("pointermove",onPointerMove);
  zone.addEventListener("pointerup",(event) => endPointer(event));
  zone.addEventListener("pointercancel",(event) => endPointer(event,true));
  zone.addEventListener("lostpointercapture",(event) => endPointer(event,true));
  zone.addEventListener("click",() => {
    if (suppressEdgeClick) { suppressEdgeClick=false; return; }
    navigate(direction);
  });
}
el.previous.addEventListener("click",() => navigate(-1));
el.next.addEventListener("click",() => navigate(1));
el.drawerButton.addEventListener("click",() => setDrawer(el.drawer.hidden));
$("closeDirectory").addEventListener("click",() => setDrawer(false,true));
el.toc.addEventListener("click",(event) => {
  const button=event.target.closest("button[data-day-key]");
  if (!button || button.disabled) return;
  const target=(state.mobile ? state.firstMobile : state.firstSpread).get(button.dataset.dayKey);
  setDrawer(false,true); navigateTo(target);
});
document.addEventListener("pointerdown",(event) => {
  if (!el.drawer.hidden && !el.drawer.contains(event.target) && !el.drawerButton.contains(event.target)) setDrawer(false);
}, on);
document.addEventListener("keydown",(event) => {
  if (!rootEl || !rootEl.isConnected || !rootEl.offsetParent) return;
  if (event.key==="Escape" && !el.drawer.hidden) { setDrawer(false,true); return; }
  if (event.defaultPrevented || event.repeat || event.isComposing || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey || $("deleteDialog").open) return;
  if (event.target.closest("input,textarea,select,[contenteditable]:not([contenteditable=false])")) return;
  if (event.key==="ArrowLeft" || event.key==="ArrowRight") {
    event.preventDefault(); if (!state.drag) navigate(event.key==="ArrowLeft" ? -1 : 1);
  }
}, on);
$("pinButton").addEventListener("click",() => {
  if (!canWrite() || !currentEntry()) return;
  const anchor=readingAnchor(), entry=currentEntry(); entry.pinned=entry.pinned ? 0 : 1;
  API.setDiaryPinned(entry.day_key,entry.pinned); state.items=sortEntries(state.items);
  renderDirectory(); rebuildBook(anchor); showToast("书签已更新；只修改本次本地状态。");
});
$("rewriteButton").addEventListener("click",() => {
  if (!canWrite() || !currentEntry()) return;
  API.rewriteDiary(currentEntry().day_key); showToast("已记录 rewriteDiary；正文尚未改动。");
});
$("writeButton").addEventListener("click",requestWrite);
$("deleteButton").addEventListener("click",() => {
  if (!canWrite() || !currentEntry()) return;
  $("deleteDialog").dataset.day=currentEntry().day_key;
  $("deleteDialog").returnValue="cancel"; $("deleteDialog").showModal();
});
$("deleteDialog").addEventListener("close",() => {
  if ($("deleteDialog").returnValue!=="confirm") return;
  const day=$("deleteDialog").dataset.day, index=state.items.findIndex((entry) => entry.day_key===day);
  if (index<0) return;
  API.deleteDiary(day); state.items.splice(index,1);
  renderDirectory(); rebuildBook({day:state.items[Math.min(index,state.items.length-1)]?.day_key,offset:0});
  showToast("已从本次列表移除，未请求服务端；刷新后恢复。");
});
reducedMotion.addEventListener("change",() => {
  if (state.debug) return;
  cancelActiveDrag();
  if (reducedMotion.matches) state.activeTurn?.finish?.();
});
document.addEventListener("visibilitychange",() => {
  if (document.hidden && !state.debug) {
    cancelActiveDrag();
    state.activeTurn?.finish?.();
  }
}, on);
window.addEventListener("blur",() => {
  if (!state.debug) cancelActiveDrag();
}, on);
new ResizeObserver(requestLayout).observe(el.book);
window.addEventListener("resize",requestLayout, on);
document.fonts.addEventListener("loadingdone",() => { if (!state.loading && !state.debug) safeRebuild(); });
/* 慢动作开关：正常 → ×0.5 → ×0.25 循环，选择记在 localStorage 里 */
const motionButton = $("motionButton");
function syncMotionButton() {
  if (!motionButton) return;
  motionButton.textContent = MOTION_LABELS[motionIndex];
  motionButton.setAttribute("aria-pressed", String(motionIndex > 0));
  motionButton.dataset.scale = String(MOTION_SCALES[motionIndex]);
}
motionButton?.addEventListener("click", () => {
  motionIndex = (motionIndex + 1) % MOTION_SCALES.length;
  try { localStorage.setItem("congling.motionIndex", String(motionIndex)); } catch {}
  syncMotionButton();
  showToast(motionIndex === 0 ? "翻页已恢复常速" : `翻页放慢到 ${MOTION_SCALES[motionIndex]} 倍速`);
});
syncMotionButton();

}

  /* ---------- 全屏看书 ----------
     面板的 iframe 没开 allow-popups，弹新窗口会被拦，所以就地收壳：
     给 <html> 挂 .is-book-only，把侧栏和顶栏藏起来，书按整屏高度重排。 */
  function syncBookOnlyButton() {
    const btn = $("bookOnlyButton");
    if (!btn) return;
    const on = document.documentElement.classList.contains("is-book-only");
    btn.textContent = on ? "退出全屏" : "全屏看";
    btn.setAttribute("aria-pressed", String(on));
  }
  function toggleBookOnly() {
    const on = document.documentElement.classList.toggle("is-book-only");
    syncBookOnlyButton();
    /* 外壳收起后可用高度变了，重排一次书页 */
    window.setTimeout(() => {
      try { state.pendingLayout = false; rebuildBook(); } catch (err) { /* 重排失败不影响阅读 */ }
    }, 80);
    return on;
  }
  /* 独立页模式：URL 带 book=1 时把面板外壳收起来，只留书 */
  (function applyBookOnlyMode() {
    try {
      if (new URLSearchParams(window.location.search).get("book") !== "1") return;
      document.documentElement.classList.add("is-book-only");
      if (!String(window.location.hash || "").startsWith("#diary")) window.location.hash = "#diary";
    } catch (err) { /* 读不到就当普通模式 */ }
  })();

async function mount(container, options) {
    container.innerHTML = TEMPLATE;
    rootEl = container.querySelector(".cbk-root");
    bridge = Object.assign({ get: async () => null, post: async () => null }, options || {});
    bindDom();
    wireEvents();
    const bookOnlyBtn = $("bookOnlyButton");
    if (bookOnlyBtn) bookOnlyBtn.addEventListener("click", () => { toggleBookOnly(); });
    const floatBack = $("floatBackButton");
    if (floatBack) floatBack.addEventListener("click", () => { if (document.documentElement.classList.contains("is-book-only")) toggleBookOnly(); });
    syncBookOnlyButton();
    await loadDiaries();
  }

  window.ConglingDiaryBook = { mount, refresh: () => loadDiaries() };
})();

