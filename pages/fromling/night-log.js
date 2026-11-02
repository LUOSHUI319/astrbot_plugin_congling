/* 长夜精力日志。两套渲染入口共用，查询不会触发作息或模型调用。 */
let nightLogHours = "24";
let nightLogCategory = "all";
let nightLogBefore = 0;
let nightLogAuto = true;
let nightLogTimer = null;

function nightLogNumber(value) {
  const n = Number(value || 0);
  return n.toLocaleString("zh-CN", { maximumFractionDigits: 3 });
}

function nightLogTime(ts) {
  return ts ? new Date(Number(ts) * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
}

function nightLogDetail(row) {
  const d = row.details || {};
  const f = nightLogNumber;
  if (row.event === "message_drain") return `收到 1 条消息，配置每条消耗 ${f(d.rate)}；先按时间恢复，再扣消息精力。消息经过长夜入口即计数，后续是否回复由其他模块判断。`;
  if (row.event === "fatigue_drain") return `${d.fatigue === "noon" ? "午间" : "夜间"}疲劳时段 ${f(d.minutes)} 分钟 × 每分钟 ${f(d.rate)}；下限为 0。`;
  if (row.event === "awake_recover") return `非疲劳时段 ${f(d.minutes)} 分钟 × 每分钟 ${f(d.rate)}；不超过精力上限。`;
  if (row.event === "sleep_recover") return `睡眠 ${f(d.minutes)} 分钟，基础每分钟恢复 ${f(d.rate)}，按各睡眠阶段系数分段计算；计算恢复 ${f(d.calculated_gain)}，实际增量受精力上限限制。`;
  if (row.event === "closing") return `命中结束语「${d.word || ""}」，${d.strength === "strong" ? "强档" : "中档"}。${d.allow_sleep ? "当前允许真睡，回复后进入睡眠。" : "真睡开关关闭。"}收线可以在精力充足时触发。`;
  if (row.event === "sleep") return `${row.reason_label || row.reason}；${d.sleep_kind === "noon" ? "短觉" : "夜间睡眠"}，预计 ${nightLogTime(d.wake_at)} 醒。`;
  if (row.event === "wake") return `${row.reason_label || row.reason}；到点或精力满格自动醒都保留满精力，被消息或手动叫醒恢复到配置值。`;
  if (row.event === "wake_check") {
    const a = d.attempt || {};
    return `${row.reason || ""}；${a.woke ? "叫醒成功" : "未叫醒，保持睡眠"}；累计 ${a.count || 0} 次，所需 ${a.needed || 0} 次，概率 ${f(Number(a.chance || 0) * 100)}%。`;
  }
  if (row.event === "sleep_guard") return `疲劳期已处理 ${d.turns || 0} 条，至少需要 ${d.required || 0} 条；暂不入睡，精力下限保护为 0.5。`;
  if (row.event === "sleep_skipped") return `原计划原因：${row.reason_label || row.reason}；真睡开关关闭。`;
  if (row.event === "snapshot") return `开始观察时的真实状态${row.reason_label ? "；已保存的入睡原因：" + row.reason_label : ""}。此前逐次变化没有历史流水。`;
  return row.reason_label || "";
}

function nightLogTrend(items, max) {
  const rows = items.slice().reverse();
  if (rows.length < 2) return '<div class="nj-empty">累计至少两条精力记录后显示趋势</div>';
  const points = [];
  const low = Number(rows[0].ts), high = Number(rows[rows.length - 1].ts);
  const y = v => 110 - Math.max(0, Math.min(max, Number(v))) / max * 90;
  rows.forEach((r, i) => {
    const x = high > low ? 30 + (Number(r.ts) - low) / (high - low) * 660 : 30 + i / (rows.length - 1) * 660;
    points.push([x, y(r.energy_before)], [x, y(r.energy_after)]);
  });
  return `<svg class="night-log-chart" viewBox="0 0 720 150" role="img" aria-label="当前页记录的精力变化趋势">
    <line x1="30" x2="690" y1="20" y2="20" class="grid"/><line x1="30" x2="690" y1="110" y2="110" class="grid"/>
    <text x="4" y="24">${esc(max)}</text><text x="12" y="114">0</text>
    <polyline fill="none" points="${points.map(p => p.map(v => v.toFixed(2)).join(",")).join(" ")}"/>
    ${rows.map((r, i) => `<circle cx="${points[i * 2 + 1][0].toFixed(2)}" cy="${points[i * 2 + 1][1].toFixed(2)}" r="3"><title>${esc(nightLogTime(r.ts) + " · " + r.label + " · " + nightLogNumber(r.energy_before) + " → " + nightLogNumber(r.energy_after))}</title></circle>`).join("")}
    <text x="30" y="140">${esc(nightLogTime(rows[0].ts))}</text><text x="690" y="140" text-anchor="end">${esc(nightLogTime(rows[rows.length - 1].ts))}</text>
  </svg>`;
}

function nightLogPage(st, data) {
  const f = nightLogNumber, max = Number(st.energy_max || 100);
  const items = data?.items || [];
  const modes = { asleep: "睡着中", awake: "醒着", waking: "刚醒，补课中" };
  const status = st.enabled === false ? "长夜未启用" : modes[st.mode] || "状态暂不可用";
  const options = (pairs, value) => pairs.map(([v, label]) => `<option value="${v}" ${String(value) === v ? "selected" : ""}>${label}</option>`).join("");
  return `<section class="nj-card night-log-page">
    <div class="nj-card-head"><h3>精力日志</h3><span class="note">全局精力 · ${esc(status)}</span></div>
    <div class="night-log-status">
      <strong>${esc(f(st.energy))}<small> / ${esc(max)}</small></strong>
      <div>${esc(st.sleep_reason_label ? "本次入睡：" + st.sleep_reason_label : "当前：" + status)}
      ${st.wake_at ? `<br>入睡 ${esc(nightLogTime(st.sleep_at))} · 预计 ${esc(nightLogTime(st.wake_at))} 醒` : ""}</div>
    </div>
    <p class="night-log-help">收到消息每条扣 ${esc(f(st.energy_per_message))}；疲劳时段每分钟扣 ${esc(f(st.energy_per_minute))}；非疲劳时段每分钟恢复 ${esc(f(st.energy_recover_per_minute))}，睡眠另乘阶段系数。精力 ≤ 0.5 且满足疲劳期最低 ${esc(st.min_turns_before_sleep || 0)} 条消息才按精力入睡，结束语和最晚入睡时间也可触发。${st.wake_when_energy_full ? "睡眠中精力达到上限就自动醒来，无需等到计划起床时间。" : ""}</p>
    <div class="night-log-controls">
      <label>时间<select id="night-log-hours">${options([["1", "最近 1 小时"], ["6", "最近 6 小时"], ["24", "最近 24 小时"], ["0", "保留期内全部"]], nightLogHours)}</select></label>
      <label>事件<select id="night-log-category">${options([["all", "全部事件"], ["drain", "精力消耗"], ["recover", "精力恢复"], ["state", "睡眠与判定"]], nightLogCategory)}</select></label>
      <button class="nj-btn tiny" id="night-log-auto" aria-pressed="${nightLogAuto}">${nightLogAuto ? "暂停自动刷新" : "开启自动刷新"}</button>
      <button class="nj-btn tiny" id="night-log-refresh">刷新最新</button>
      <span class="night-log-help">${nightLogBefore ? "历史页，自动刷新已暂缓" : nightLogAuto ? "每 15 秒刷新" : "自动刷新已暂停"}</span>
    </div>
    <div class="night-log-help">观察始于 ${esc(nightLogTime(data?.observed_since))}；保留 7 天，最多 20,000 条。上限处没有实际变化的时间恢复不重复记账。查询不调用模型。</div>
    ${data?.__error ? `<div class="nj-empty" role="alert">日志读取失败：${esc(data.__error)}<br>点击“刷新最新”重试</div>` : `
    <details class="night-log-trend" open><summary>当前页精力趋势（${items.length} 条，按时间排列）</summary>${nightLogTrend(items, max)}</details>
    <div class="night-log-list">${items.length ? items.map(row => {
      const delta = Number(row.delta || 0);
      return `<details class="night-log-row">
        <summary><time>${esc(nightLogTime(row.ts))}</time><span class="night-log-event">${esc(row.label)}${row.reason_label ? `<small>${esc(row.reason_label)}</small>` : ""}</span><span class="night-log-values">${esc(f(row.energy_before))} → ${esc(f(row.energy_after))}</span><b class="${delta < 0 ? "spend" : delta > 0 ? "gain" : "same"}">${delta > 0 ? "+" : ""}${esc(f(delta))}</b></summary>
        <div class="night-log-details"><p>${esc(nightLogDetail(row))}</p>
        <p>状态 ${esc(modes[row.mode_before] || row.mode_before)} → ${esc(modes[row.mode_after] || row.mode_after)}${row.session_id ? " · 会话 " + esc(row.session_id) : ""}${row.msg_id ? " · 消息 " + esc(row.msg_id) : ""} · 记录 #${esc(row.id)}</p></div>
      </details>`;
    }).join("") : '<div class="nj-empty">当前筛选下暂无日志。新事件发生后会出现在这里。</div>'}</div>
    <div class="night-log-controls"><button class="nj-btn tiny" id="night-log-latest" ${!nightLogBefore ? "disabled" : ""}>回到最新</button><button class="nj-btn tiny" id="night-log-older" ${!data?.has_more ? "disabled" : ""}>更早记录</button><span class="night-log-help">最新在上，当前 ${items.length} 条</span></div>`}
  </section>`;
}

function nightLogBind(data) {
  const refresh = () => renderNight();
  [["night-log-hours", v => nightLogHours = v], ["night-log-category", v => nightLogCategory = v]].forEach(([id, set]) => {
    const el = document.getElementById(id);
    if (el) el.onchange = () => { set(el.value); nightLogBefore = 0; refresh(); };
  });
  const bind = (id, action) => { const el = document.getElementById(id); if (el) el.onclick = action; };
  bind("night-log-auto", () => { nightLogAuto = !nightLogAuto; refresh(); });
  bind("night-log-refresh", () => { nightLogBefore = 0; refresh(); });
  bind("night-log-latest", () => { nightLogBefore = 0; refresh(); });
  bind("night-log-older", () => { nightLogBefore = Number(data.next_before_id || 0); refresh(); });
  if (!nightLogTimer) nightLogTimer = setInterval(() => {
    // 翻历史、查看展开详情、选择文本和编辑筛选时保持页面稳定。
    if (currentView !== "night" || nightTab !== "log" || !nightLogAuto || nightLogBefore || document.hidden ||
        document.querySelector(".night-log-row[open]") || window.getSelection()?.toString() ||
        document.activeElement?.matches("select,input")) return;
    const scroll = window.scrollY;
    Promise.resolve(refresh()).then(() => window.scrollTo(0, scroll)).catch(() => {});
  }, 15000);
}

function nightLogFetch() {
  return get("night/logs", { hours: nightLogHours, category: nightLogCategory, before_id: nightLogBefore, limit: 100 })
    .catch(err => ({ __error: String(err?.message || err) }));
}
