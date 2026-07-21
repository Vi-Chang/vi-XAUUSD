/* XAUUSD 交易分析終端 — Dashboard 前端
 * 圖表:TradingView lightweight-charts v4(自托管,Apache 2.0)
 * 資料一律來自本系統 API/DB,確保與分析引擎同源。
 */
"use strict";

const TF_SEC = { "15M": 900, "1H": 3600, "4H": 14400, "1D": 86400 };
const C = {
  bull: "#26A69A", bear: "#EF5350", info: "#58A6FF",
  warn: "#F0A020", danger: "#F85149", dim: "#8B949E",
};

const S = {
  tf: "15M",
  chart: null, candles: null, volume: null,
  lastBar: null, barTimes: [],
  zonePrims: [], priceLines: [], eventPrims: [],
  analysis: null, events: [],
  prevBid: null, countdownTarget: null,
};

const $ = (id) => document.getElementById(id);
const unskel = (el) => el && el.classList.remove("skel");
const fmt = (v, d = 2) => (v == null ? "–" : Number(v).toFixed(d));

/* ═══ 圖表初始化 ═══ */
function initChart() {
  const host = $("chart");
  S.chart = LightweightCharts.createChart(host, {
    layout: { background: { color: "transparent" }, textColor: C.dim,
              fontFamily: "'JetBrains Mono', ui-monospace, Consolas, monospace" },
    grid: { vertLines: { color: "#1a212b" }, horzLines: { color: "#1a212b" } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#21262D" },
    rightPriceScale: { borderColor: "#21262D" },
    autoSize: true,
  });
  S.candles = S.chart.addCandlestickSeries({
    upColor: C.bull, downColor: C.bear, borderVisible: false,
    wickUpColor: C.bull, wickDownColor: C.bear,
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
  });
  S.volume = S.chart.addHistogramSeries({
    priceFormat: { type: "volume" }, priceScaleId: "vol",
  });
  S.chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
}

/* ═══ 區域色帶 primitive(candidate_levels 支撐/壓力區)═══ */
function zonePrimitive(priceLow, priceHigh, color) {
  return {
    updateAllViews() {},
    paneViews() {
      return [{
        renderer: () => ({
          draw(target) {
            target.useBitmapCoordinateSpace((scope) => {
              const y1 = S.candles.priceToCoordinate(priceHigh);
              const y2 = S.candles.priceToCoordinate(priceLow);
              if (y1 == null || y2 == null) return;
              const ctx = scope.context;
              const top = Math.min(y1, y2) * scope.verticalPixelRatio;
              const h = Math.max(1, Math.abs(y2 - y1) * scope.verticalPixelRatio);
              ctx.fillStyle = color;
              ctx.fillRect(0, top, scope.bitmapSize.width, h);
            });
          },
        }),
      }];
    },
  };
}

/* ═══ 事件垂直線 primitive(高影響事件時間軸標記)═══ */
function eventLinePrimitive(timeSec, label) {
  return {
    updateAllViews() {},
    paneViews() {
      return [{
        renderer: () => ({
          draw(target) {
            target.useBitmapCoordinateSpace((scope) => {
              const x = S.chart.timeScale().timeToCoordinate(timeSec);
              if (x == null) return;
              const ctx = scope.context;
              const px = x * scope.horizontalPixelRatio;
              ctx.strokeStyle = "rgba(240,160,32,.55)";
              ctx.setLineDash([4 * scope.verticalPixelRatio, 4 * scope.verticalPixelRatio]);
              ctx.lineWidth = Math.max(1, scope.horizontalPixelRatio);
              ctx.beginPath();
              ctx.moveTo(px, 0);
              ctx.lineTo(px, scope.bitmapSize.height);
              ctx.stroke();
              ctx.setLineDash([]);
              ctx.fillStyle = C.warn;
              ctx.font = `${11 * scope.verticalPixelRatio}px sans-serif`;
              ctx.fillText("⚠ " + label, px + 4 * scope.horizontalPixelRatio,
                           14 * scope.verticalPixelRatio);
            });
          },
        }),
      }];
    },
  };
}

function clearOverlays() {
  for (const p of [...S.zonePrims, ...S.eventPrims]) {
    try { S.candles.detachPrimitive(p); } catch (e) { /* noop */ }
  }
  S.zonePrims = []; S.eventPrims = [];
  for (const pl of S.priceLines) {
    try { S.candles.removePriceLine(pl); } catch (e) { /* noop */ }
  }
  S.priceLines = [];
  S.candles.setMarkers([]);
}

/* ═══ 疊加層:zones / 劇本價位 / 結構事件 / 事件時間 ═══ */
async function applyOverlays() {
  if (!S.analysis || !S.barTimes.length) return;
  clearOverlays();
  const a = S.analysis;

  const zoneSets = [
    [a.key_levels.strong_support_zones, "rgba(38,166,154,.16)"],
    [a.key_levels.weak_support_zones, "rgba(38,166,154,.07)"],
    [a.key_levels.strong_resistance_zones, "rgba(239,83,80,.16)"],
    [a.key_levels.weak_resistance_zones, "rgba(239,83,80,.07)"],
  ];
  for (const [zones, color] of zoneSets) {
    for (const z of zones || []) {
      const p = zonePrimitive(z.price_low, z.price_high, color);
      S.candles.attachPrimitive(p);
      S.zonePrims.push(p);
    }
  }

  // 觸發中/準備中劇本的 Entry / SL / Targets 虛線
  for (const [sc, tag] of [[a.long_scenario, "多"], [a.short_scenario, "空"]]) {
    if (!sc || !["PREPARE", "TRIGGERED"].includes(sc.status)) continue;
    const rp = sc.resolved_prices || {};
    const mk = (id, color, title) => {
      const lv = rp[id];
      if (!lv) return;
      const price = (lv.price_low + lv.price_high) / 2;
      S.priceLines.push(S.candles.createPriceLine({
        price, color, lineWidth: 1,
        lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true, title: `${tag}${title}`,
      }));
    };
    mk(sc.entry_zone_id, C.info, "進場");
    mk(sc.stop_loss_id, C.danger, "賠錢出場");
    (sc.target_ids || []).forEach((tid, i) => mk(tid, C.bull, `目標${i + 1}`));
  }

  // 結構事件標記(BOS/CHoCH/假突破)
  try {
    const evs = await (await fetch(`/api/structure/events?timeframe=${S.tf}&limit=40`)).json();
    const barSet = new Set(S.barTimes);
    const markers = [];
    for (const ev of evs) {
      let t = ev.time - (ev.time % TF_SEC[S.tf]);
      if (!barSet.has(t)) {
        t = S.barTimes.findLast((b) => b <= ev.time);
        if (t == null) continue;
      }
      const up = ev.event_type.endsWith("_UP") || ev.event_type === "FAILED_BREAKDOWN";
      const label = MSG.event[ev.event_type] || ev.event_type;
      markers.push({
        time: t,
        position: up ? "belowBar" : "aboveBar",
        color: ev.still_valid ? (up ? C.bull : C.bear) : C.dim,
        shape: up ? "arrowUp" : "arrowDown",
        text: label,
      });
    }
    markers.sort((x, y) => x.time - y.time);
    S.candles.setMarkers(markers);
  } catch (e) { console.warn("structure events failed", e); }

  // 高影響事件垂直線(僅畫得出的時間;未來事件由倒數卡涵蓋)
  for (const ev of S.events) {
    if (ev.impact !== "HIGH") continue;
    const p = eventLinePrimitive(ev.time, ev.name_zh || ev.name);
    S.candles.attachPrimitive(p);
    S.eventPrims.push(p);
  }
}

/* ═══ K 棒載入與週期切換 ═══ */
async function loadCandles(tf, keepRange) {
  const saved = keepRange ? S.chart.timeScale().getVisibleLogicalRange() : null;
  const rows = await (await fetch(`/api/candles?timeframe=${tf}&limit=300`)).json();
  const bars = rows.map((r) => ({ time: r.time, open: r.open, high: r.high,
                                  low: r.low, close: r.close }));
  const vols = rows.map((r) => ({ time: r.time, value: r.volume,
    color: r.close >= r.open ? "rgba(38,166,154,.45)" : "rgba(239,83,80,.45)" }));
  S.candles.setData(bars);
  S.volume.setData(vols);
  S.barTimes = bars.map((b) => b.time);
  S.lastBar = bars.length ? { ...bars[bars.length - 1] } : null;
  if (saved) S.chart.timeScale().setVisibleLogicalRange(saved);
  else S.chart.timeScale().fitContent();
  const skel = $("chart-skeleton");
  if (skel && bars.length) skel.remove();
  if (!bars.length) {
    const skelEl = $("chart-skeleton");
    if (skelEl) skelEl.querySelector("span").textContent =
      "資料庫尚無 K 棒(等待第一次排程分析寫入)";
  }
  await applyOverlays();
}

function switchTF(tf) {
  if (tf === S.tf) return;
  S.tf = tf;
  document.querySelectorAll(".tf-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.tf === tf));
  loadCandles(tf, true).catch(console.error);
}

/* ═══ 即時 tick → 未收線 K 棒跳動 + 價格區 ═══ */
function onTick(t) {
  updatePricePanel(t.bid, t.ask, t.spread);
  if (!S.lastBar) return;
  const sec = TF_SEC[S.tf];
  const boundary = S.lastBar.time + sec;
  if (t.time >= boundary) {
    const newTime = t.time - (t.time % sec);
    S.lastBar = { time: newTime, open: t.mid, high: t.mid, low: t.mid, close: t.mid };
    S.barTimes.push(newTime);
  } else {
    S.lastBar.close = t.mid;
    S.lastBar.high = Math.max(S.lastBar.high, t.mid);
    S.lastBar.low = Math.min(S.lastBar.low, t.mid);
  }
  S.candles.update(S.lastBar);
}

function updatePricePanel(bid, ask, spread) {
  const bidEl = $("px-bid"), askEl = $("px-ask"), spEl = $("px-spread");
  [bidEl, askEl, spEl].forEach(unskel);
  const dir = S.prevBid == null ? 0 : Math.sign(bid - S.prevBid);
  S.prevBid = bid;
  bidEl.textContent = fmt(bid);
  askEl.textContent = fmt(ask);
  spEl.textContent = fmt(spread);
  const cls = dir > 0 ? "px-up" : dir < 0 ? "px-down" : "";
  const flash = dir > 0 ? "flash-up" : dir < 0 ? "flash-down" : "";
  for (const el of [bidEl, askEl]) {
    el.classList.remove("px-up", "px-down", "flash-up", "flash-down");
    if (cls) { void el.offsetWidth; el.classList.add(cls, flash); }
  }
}

/* ═══ 分析結果 → 右欄/分頁 ═══ */
function decisionClass(action) {
  if (action === "NO_TRADE") return "d-notrade";
  if (action === "WATCH") return "d-watch";
  if (action.startsWith("PREPARE")) return "d-prepare";
  if (action === "LONG" || action === "MANAGE") return "d-long";
  if (action === "SHORT" || action === "EXIT") return "d-short";
  return "d-notrade";
}

const relTime = (ms) => {
  const m = Math.floor(ms / 60000);
  if (m < 1) return "剛剛";
  if (m < 60) return `${m} 分鐘前`;
  return `${Math.floor(m / 60)} 小時 ${m % 60} 分前`;
};

/* ═══ 快照版本與過期警示(BUGFIX R6)═══ */
function updateFreshnessUI() {
  if (!S.analysisMeta) return;
  const age = Date.now() - S.analysisMeta.ts;
  const chip = $("chip-version");
  unskel(chip);
  chip.textContent = `分析 v${S.analysisMeta.version}・${relTime(age)}`;
  const expired = S.analysisMeta.serverExpired || age > 2 * 15 * 60000; // 2 根 15M
  chip.className = "chip " + (expired ? "bad" : age > 15 * 60000 ? "warn" : "good");
  const banner = $("stale-banner");
  if (expired) {
    $("stale-age").textContent = Math.floor(age / 60000);
    banner.hidden = false;
  } else banner.hidden = true;
}
setInterval(updateFreshnessUI, 30000);

function applyAnalysis(a) {
  S.analysis = a;
  S.analysisMeta = {
    version: a.version || 0,
    ts: Date.parse(a.timestamp_utc || "") || Date.now(),
    serverExpired: !!(a.freshness && a.freshness.snapshot_expired),
  };
  updateFreshnessUI();
  // TC-11:四大區塊一律標記本次快照版本(單一來源渲染)
  for (const id of ["decision-card", "mistake-box", "tf-capsules", "panel-scenarios"]) {
    const el = $(id);
    if (el) el.dataset.v = String(a.version || 0);
  }

  const badge = $("decision-badge");
  unskel(badge);
  badge.textContent = actionZh(a.decision.action);
  badge.className = "decision-badge " + decisionClass(a.decision.action);

  const grade = $("grade-badge");
  unskel(grade);
  grade.textContent = a.decision.confidence_grade;
  grade.className = "grade-badge g-" + a.decision.confidence_grade;

  $("evidence-bar").style.width = `${a.decision.evidence_score}%`;
  $("evidence-num").textContent = a.decision.evidence_score;
  const reason = $("decision-reason");
  unskel(reason);
  reason.textContent = a.decision.reason;

  // 多週期膠囊
  const tfMap = { "1D": a.timeframes.daily, "4H": a.timeframes.h4,
                  "1H": a.timeframes.h1, "15M": a.timeframes.m15 };
  document.querySelectorAll(".capsule").forEach((cap) => {
    const v = tfMap[cap.dataset.tf];
    unskel(cap);
    cap.classList.remove("up", "down", "range");
    const st = (v && v.structure) || "";
    if (st.startsWith("UP")) cap.classList.add("up");
    else if (st.startsWith("DOWN")) cap.classList.add("down");
    else if (st) cap.classList.add("range");
    cap.title = st + (v && v.momentum ? " | " + v.momentum : "");
  });
  const msChip = $("market-state-chip");
  unskel(msChip);
  msChip.textContent = stateZh(a.market_state);
  msChip.className = "chip " + (a.market_state.includes("BULL") ? "good"
    : a.market_state.includes("BEAR") ? "bad"
    : a.market_state.includes("TRANSITION") || a.market_state.includes("PENDING") ? "warn" : "info");

  // 頂部 chips
  const mkChip = $("chip-market");
  unskel(mkChip);
  const q = a.data_quality.status;
  const qChip = $("chip-quality");
  unskel(qChip);
  qChip.textContent = "資料品質 " + q;
  qChip.className = "chip " + (q === "GOOD" ? "good" : q === "DEGRADED" ? "warn" : "bad");
  $("sys-quality").textContent = q;
  $("sys-provider").textContent = a.current_price.provider || "–";
  $("sys-lastrun").textContent = (a.timestamp_taipei || "").slice(11, 19) || "–";

  if (a.current_price.bid != null) {
    updatePricePanel(a.current_price.bid, a.current_price.ask, a.current_price.spread);
  }

  const mistake = $("mistake-box");
  if (a.most_likely_user_mistake_now) {
    mistake.textContent = a.most_likely_user_mistake_now;
    mistake.classList.add("show");
  } else mistake.classList.remove("show");

  // 事件風險(全中文;僅倒數時間保留數字格式)
  renderEventRisk(a.event_risk);
  renderBias(a.bias_analysis);

  if (a.offset_info) renderOffset(a.offset_info);
  const offVal = a.offset_info ? a.offset_info.value : 0;
  renderScenario($("scenario-long"), a.long_scenario, "做多劇本", offVal);
  renderScenario($("scenario-short"), a.short_scenario, "做空劇本", offVal);
  applyOverlays().catch(console.error);
}

const IMPACT_ZH = { HIGH: "高影響", MEDIUM: "中影響", LOW: "低影響", UNKNOWN: "未知" };
const TIME_RISK_ZH = {
  HIGH: "事件風險:高(進入鎖定窗)", MEDIUM: "事件風險:中(接近公布)",
  LOW: "事件風險:低(緩衝充足)", UNKNOWN: "事件風險:未知",
};

function renderEventRisk(er) {
  const nameEl = $("event-name"), detailEl = $("event-detail"),
        impactChip = $("event-impact-chip"), cd = $("event-countdown");
  if (er && er.minutes_remaining != null && er.next_event) {
    S.countdownTarget = Date.now() + er.minutes_remaining * 60000;
    // P2:固有影響力(靜態 chip)與時間風險(動態文字)分開顯示
    const timeRisk = er.time_risk || er.level;
    nameEl.textContent = `${er.next_event}　${TIME_RISK_ZH[timeRisk] || ""}`
      + (er.event_lockout ? "・鎖定中" : "");
    impactChip.style.display = "";
    const impact = er.event_impact || "UNKNOWN";
    impactChip.textContent = IMPACT_ZH[impact] || impact;
    impactChip.className = "chip " + (impact === "HIGH" ? "bad"
      : impact === "MEDIUM" ? "warn" : impact === "UNKNOWN" ? "warn" : "good");
  } else {
    S.countdownTarget = null;
    unskel(cd);
    cd.textContent = "—";
    impactChip.style.display = "none";
    nameEl.textContent = er && er.level === "UNKNOWN"
      ? "所有事件來源失效" : "近期無已知高影響事件";
  }
  detailEl.textContent = (er && er.reason) ||
    "事件清單來自 data/manual_events.json,請每週日更新本週高影響事件。";
}

function renderBias(b) {
  if (!b) return;
  $("bias-bull-pct").textContent = `${b.bull_pct}%`;
  $("bias-bear-pct").textContent = `${b.bear_pct}%`;
  $("bias-bull-fill").style.width = `${b.bull_pct}%`;
  $("bias-bear-fill").style.width = `${b.bear_pct}%`;
  const strip = (s) => s.replace(/^(STRUCT|LEVEL|MOMO|HTF):/, "");
  const fill = (listId, countId, items) => {
    $(countId).textContent = items.length;
    $(listId).innerHTML = items.length
      ? items.map((x) => `<li>${strip(x)}</li>`).join("")
      : "<li>目前無已成立條件</li>";
  };
  fill("bias-bull-list", "bias-bull-count", b.bull_evidence || []);
  fill("bias-bear-list", "bias-bear-count", b.bear_evidence || []);
  if (b.disclaimer) $("bias-disclaimer").textContent = b.disclaimer;
  const flags = b.chase_flags || [];
  let flagBox = document.getElementById("bias-flags");
  if (!flagBox) {
    flagBox = document.createElement("div");
    flagBox.id = "bias-flags";
    flagBox.className = "bias-flags";
    $("bias-disclaimer").before(flagBox);
  }
  flagBox.innerHTML = flags.map((f) =>
    `<span class="chip warn" title="${f.split(":").slice(1).join(":")}">${MSG.chase[f.split(":")[0]] || f.split(":")[0]}</span>`).join("");
}

function renderScenario(el, sc, title, offset) {
  if (!sc) { el.innerHTML = '<div class="empty">無資料</div>'; return; }
  const createdAge = sc.created_at ? relTime(Date.now() - Date.parse(sc.created_at)) : "";
  // BUGFIX R2:INVALID → 絕不顯示錯誤價位
  // P1 分級:FATAL(紅,程式錯誤)vs REJECT(黃,條件不足);FATAL 存在時不顯示 rr1
  if (sc.status === "INVALID") {
    const fatal = !!sc.invalid_fatal;
    const reasons = (sc.invalid_reasons || []);
    const shown = (fatal ? reasons.filter((r) => !r.startsWith("rr1")) : reasons)
      .slice(0, 3).join(";") || "偵測到自相矛盾的價位組合";
    el.innerHTML = `
      <div class="sc-head"><span class="sc-dir">${title}</span>
        <span class="sc-status INVALIDATED">${fatal ? "計算錯誤" : "條件不足"}</span>
        ${createdAge ? `<span class="sc-meta-age">${createdAge}</span>` : ""}</div>
      <div class="${fatal ? "sc-invalid-fatal" : "sc-invalid"}">
        ${fatal ? "⛔ 停損計算錯誤,已攔截(系統將自動重算)" : "⚠️ 條件不足,等待更好的機會"}<br>
        <small>${shown}</small></div>`;
    return;
  }
  const rp = sc.resolved_prices || {};
  const staleTag = sc.stale
    ? `<span class="sc-stale-tag" title="${sc.stale_reason || ""}">已過時,等待重算</span>` : "";
  const tag = offset ? `<span class="tmgm-tag">TMGM 校正 ${offset > 0 ? "+" : ""}${offset}</span>` : "";
  const lv = (id) => {
    const z = rp[id];
    return z ? `${fmt(z.price_low)} – ${fmt(z.price_high)}` : "–";
  };
  const rrPills = (sc.risk_reward || []).map((r) => `<span class="rr-pill">賺賠比 ${r} 倍</span>`).join("");
  const confirms = (sc.required_confirmations || [])
    .map((c) => `<li>${c}</li>`).join("");
  el.innerHTML = `
    <div class="sc-head"><span class="sc-dir">${title}</span>
      <span class="sc-status ${sc.status}">${SC_STATUS_ZH[sc.status] || sc.status}</span>${staleTag}${tag}
      ${createdAge ? `<span class="sc-meta-age">建立於 ${createdAge}</span>` : ""}</div>
    <div class="${sc.stale ? "sc-body-stale" : ""}">
    <div class="sc-levels">
      <div class="kv"><span>進場區</span><span class="num">${lv(sc.entry_zone_id)}</span></div>
      <div class="kv"><span>賠錢出場價</span><span class="num">${lv(sc.stop_loss_id)}</span></div>
      <div class="kv"><span>目標價</span><span class="num">${
        (sc.target_ids || []).map((t) => lv(t)).filter((x) => x !== "–").join(" / ") || "–"}</span></div>
    </div>
    ${(rrPills && !sc.stale) ? `<div class="sc-rr">${rrPills}</div>` : ""}
    </div>
    ${sc.setup ? `<div class="sc-confirm">${sc.setup}</div>` : ""}
    ${confirms ? `<div class="sc-confirm">還要等這些條件:<ul>${confirms}</ul></div>` : ""}`;
}

/* ═══ TMGM 價格校正(Price Offset)═══ */
function renderOffset(info) {
  if (!info) return;
  $("op-source").textContent = info.analysis_source;
  $("op-broker").textContent = info.trading_broker;
  // P0:動態標籤 Offset ({broker} − {active_source}),不得寫死來源名稱
  const lbl = document.querySelector("#op-manual-row label");
  if (lbl) lbl.textContent = `Offset (${info.trading_broker} − ${info.analysis_source})`;
  if (info.calibrated === false) {
    $("op-offset").textContent = "未校準";
    $("op-offset").style.color = "var(--danger)";
    $("op-mode").textContent = "暫停出訊";
  } else {
    const v = info.value || 0;
    $("op-offset").textContent = `${v > 0 ? "+" : ""}${v.toFixed(2)}`;
    $("op-offset").style.color = v > 0 ? "var(--bull)" : v < 0 ? "var(--bear)" : "var(--text)";
    $("op-mode").textContent = info.mode;
  }
}

function setupOffsetEditor() {
  const editor = $("op-editor");
  const openBtn = $("op-edit"), saveBtn = $("op-save"), cancelBtn = $("op-cancel");
  const input = $("op-input"), hint = $("op-hint");
  const manualRow = $("op-manual-row");

  const syncModeUI = () => {
    const mode = document.querySelector('input[name="op-mode-radio"]:checked').value;
    manualRow.style.opacity = mode === "manual" ? "1" : ".45";
    input.disabled = mode !== "manual";
    const src = $("op-source").textContent || "分析源";
    const broker = $("op-broker").textContent || "TMGM";
    hint.textContent = mode === "auto"
      ? `Auto 模式:未來接上 ${broker} 即時價後,自動計算 Offset = ${broker} − ${src}。目前無即時源,暫存模式設定但仍套用手動值。`
      : `此 Offset 僅校正劇本進場/停損/停利價為 ${broker} 掛單價(依當前資料源 ${src} 各自校準,24 小時未更新會暫停出訊)。`;
  };

  openBtn.addEventListener("click", async () => {
    const on = editor.hasAttribute("hidden");
    if (!on) { editor.setAttribute("hidden", ""); return; }
    try {
      const info = await (await fetch("/api/offset")).json();
      input.value = info.value;
      document.querySelector(`input[name="op-mode-radio"][value="${info.mode}"]`).checked = true;
    } catch (e) { /* noop */ }
    syncModeUI();
    editor.removeAttribute("hidden");
    input.focus();
  });
  cancelBtn.addEventListener("click", () => editor.setAttribute("hidden", ""));
  document.querySelectorAll('input[name="op-mode-radio"]').forEach((r) =>
    r.addEventListener("change", syncModeUI));

  saveBtn.addEventListener("click", async () => {
    const mode = document.querySelector('input[name="op-mode-radio"]:checked').value;
    const body = { mode };
    if (mode === "manual") {
      const val = parseFloat(input.value);
      if (Number.isNaN(val)) { hint.textContent = "請輸入有效的 Offset 數值"; return; }
      body.value = val;
    }
    try {
      const info = await postJSON("/api/offset", body);
      renderOffset(info);
      editor.setAttribute("hidden", "");
      // 即時生效:重新取分析(不重跑,套用新 Offset)
      const a = await (await fetch("/api/analysis/latest")).json();
      applyAnalysis(a);
    } catch (e) { hint.textContent = "儲存失敗:" + e; }
  });
}

/* ═══ 帳戶層(老師帶單 vs 自己交易)═══ */
S.accounts = [];
const accountName = (id) => {
  const a = S.accounts.find((x) => x.id === id);
  return a ? a.name : (id == null ? "未指定帳戶" : `帳戶#${id}`);
};

async function loadAccounts() {
  try {
    S.accounts = await (await fetch("/api/accounts")).json();
    const sel = $("pf-account");
    sel.innerHTML = S.accounts.map((a) =>
      `<option value="${a.id}"${a.strategy_source === "SELF" ? " selected" : ""}>${a.name}</option>`).join("");
  } catch (e) { console.warn("accounts load failed", e); }
}

async function loadComparison() {
  const body = $("compare-body");
  try {
    const data = await (await fetch("/api/accounts/comparison")).json();
    const accs = data.accounts || [];
    if (!accs.length) {
      body.innerHTML = '<div class="empty">尚無帳戶。</div>';
      return;
    }
    const f = (v, suffix = "") => (v == null ? "–" : `${v}${suffix}`);
    const pnlCell = (v) => v == null ? "–"
      : `<span class="${v >= 0 ? "cmp-pos" : "cmp-neg"}">${v >= 0 ? "+" : ""}${v}</span>`;
    const rows = [
      ["已平倉筆數", (s) => f(s.total_trades)],
      ["勝 / 敗", (s) => `${s.wins} / ${s.losses}`],
      ["勝率", (s) => f(s.win_rate, "%")],
      ["Expectancy(平均 R)", (s) => pnlCell(s.avg_r)],
      ["總 R", (s) => pnlCell(s.total_r)],
      ["獲利因子", (s) => f(s.profit_factor)],
      ["最大回撤(R)", (s) => f(s.max_drawdown_r)],
      ["總損益(USD)", (s) => pnlCell(s.total_pnl_usd)],
      ["行為標籤數(紀律)", (s) => f(s.behavior_flags)],
    ];
    body.innerHTML = `
      <table class="hist-table cmp-table"><thead><tr>
        <th>指標</th>${accs.map((a) =>
          `<th>${a.name}<div class="cmp-src">${a.strategy_source}</div></th>`).join("")}
      </tr></thead><tbody>
        ${rows.map(([label, fn]) => `<tr><td>${label}</td>${
          accs.map((a) => `<td class="num">${fn(a.stats)}</td>`).join("")}</tr>`).join("")}
      </tbody></table>
      <div class="bias-disclaimer" style="margin-top:10px">${data.note || ""}</div>`;
  } catch (e) {
    body.innerHTML = '<div class="empty">對照統計載入失敗。</div>';
  }
}

/* ═══ 老師帶單(僅供參考,不影響決策)═══ */
async function loadMentor() {
  const body = $("mentor-body");
  try {
    const data = await (await fetch("/api/mentor/signals")).json();
    if (!data.has_signals) {
      body.innerHTML = '<div class="empty">目前沒有老師帶單。新增後這裡會顯示老師方向與系統方向的比對。</div>';
      return;
    }
    const alignChip = (a, text) => {
      const cls = a === "ALIGNED" ? "good" : a === "OPPOSITE" ? "bad" : "warn";
      return `<span class="chip ${cls}">${text}</span>`;
    };
    body.innerHTML = data.signals.map((s) => `
      <div class="mentor-card">
        <div class="mentor-head">
          <span class="pos-side">${s.direction === "LONG" ? "老師做多" : "老師做空"}</span>
          ${alignChip(s.alignment, s.alignment_text)}
          <button class="btn btn-sm" onclick="dismissMentor(${s.id})">移除</button>
        </div>
        <div class="kv"><span>老師進場價</span><span class="num">${fmt(s.entry_price)}</span></div>
        ${s.stop_loss != null ? `<div class="kv"><span>老師停損(賠錢出場)</span><span class="num">${fmt(s.stop_loss)}</span></div>` : ""}
        ${(s.targets || []).length ? `<div class="kv"><span>老師停利(目標價)</span><span class="num">${s.targets.map((t) => fmt(t)).join(" / ")}</span></div>` : ""}
        <div class="kv"><span>系統目前方向</span><span>${
          s.system_direction === "LONG" ? "做多" : s.system_direction === "SHORT" ? "做空" : "無明確方向"}</span></div>
        <div class="kv"><span>與現價差</span><span class="num">${s.entry_vs_current_text || "–"}</span></div>
        ${s.note ? `<div class="mentor-memo">老師備註:${s.note}</div>` : ""}
      </div>`).join("") +
      `<div class="bias-disclaimer">${data.note}</div>`;
  } catch (e) {
    body.innerHTML = '<div class="empty">老師帶單載入失敗。</div>';
  }
}

async function loadMentorHistory() {
  const body = $("mentor-history");
  try {
    const h = await (await fetch("/api/mentor/history")).json();
    if (!h.trades.length) {
      body.innerHTML = '<div class="empty">尚無歷史紀錄。</div>';
      return;
    }
    const s = h.summary;
    const pnlCls = (v) => (v >= 0 ? "cmp-pos" : "cmp-neg");
    const gapNote = (h.known_gaps || []).map((g) =>
      `<div class="mentor-gap">⚠ 已知資料缺口:${g} —— 這段期間「沒有紀錄」,不代表老師空手</div>`).join("");
    body.innerHTML = `
      <div class="mentor-summary">
        <span class="chip info">共 ${s.count} 筆</span>
        <span class="chip good">勝 ${s.wins}</span>
        <span class="chip bad">負 ${s.losses}</span>
        <span class="chip">淨損益 <b class="num ${pnlCls(s.net_pl_usd)}">${s.net_pl_usd >= 0 ? "+" : ""}${s.net_pl_usd}</b></span>
        <span class="chip">扣費後 <b class="num ${pnlCls(s.net_after_fees_usd)}">${s.net_after_fees_usd >= 0 ? "+" : ""}${s.net_after_fees_usd}</b></span>
        <span class="chip">獲利因子 <b class="num">${s.profit_factor ?? "–"}</b></span>
      </div>
      ${gapNote}
      <div style="overflow-x:auto"><table class="hist-table"><thead><tr>
        <th>方向</th><th>進場 → 出場</th><th>點數</th><th>手數</th><th>損益</th>
        <th>賠錢出場價</th><th>平倉時間</th></tr></thead><tbody>
        ${h.trades.map((t) => `<tr>
          <td class="${t.direction === "LONG" ? "cmp-pos" : "cmp-neg"}">${t.direction === "LONG" ? "做多" : "做空"}</td>
          <td class="num">${fmt(t.entry_price)} → ${fmt(t.close_price)}</td>
          <td class="num">${fmt(t.points)}</td>
          <td class="num">${fmt(t.lots)}</td>
          <td class="num ${pnlCls(t.pl_usd)}">${t.pl_usd >= 0 ? "+" : ""}${fmt(t.pl_usd)}</td>
          <td class="num mentor-nodata" title="歷史匯入,無停損資料">${t.stop_loss != null ? fmt(t.stop_loss) : "—"}</td>
          <td class="num">${(t.close_time || "").slice(0, 16).replace("T", " ")}</td>
        </tr>`).join("")}
      </tbody></table></div>
      <div class="bias-disclaimer">${h.note}</div>`;
  } catch (e) {
    body.innerHTML = '<div class="empty">歷史紀錄載入失敗。</div>';
  }
}

async function dismissMentor(id) {
  try {
    await postJSON(`/api/mentor/signals/${id}/deactivate`, {});
    loadMentor();
  } catch (e) { alert("移除失敗:" + e); }
}
window.dismissMentor = dismissMentor;

/* ═══ 手動持倉管理 ═══ */
async function loadPositions() {
  const list = $("position-list");
  try {
    const rows = await (await fetch("/api/positions")).json();
    if (!rows.length) {
      list.innerHTML = '<div class="empty">尚無持倉紀錄。用上方表單輸入你在券商實際建立的部位,系統會追蹤 R 倍數並依規則給出管理建議。</div>';
      return;
    }
    list.innerHTML = rows.map(posCard).join("");
  } catch (e) {
    list.innerHTML = '<div class="empty">持倉載入失敗。</div>';
  }
}

function posCard(p) {
  const r = p.r_multiple;
  const rPct = r == null ? 0 : Math.max(0, Math.min(100, (r / 3) * 100));
  const pnl = p.unrealized_pnl;
  const hist = [
    ...(p.stop_modification_history || []).map((h) =>
      `<li>${h.time.slice(5, 16).replace("T", " ")} 停損 ${fmt(h.old_stop)} → ${fmt(h.new_stop)}${h.widening ? "(⚠ 擴大)" : ""}</li>`),
    ...(p.partial_exit_history || []).map((h) =>
      `<li>${h.time.slice(5, 16).replace("T", " ")} 平倉 ${h.percent}% @ ${fmt(h.price)}(R=${h.r_at_exit ?? "–"})</li>`),
  ].join("");
  return `
  <div class="pos-card ${p.side.toLowerCase()}" data-id="${p.id}">
    <div class="pos-head">
      <span class="pos-side">${p.side === "LONG" ? "做多" : "做空"}</span>
      <span class="chip info">${accountName(p.account_id)}</span>
      <span class="num">${fmt(p.lot_size)} 手・剩餘 ${p.remaining_percent}%</span>
      ${p.is_open ? "" : '<span class="pos-closed-tag">已平倉</span>'}
      ${pnl != null ? `<span class="pos-pnl ${pnl >= 0 ? "pos" : "neg"}">${pnl >= 0 ? "+" : ""}${fmt(pnl)} USD</span>` : ""}
    </div>
    <div class="pos-meta">
      <span>進場 <span class="num">${fmt(p.entry_price)}</span></span>
      <span>賠錢出場價 <span class="num">${fmt(p.stop_loss)}</span></span>
      <span>目標價 <span class="num">${(p.planned_targets || []).map((t) => fmt(t)).join(" / ") || "–"}</span></span>
      <span>開倉 <span class="num">${p.open_time.slice(5, 16).replace("T", " ")}</span></span>
    </div>
    ${p.is_open ? `
    <div class="pos-row"><div class="lbl"><span>賺賠比進度(回本 → 3 倍)</span>
      <span class="num">${r == null ? "沒設出場價" : fmt(r, 2) + " 倍"}</span></div>
      <div class="progress"><div class="fill" style="width:${rPct}%"></div></div></div>
    ${p.recommended_action ? `<div class="pos-advice">${p.recommended_action}</div>` : ""}
    <div class="pos-actions">
      <button class="btn btn-sm btn-warn" onclick="actStop(${p.id})">改出場價</button>
      <button class="btn btn-sm" onclick="actPartial(${p.id})">分批平倉</button>
      <button class="btn btn-sm btn-danger" onclick="actClose(${p.id})">全部平倉</button>
    </div>` : ""}
    ${hist ? `<details class="pos-hist"><summary>操作歷史</summary><ul>${hist}</ul></details>` : ""}
  </div>`;
}

async function postJSON(url, body) {
  const r = await fetch(url, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || r.status);
  return data;
}

async function actStop(id) {
  const v = prompt("新的賠錢出場價(只能往獲利方向移;往賠更多的方向移會被記一筆凹單):");
  if (!v) return;
  try {
    const out = await postJSON(`/api/positions/${id}/stop`, { stop_loss: parseFloat(v) });
    if (out.behavior_flag) alert("⚠ 交易教練:你把出場價往賠更多的方向挪了(凹單),要小心。");
  } catch (e) { alert("失敗:" + e.message); }
  loadPositions(); loadCoach();
}

async function actPartial(id) {
  const pct = prompt("平倉比例 %(例:30):");
  if (!pct) return;
  const px = prompt("平倉價格(留空 = 使用當前市價):");
  try {
    const out = await postJSON(`/api/positions/${id}/partial_exit`,
      { percent: parseFloat(pct), price: px ? parseFloat(px) : null });
    if (out.behavior_flag) alert(`⚠ 交易教練:偵測到 ${out.behavior_flag}`);
  } catch (e) { alert("失敗:" + e.message); }
  loadPositions(); loadCoach();
}

async function actClose(id) {
  if (!confirm("確定全部平倉?")) return;
  const px = prompt("平倉價格(留空 = 使用當前市價):");
  try {
    const out = await postJSON(`/api/positions/${id}/close`,
      { price: px ? parseFloat(px) : null });
    if (out.behavior_flag) alert(`⚠ 交易教練:偵測到 ${out.behavior_flag}`);
  } catch (e) { alert("失敗:" + e.message); }
  loadPositions(); loadCoach();
}

async function loadCoach() {
  const body = $("coach-body");
  try {
    const flags = await (await fetch("/api/behavior/flags")).json();
    if (!flags.length) {
      body.innerHTML = '<div class="empty">尚無行為標籤。當持倉操作觸發紀律問題(擴大停損、過早平倉…)時會顯示於此。</div>';
      return;
    }
    body.innerHTML = flags.map((f) => `
      <div class="coach-flag">
        <span class="cf-name">${f.flag}</span>
        <span class="cf-time">${f.detected_at.slice(0, 16).replace("T", " ")} UTC</span>
        <p>${f.corrective_action}</p>
      </div>`).join("");
  } catch (e) {
    body.innerHTML = '<div class="empty">行為紀錄載入失敗。</div>';
  }
}

async function loadHistory() {
  const body = $("history-body");
  try {
    const rows = await (await fetch("/api/analysis/history?limit=30")).json();
    if (!rows.length) {
      body.innerHTML = '<div class="empty">尚無歷史分析紀錄。</div>';
      return;
    }
    body.innerHTML = `<table class="hist-table"><thead><tr>
      <th>時間 (UTC)</th><th>市場狀態</th><th>決策</th><th>信心</th><th>證據</th><th>品質</th>
      </tr></thead><tbody>${rows.map((r) => `<tr>
        <td class="num">${r.run_time.slice(5, 16).replace("T", " ")}</td>
        <td>${stateZh(r.market_state)}</td>
        <td><span class="act-pill ${decisionClass(r.action)}">${r.action}</span></td>
        <td><span class="grade-badge g-${r.grade}" style="width:26px;height:26px;font-size:.85rem">${r.grade}</span></td>
        <td class="num">${r.evidence_score}</td>
        <td>${r.quality}</td></tr>`).join("")}</tbody></table>`;
  } catch (e) {
    body.innerHTML = '<div class="empty">歷史紀錄載入失敗。</div>';
  }
}

/* ═══ 倒數計時 ═══ */
setInterval(() => {
  if (!S.countdownTarget) return;
  const cd = $("event-countdown");
  unskel(cd);
  let ms = S.countdownTarget - Date.now();
  if (ms < 0) ms = 0;
  const h = Math.floor(ms / 3600000), m = Math.floor(ms / 60000) % 60,
        s = Math.floor(ms / 1000) % 60;
  cd.textContent = `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  cd.classList.toggle("urgent", ms > 0 && ms < 30 * 60000);
}, 1000);

/* ═══ WebSocket ═══ */
let wsRetry = 0;
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { wsRetry = 0; $("conn-dot").className = "dot ok"; };
  ws.onclose = () => {
    $("conn-dot").className = "dot bad";
    setTimeout(connectWS, Math.min(30000, 1000 * 2 ** wsRetry++));
  };
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "tick") onTick(msg);
      else if (msg.type === "analysis") applyAnalysis(msg.data);
      else if (msg.type === "candle_closed") loadCandles(S.tf, true).catch(console.error);
    } catch (err) { console.warn("ws message error", err); }
  };
}

/* ═══ 啟動 ═══ */
async function boot() {
  initChart();
  document.querySelectorAll(".tf-btn").forEach((b) =>
    b.addEventListener("click", () => switchTF(b.dataset.tf)));
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      $("panel-" + t.dataset.tab).classList.add("active");
      if (t.dataset.tab === "history") loadHistory();
      if (t.dataset.tab === "position") loadPositions();
      if (t.dataset.tab === "mentor") { loadMentor(); loadMentorHistory(); }
      if (t.dataset.tab === "coach") loadCoach();
      if (t.dataset.tab === "compare") loadComparison();
    }));

  $("mentor-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const tgt = $("mf-target").value ? [parseFloat($("mf-target").value)] : [];
      await postJSON("/api/mentor/signals", {
        direction: $("mf-dir").value,
        entry_price: parseFloat($("mf-entry").value),
        stop_loss: $("mf-stop").value ? parseFloat($("mf-stop").value) : null,
        targets: tgt,
        note: $("mf-note").value || null,
      });
      e.target.reset();
      loadMentor();
    } catch (err) { alert("新增失敗:" + err); }
  });

  $("pos-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const targets = $("pf-targets").value.split(",")
      .map((s) => parseFloat(s.trim())).filter((x) => !isNaN(x));
    try {
      await postJSON("/api/positions", {
        side: $("pf-side").value,
        entry_price: parseFloat($("pf-entry").value),
        stop_loss: $("pf-stop").value ? parseFloat($("pf-stop").value) : null,
        lot_size: parseFloat($("pf-lot").value),
        planned_targets: targets,
        account_id: parseInt($("pf-account").value, 10) || null,
      });
      e.target.reset();
      loadPositions();
    } catch (err) { alert("新增失敗:" + err.message); }
  });

  connectWS();
  loadAccounts();
  setupOffsetEditor();
  try { renderOffset(await (await fetch("/api/offset")).json()); } catch (e) { /* noop */ }

  try {
    const h = await (await fetch("/health")).json();
    const mk = $("chip-market");
    unskel(mk);
    mk.textContent = h.market_open ? "開盤中" : "休市";
    mk.className = "chip " + (h.market_open ? "good" : "warn");
  } catch (e) { /* noop */ }

  try {
    S.events = await (await fetch("/api/events/upcoming")).json();
  } catch (e) { S.events = []; }

  // 先取最新分析(首次呼叫會觸發分析並把 K 棒寫入 DB),再載入圖表
  try {
    const a = await (await fetch("/api/analysis/latest")).json();
    applyAnalysis(a);
  } catch (e) { console.error("analysis load failed", e); }

  try { await loadCandles(S.tf, false); } catch (e) { console.error(e); }

  // 保險輪詢:WS 斷線期間每 5 分鐘補一次分析
  setInterval(async () => {
    try { applyAnalysis(await (await fetch("/api/analysis/latest")).json()); }
    catch (e) { /* noop */ }
  }, 300000);
}

boot();
