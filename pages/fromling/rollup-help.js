/* 醒后补课参数手册：只读取当前填写值，不保存配置、不调用模型。 */
const NIGHT_ROLLUP_GUIDE = [
  {
    key: "waking_timeout_minutes", title: "补课状态超时（分钟）", default: 5, group: "状态与开关",
    logic: "限制“刚醒，补课中”状态最多持续多久，最低 1 分钟；到时转为“醒着”。它不删除未读消息、不让 bot 入睡，也不是摘要模型的请求超时。",
    example: "设为 5：21:00 醒来后一直没完成补课，约 21:05 的作息心跳会结束该状态；有新消息并完成爬楼时，可以更早结束。想减少等待可用 2，群消息较多可用 5–10。",
    relation: "独立于“启用醒后补课”和“醒后静默待机”；前者控制摘要与回复，后者控制安静多久后准备想睡文案。",
  },
  {
    key: "rollup.enabled", title: "启用醒后补课", default: false, group: "状态与开关",
    logic: "开启后，阅读遗留未读消息，再打分并准备历史补课回复，同时启用醒后静默待机检查；关闭后不补答历史问题。",
    example: "希望 bot 补答之前遗漏的问题时开启；只想阅读历史背景、回复当前消息时关闭，爬楼开启时仍会分块阅读历史。",
    relation: "“爬楼机制”负责历史阅读与摘要；补课负责挑选历史问题回应。关闭补课不等于关闭阅读，也不等于关闭 5 分钟状态兜底。",
  },
  {
    key: "rollup.chunk_size", title: "分块条数", default: 45, group: "消息分块",
    logic: "把增量消息按顺序拆成摘要块；达到条数上限或时间跨度上限，先满足哪一个就切块。最低 1 条。",
    example: "设为 45：120 条时间密集的消息大致拆成 45、45、30 三块。群很活跃可试 60；想让每块更短、便于模型理解可试 30。",
    relation: "条数越小，每块更轻，但同一批历史可能需要更多次摘要调用；条数越大，单次上下文更长。",
  },
  {
    key: "rollup.chunk_window_minutes", title: "分块时间窗（分钟）", default: 60, group: "消息分块",
    logic: "控制同一摘要块内的消息时间跨度，最低 1 分钟；它看消息时间戳，不是让 bot 再等这么久才补课。",
    example: "设为 60：09:00 的消息和 10:05 的消息会被分到不同块，即使总条数不足 45。话题变化快的群可试 30，讨论连贯的群可保留 60。",
    relation: "和“分块条数”一起生效，先触及任一条件就分块。",
  },
  {
    key: "rollup.chunk_hard_cap", title: "单块硬上限（条）", default: 80, group: "消息分块",
    logic: "分块保护上限；代码会把实际硬上限取为它与“分块条数”中较大的值。正常情况下，分块条数会先触发切块。",
    example: "分块条数 45、硬上限 80 时，一般仍按 45 条切块；只把 80 提高到 120，不会让每块自动变成 120 条。",
    relation: "建议保持硬上限 ≥ 分块条数；它不是整次补课读取历史的总条数限制。",
  },
  {
    key: "rollup.hi_threshold", title: "高分线", default: 80, group: "评分与回复",
    logic: "评分达到高分线的清单项进入待回复；低于它的项目再与低分线比较。评分由 at_guard 或 congling 的现有判据提供。",
    example: "高分线 80、低分线 50：85 分可回复，65 分记为待追问，30 分静默。只想补答明显点名的问题，可把高分线试调到 90。",
    relation: "建议使用 0–100，且低分线 ≤ 高分线。提高高分线会减少候选回复；不同判定模型的评分尺度需要观察日志后调整。",
  },
  {
    key: "rollup.mid_threshold", title: "低分线", default: 50, group: "评分与回复",
    logic: "评分达到低分线但未达到高分线，记为待追问或低优先项目；低于低分线则静默。当前中间档不会自动追加一次小模型补判。",
    example: "设为 50：50–79 分进入中间档。想少记录边缘话题，可试 60；想保留更多待追问候选，可试 40。",
    relation: "降低低分线不会直接增加正式回复，正式回复仍由高分线筛选。",
  },
  {
    key: "rollup.max_replies_per_wake", title: "单次最多回几条", default: 3, group: "评分与回复",
    logic: "同一会话在本次醒来的全部摘要块中，合计最多选 N 个问题，最低 1；合并为一段回复，不等于发 N 条消息。",
    example: "设为 3：三块合计有 8 个高分问题时，只取前 3 个合并回应；想少补答可设 1。",
    relation: "补课回复成功发送后才提交游标；不会在每条普通回复时重跑补课。",
  },
  {
    key: "rollup.mirror_livingmemory", title: "镜像到 LivingMemory", default: false, group: "记忆与待机", pending: true,
    logic: "设计用途是把补课里形成的偏好、结论、约定写入 LivingMemory；当前补课流程尚未读取这个开关，它是预留参数。",
    example: "希望记住“下周三一起看电影”这样的长期约定，是这个参数预期的场景；当前仅开启此项不会完成补课记忆镜像。",
    relation: "congling 普通记忆的 LivingMemory 镜像是另一条流程，不能据此判断本开关已经生效。",
  },
  {
    key: "rollup.wake_idle_minutes", title: "醒后静默待机（分钟）", default: 10, group: "记忆与待机",
    logic: "仅本次被消息或手动叫醒、且当前精力未满时，从醒来或最后消息中较晚的时间起算；连续安静 N 分钟后准备一次提示，0 关闭。",
    example: "设为 10：21:00 醒来，21:06 有新消息，则约 21:16 才准备提示。同一段静默不会每分钟重复触发；有新消息或再次醒来后重新计时。",
    relation: "新消息会撤销旧提示；自然醒、满精力和人格切换不会沿用旧提示，也不自动重新入睡。",
  },
  {
    key: "rollup.idle_merge_seconds", title: "待机并入窗口（秒）", default: 60, group: "记忆与待机",
    logic: "想睡提示生成后的有效期；仅仍有效且未收到新消息、作息和人格仍符合条件时，允许并入已经进行中的回复。",
    example: "设为 60：21:16:00 准备提示，21:17:01 取出时已经过期；21:16:10 若收到新消息，立即丢弃旧提示。",
    relation: "不会为了等窗口主动发消息；超时丢弃，不单独发送。0 表示不保留待发提示。",
  },
  {
    key: "rollup.wake_mood_pool", title: "强制唤醒情绪池", default: [], group: "记忆与待机",
    logic: "被消息或手动叫醒且精力仍低时，随机抽取一个情绪作为一次性的内部语气提示；自然醒或满格自动醒不会应用这段强制唤醒情绪。",
    example: "可填“困倦、朦胧、温柔”，让没睡够时的接话更松缓；留空则不指定情绪，使用简短情绪词更方便控制语气。",
    relation: "需要启用醒后补课；情绪是语气参考，不应直接作为括号里的内心独白输出。",
  },
];

function nightRollupHelpButton() {
  return '<button type="button" class="rollup-help-button" data-rollup-help aria-label="查看醒后补课配置说明" aria-haspopup="dialog" aria-controls="night-rollup-help" title="配置逻辑与场景示例">？</button>';
}

function nightRollupValue(key) {
  const path = "night." + key;
  const el = Array.from(document.querySelectorAll("#view [data-path]")).find(node => node.dataset.path === path);
  if (!el) return "—";
  if (el.dataset.type === "bool") return el.checked ? "开启" : "关闭";
  return String(el.value || "").trim() || "空";
}

function nightRollupDefault(value) {
  return typeof value === "boolean" ? (value ? "开启" : "关闭") : Array.isArray(value) ? "空" : String(value);
}

function openNightRollupHelp(trigger) {
  if (document.getElementById("night-rollup-help")) return;
  const scroll = window.scrollY;
  const overflow = document.body.style.overflow;
  const dialog = document.createElement("dialog");
  dialog.id = "night-rollup-help";
  dialog.className = "rollup-help-dialog";
  dialog.setAttribute("aria-labelledby", "rollup-help-title");
  dialog.setAttribute("aria-describedby", "rollup-help-intro");
  dialog.innerHTML = `
    <header class="rollup-help-header"><div><h2 id="rollup-help-title">醒后补课 · 配置说明</h2><p id="rollup-help-intro">参数作用、配合逻辑与场景示例；当前填写值包含尚未保存的修改。</p></div><button type="button" class="nj-btn tiny" data-help-close aria-label="关闭醒后补课配置说明">关闭</button></header>
    <div class="rollup-help-search"><label for="rollup-help-query">查找参数</label><input id="rollup-help-query" type="search" placeholder="例如：超时、分块、评分、静默" autocomplete="off" /></div>
    <div class="rollup-help-body">
      <section class="rollup-help-overview"><h3>先区分两个时钟</h3><p><b>补课状态超时 5 分钟</b>：最多等多久退出“刚醒，补课中”。<br><b>醒后静默待机 10 分钟</b>：没人说话多久后准备一次想睡提示。</p><div class="rollup-help-flow"><span>醒来</span><span>分块摘要</span><span>评分筛选</span><span>合并回复</span></div><p>补课流程通常在醒后的回复入口执行；开启开关不等于立即主动把全部历史发出去。</p></section>
      <section class="rollup-help-presets"><h3>起步配置示例</h3><p><b>普通群聊：</b>超时 5，分块 45 条 / 60 分钟，硬上限 80，高分线 80、低分线 50，回复上限 3，静默待机 10。</p><p><b>少插话：</b>超时 5，分块 30 条 / 30 分钟，高分线 90、低分线 60，回复上限 1，静默待机 0；其他项可保留默认。</p><p>示例供参考，查看手册不会修改或保存配置。</p></section>
      ${NIGHT_ROLLUP_GUIDE.map(item => `<article class="rollup-help-item" data-help-key="${esc(item.key)}"><div class="rollup-help-item-head"><span>${esc(item.group)}</span>${item.pending ? '<b class="rollup-help-pending">当前未接入</b>' : ""}</div><h3>${esc(item.title)}</h3><div class="rollup-help-values">当前填写：${esc(nightRollupValue(item.key))} · 默认：${esc(nightRollupDefault(item.default))}</div><dl><dt>配置逻辑</dt><dd>${esc(item.logic)}</dd><dt>场景示例</dt><dd>${esc(item.example)}</dd><dt>参数配合</dt><dd>${esc(item.relation)}</dd></dl></article>`).join("")}
      <p class="rollup-help-empty" hidden>没有匹配的参数，试试“超时”“分块”或“静默”。</p>
    </div>
    <footer class="rollup-help-footer">共 ${NIGHT_ROLLUP_GUIDE.length} 项参数 · 修改设置后请回到页面点击“保存本页设置”</footer>`;
  document.body.appendChild(dialog);
  const restore = () => {
    document.body.style.overflow = overflow;
    dialog.remove();
    window.scrollTo(0, scroll);
    if (trigger?.isConnected) trigger.focus({ preventScroll: true });
  };
  dialog.addEventListener("close", restore, { once: true });
  // 搜索框会优先消费 Escape；弹窗捕获后明确关闭并恢复焦点。
  dialog.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      dialog.close();
    }
  }, true);
  dialog.querySelector("[data-help-close]").onclick = () => dialog.close();
  dialog.addEventListener("click", event => {
    const rect = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close();
  });
  dialog.querySelector("#rollup-help-query").oninput = event => {
    const query = event.target.value.trim().toLowerCase();
    let count = 0;
    dialog.querySelectorAll("[data-help-key]").forEach(card => {
      card.hidden = !card.textContent.toLowerCase().includes(query) && !card.dataset.helpKey.toLowerCase().includes(query);
      if (!card.hidden) count++;
    });
    dialog.querySelector(".rollup-help-empty").hidden = count > 0;
  };
  document.body.style.overflow = "hidden";
  dialog.showModal();
  dialog.querySelector("#rollup-help-query").focus();
}

document.addEventListener("click", event => {
  const trigger = event.target.closest("[data-rollup-help]");
  if (trigger) openNightRollupHelp(trigger);
});
