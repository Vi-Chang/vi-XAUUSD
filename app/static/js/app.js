/* XAUUSD 交易分析終端 — Dashboard 前端
 * 圖表:TradingView lightweight-charts v4(自托管,Apache 2.0)
 * 資料一律來自本系統 API/DB,確保與分析引擎同源。
 */
"use strict";

// 共用 HTML escaping(防 XSS,單一來源):
// - h`` 預設跳脫每個 ${},回傳 SafeHtml;巢狀 h`` 片段原樣放行(不需 bypass)。
// - joinSafe(arr) 合成片段陣列;trusted() 僅供程式碼內固定 HTML 字面值。
const { esc, h, joinSafe, trusted } = window.XSS;

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
  showAllMarkers: false,
  authed: false, privWs: null,   // 管理登入狀態 + 私人 WebSocket
};

// 私人面板(登入後才載入;登出/過期時清空 DOM)
const PRIVATE_PANELS = ["position-list", "coach-body"];

const $ = (id) => document.getElementById(id);
const unskel = (el) => el && el.classList.remove("skel");
const fmt = (v, d = 2) => (v == null ? "–" : Number(v).toFixed(d));
const fmtTs = (v) => v ? String(v).slice(0, 16).replace("T", " ") : "–";

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
        axisLabelVisible: true, title: `${tag}${title}區中位`,
      }));
    };
    const mkExact = (price, color, title) => {
      if (price == null) return;
      S.priceLines.push(S.candles.createPriceLine({
        price, color, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: true, title: `${tag}${title}`,
      }));
    };
    mk(sc.entry_zone_id, C.info, "進場");
    if (sc.stop_loss_price != null) mkExact(sc.stop_loss_price, C.danger, "停損價");
    else mk(sc.stop_loss_id, C.danger, "停損參考");
    (sc.target_ids || []).forEach((tid, i) => mk(tid, C.bull, `目標${i + 1}`));
  }

  // 結構事件標記(BOS/CHoCH/假突破)
  try {
    const evs = await (await fetch(`/api/structure/events?timeframe=${S.tf}&limit=40`)).json();
    const barSet = new Set(S.barTimes);
    const markers = [];
    const visibleEvents = S.showAllMarkers ? evs : evs.slice(-6);
    for (const ev of visibleEvents) {
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
  if ($("quick-live-price")) $("quick-live-price").textContent = fmt(t.mid);
  if ($("quick-live-time")) $("quick-live-time").textContent = new Date(t.time * 1000).toLocaleTimeString("zh-TW", { hour12: false });
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
  // 尚無可公開分析(例如隱私邊界版本升級後,等待下一次新版分析)→ 友善等待訊息。
  // 不顯示內部版本或安全細節。
  if (!a || a.available === false) {
    const badge = $("decision-badge");
    if (badge) { unskel(badge); badge.textContent = "分析更新中"; badge.className = "decision-badge"; }
    const reason = $("decision-reason");
    if (reason) { unskel(reason); reason.textContent = "分析格式已更新,等待下一次排程產生最新市場分析。"; }
    return;
  }
  S.analysis = a;
  S.analysisMeta = {
    version: a.version || 0,
    ts: Date.parse(a.timestamp_utc || "") || Date.now(),
    serverExpired: !!(a.freshness && a.freshness.snapshot_expired),
    candleRefreshPending: !!(a.freshness && a.freshness.candle_refresh_pending),
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
  grade.textContent = gradeZh(a.decision.confidence_grade);
  grade.className = "grade-badge g-" + a.decision.confidence_grade;
  const calibrationNote = $("calibration-note");
  if (calibrationNote) {
    calibrationNote.textContent = a.calibration_message || "";
    calibrationNote.hidden = !a.calibration_message;
  }

  const signalScore = a.decision.signal_score;
  $("evidence-bar").style.width = `${signalScore == null ? 0 : signalScore}%`;
  $("evidence-num").textContent = signalScore == null ? "未取得" : signalScore;
  const reason = $("decision-reason");
  unskel(reason);
  reason.textContent = a.decision.reason;
  renderQuickAction(a);
  renderEntryPlan(a.entry_engine);
  renderDirectionalAlert(a.directional_alert);
  renderMarketMonitors(a);
  renderFinalDecision(a.final_decision_state);

  // 資料不足 → 醒目「暫不交易」橫幅(資料過期/異常/休市/證據不足時系統一律 NO_TRADE)
  const ntBanner = $("no-trade-banner");
  if (ntBanner) {
    if (a.decision.action === "NO_TRADE") {
      const nr = $("no-trade-reason");
      if (nr) nr.textContent = a.decision.reason || "資料品質未通過,暫停輸出可執行訊號。";
      const lu = (a.current_price && a.current_price.last_update) || a.snapshot_ts || "";
      const nu = $("no-trade-updated");
      if (nu) nu.textContent = lu ? ("資料更新時間 " + String(lu).slice(11, 19) + " UTC") : "";
      ntBanner.hidden = false;
      badge.classList.add("blocked");
    } else {
      ntBanner.hidden = true;
    }
  }

  const n = a.normalized_analysis;
  // 多週期膠囊：顯示統一狀態，不再只看結構 UP/DOWN。
  const tfMap = { "1D": a.timeframes.daily, "4H": a.timeframes.h4,
                  "1H": a.timeframes.h1, "15M": a.timeframes.m15 };
  const normalizedTf = Object.fromEntries(((n && n.timeframeAssessments) || []).map((x) => [x.timeframe, x]));
  document.querySelectorAll(".capsule").forEach((cap) => {
    const v = tfMap[cap.dataset.tf];
    const nv = normalizedTf[cap.dataset.tf];
    unskel(cap);
    cap.classList.remove("up", "down", "range");
    const st = nv ? nv.trend.toUpperCase() : ((v && v.structure) || "");
    if (st.startsWith("UP")) cap.classList.add("up");
    else if (st.startsWith("DOWN")) cap.classList.add("down");
    else if (st) cap.classList.add("range");
    cap.textContent = nv ? `${cap.dataset.tf}・${nv.label}` : cap.dataset.tf;
    cap.title = nv ? `${nv.label}｜${nv.momentum}` : st + (v && v.momentum ? " | " + v.momentum : "");
  });
  const msChip = $("market-state-chip");
  unskel(msChip);
  const stateCode = n ? n.marketStateCode : a.market_state;
  msChip.textContent = n ? n.marketStateLabel : stateZh(stateCode);
  msChip.className = "chip " + (stateCode.includes("BULL") ? "good"
    : stateCode.includes("BEAR") ? "bad"
    : stateCode.includes("FAILED") || stateCode.includes("TRANSITION") || stateCode.includes("PENDING") ? "warn" : "info");

  // 頂部 chips
  const mkChip = $("chip-market");
  unskel(mkChip);
  const q = a.data_quality.status;
  const qChip = $("chip-quality");
  unskel(qChip);
  qChip.textContent = "資料品質 " + qualityZh(q);
  qChip.className = "chip " + (q === "GOOD" ? "good" : q === "DEGRADED" ? "warn" : "bad");
  renderNormalized(n);
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
  renderEventOutcome(a.event_risk);
  renderBias(a.bias_analysis, n);

  if (a.offset_info) renderOffset(a.offset_info);
  const offVal = a.offset_info ? a.offset_info.value : 0;
  renderScenario($("scenario-long"), a.long_scenario, "做多劇本", offVal);
  renderScenario($("scenario-short"), a.short_scenario, "做空劇本", offVal);
  renderAiStrategy(a.ai_strategy);
  applyOverlays().catch(console.error);
}

function renderDirectionalAlert(alert) {
  const card = $("bearish-monitor-card");
  if (!card) return;
  const active = alert && !["NEUTRAL", "SHORT_INVALIDATED"].includes(alert.status);
  card.hidden = !active;
  if (!active) return;
  const events = {
    INTRABAR_BREACH: "盤中測試", BREAKDOWN_CONFIRMED: "跌破已確認",
    RETEST_REJECTED: "反彈回測失敗", BEARISH_CONTINUATION: "空方延續",
    SHORT_ENTRY_READY: "空單條件完成", FALSE_BREAKOUT: "假跌破",
  };
  const statuses = {
    SHORT_WATCH: "等待收盤", BEARISH_WATCH: "空方觀察中",
    SHORT_ENTRY_READY: "可依計畫評估",
  };
  $("bearish-monitor-event").textContent = events[alert.event_type] || "空方持續監控";
  $("bearish-monitor-status").textContent = statuses[alert.status] || alert.status;
  $("bearish-monitor-level").textContent = alert.level == null ? "–" : Number(alert.level).toFixed(2);
  $("bearish-monitor-time").textContent = fmtTs(alert.candle_close_time);
  $("bearish-monitor-message").textContent = alert.message || "持續等待下一個 15M 結構事件。";
}

function renderFinalDecision(finalState) {
  if (!finalState) return;
  const event = finalState.latest_event || {};
  if (event.eventId) finalState = {
    ...finalState,
    state: event.currentState,
    source_price: event.currentPrice,
    reason: event.transitionReason,
    flat_action: event.flatAction,
    long_manage: event.longManage,
    short_manage: event.shortManage,
    confirmation: event.confirmation,
    quote_time: event.calculatedAt,
    latest_closed_price: event.latestClosedCandlePrice,
    last_closed_candle_time: event.candleCloseTime,
    presentation: event.presentation || finalState.presentation,
  };
  const labels = {
    WAIT: "等待", LONG_BIAS: "行情偏多，尚未到進場區", SHORT_BIAS: "行情偏空，尚未到進場區",
    LONG_WATCH: "偏多等待確認，尚不可進場", LONG_READY: "多單進場條件成立",
    SHORT_WATCH: "偏空等待確認，尚不可進場", SHORT_READY: "空單進場條件成立",
    LONG_MANAGE: "多方獲利管理", SHORT_MANAGE: "空方獲利管理",
    MISSED_ENTRY: "已錯過進場", INVALIDATED: "舊劇本已失效", DATA_STALE: "資料過期",
  };
  const presentation = finalState.presentation || event.presentation || {};
  $("quick-final-state").textContent = labels[finalState.state] || "等待";
  $("quick-flat-action").textContent = finalState.flat_action || "等待下一個明確條件";
  $("quick-position-action").textContent = finalState.direction === "SHORT"
    ? finalState.short_manage : finalState.long_manage;
  $("quick-trigger").textContent = finalState.confirmation || "等待下一根 15 分鐘 K 線";
  $("quick-freshness").textContent = finalState.state === "DATA_STALE" ? "資料過期，禁止進場" : "資料正常";
  $("quick-live-price").textContent = finalState.source_price == null ? "–" : fmt(finalState.source_price);
  $("quick-live-time").textContent = fmtTs(finalState.quote_time);
  $("quick-closed-price").textContent = finalState.latest_closed_price == null ? "–" : fmt(finalState.latest_closed_price);
  $("quick-closed-time").textContent = fmtTs(finalState.last_closed_candle_time);
  $("quick-action-title").textContent = presentation.title || finalState.action || labels[finalState.state] || "等待";
  $("quick-action-why").textContent = finalState.reason || "等待條件一致";
  $("quick-action-next").textContent = finalState.flat_action || "等待下一個明確條件";
  $("quick-action-card").dataset.action = presentation.tone || (finalState.state === "DATA_STALE" ? "danger" : "neutral");
}

async function refreshTelegramStatus() {
  try {
    const status = await (await fetch("/api/telegram/status")).json();
    $("telegram-connection").textContent = status.connected ? "已連線" : "未設定／異常";
    $("telegram-last-event").textContent = status.lastEvent || "尚無事件";
    $("telegram-last-sent").textContent = status.lastSentAt ? fmtTs(status.lastSentAt) : "尚無成功通知";
    const labels = { PENDING: "通知傳送中", RETRYING: "通知重試中", FAILED: "Telegram 發送失敗", SENT: "已送達", IDLE: "尚無通知" };
    $("telegram-delivery").textContent = labels[status.status] || status.status;
  } catch (e) {
    $("telegram-connection").textContent = "異常";
    $("telegram-delivery").textContent = "Telegram 狀態讀取失敗";
  }
}

function renderMarketMonitors(a) {
  const price = (value) => value == null ? "–" : Number(value).toFixed(2);
  renderBreakoutSetupLedger(a.breakout_setup_manager || {});
  renderTrendContinuation(a.trend_continuation_engine || {});
  const breakout = a.breakout_alert || {};
  const breakoutCard = $("breakout-alert-card");
  if (breakoutCard) {
    breakoutCard.hidden = !breakout.status || breakout.status === "NEUTRAL";
    if (!breakoutCard.hidden) {
      const statusZh = {
        PENDING_BREAKOUT: "突破中，等待收盤", BREAKOUT_CONFIRMED: "突破已成立",
        BULLISH_CONTINUATION: "多頭延續已確認", BREAKOUT_RETEST: "突破區回踩",
        BREAKOUT_FAILED: "突破失敗",
      };
      const actionZhMap = {
        wait_confirmation: "等待確認", wait_pullback: "等待回踩",
        avoid_chasing: "禁止追價", breakout_failed: "突破失敗",
      };
      $("breakout-alert-status").textContent = statusZh[breakout.status] || breakout.status;
      $("breakout-alert-action").textContent = actionZhMap[breakout.action] || breakout.action || "–";
      $("breakout-alert-level").textContent = price(breakout.zone_high);
      $("breakout-alert-closes").textContent = `${breakout.consecutive_closes || 0} 根已收盤 K`;
      $("breakout-alert-trend").textContent = breakout.trend === "bullish" ? "多頭" : breakout.trend === "bearish" ? "空頭" : "震盪";
    }
  }

  const advisor = a.hypothetical_exit_advisor || {};
  const exitCard = $("hypothetical-exit-card");
  const plans = advisor.plans || {};
  if (exitCard) {
    exitCard.hidden = !plans.LONG && !plans.SHORT;
    if (!exitCard.hidden) {
      const longPlan = plans.LONG || {};
      const shortPlan = plans.SHORT || {};
      $("hypothetical-long-exit").textContent = longPlan.partial_exit && longPlan.full_exit
        ? `若持有多單：進入 ${price(longPlan.partial_exit.low)}–${price(longPlan.partial_exit.high)} 建議先平倉30%；${price(longPlan.full_exit.low)}–${price(longPlan.full_exit.high)} 評估剩餘部位` : "止盈計畫尚未建立：缺少有效壓力區";
      $("hypothetical-long-defense").textContent = price(longPlan.defense_price);
      $("hypothetical-short-exit").textContent = shortPlan.partial_exit && shortPlan.full_exit
        ? `若持有空單：進入 ${price(shortPlan.partial_exit.low)}–${price(shortPlan.partial_exit.high)} 建議先平倉30%；${price(shortPlan.full_exit.low)}–${price(shortPlan.full_exit.high)} 評估剩餘部位` : "止盈計畫尚未建立：缺少有效支撐區";
      $("hypothetical-short-defense").textContent = price(shortPlan.defense_price);
    }
  }

  const manager = a.trade_plan_manager || {};
  const activeTradePlan = (manager.activePlans || [])[0];
  const tracker = a.virtual_profit_tracker || {};
  const profitCard = $("virtual-profit-card");
  if (profitCard) {
    profitCard.hidden = !activeTradePlan && !tracker.setup_id && !(manager.errors || []).length;
    if (!profitCard.hidden) {
      if (activeTradePlan) {
        const side = activeTradePlan.direction === "LONG" ? "多單" : "空單";
        const completed = activeTradePlan.completedEvents || [];
        const next = !completed.includes("TAKE_PROFIT_1") ? "TP1" : !completed.includes("TAKE_PROFIT_2") ? "TP2" : "TP3／移動止盈";
        $("virtual-profit-summary").textContent = `若你持有${side}｜目前 ${Number(activeTradePlan.currentR || 0).toFixed(2)}R｜下一目標 ${next}`;
        $("virtual-profit-targets").textContent = `TP1 ${price(activeTradePlan.tp1Price)} 平30%／TP2 ${price(activeTradePlan.tp2Price)} 再平30%／TP3 ${price(activeTradePlan.tp3Price)} 剩餘40%移動止盈`;
        $("virtual-profit-protection").textContent = `${price(activeTradePlan.trailingStopPrice)}；提前退出：${activeTradePlan.earlyExitCondition}`;
      } else if ((manager.errors || []).length) {
        $("virtual-profit-summary").textContent = `止盈計畫尚未建立：${manager.errors.join("；")}`;
        $("virtual-profit-targets").textContent = "–";
        $("virtual-profit-protection").textContent = "–";
      } else {
        const reached = (tracker.reached_levels || []).join("、") || "尚未到達 TP1";
        $("virtual-profit-summary").textContent = `若你有在 ${price(tracker.entry_price)} 附近進場：${reached}`;
        $("virtual-profit-targets").textContent = `${price(tracker.tp1)}／${price(tracker.tp2)}／${price(tracker.tp3)}`;
        $("virtual-profit-protection").textContent = price(tracker.protection_price);
      }
    }
  }
}

function renderTrendContinuation(engine) {
  const card = $("trend-continuation-card");
  const list = $("trend-continuation-list");
  if (!card || !list) return;
  card.hidden = !engine.enabled;
  if (card.hidden) return;
  const names = { SHALLOW_PULLBACK_LONG: "淺回踩", BREAKOUT_RETEST_LONG: "突破回踩",
    BULL_FLAG_CONTINUATION: "旗形整理突破", MOMENTUM_CONTINUATION: "動能延續" };
  const market = { TREND_CONTINUATION_LONG: "強勢多頭", TREND_CONTINUATION_SHORT: "強勢空頭",
    RANGE: "區間整理", REVERSAL: "反轉", UNDEFINED: "型態未定" }[engine.marketType] || "型態未定";
  $("trend-shadow-badge").textContent = engine.shadowMode ? "觀察模式" : "正式提示";
  $("trend-continuation-summary").textContent = `${market}｜趨勢評分 ${engine.trendScore || 0}｜15M ATR ${Number(engine.atrValue || 0).toFixed(2)}`;
  list.replaceChildren(...(engine.candidates || []).map((c) => {
    const box = document.createElement("div");
    box.className = "scenario-item";
    const ready = String(c.status || "").startsWith("ENTRY_READY_");
    const zone = c.entryZoneLow == null ? "尚未建立" : `${Number(c.entryZoneLow).toFixed(2)}–${Number(c.entryZoneHigh).toFixed(2)}`;
    const missing = (c.missingConditions || []).join("；") || "條件完整";
    box.textContent = `${ready ? "可評估進場" : "尚未成立"}｜${names[c.type] || c.type}｜進場區 ${zone}｜RR ${c.riskReward || "–"}｜${missing}`;
    return box;
  }));
}

function renderBreakoutSetupLedger(manager) {
  const card = $("breakout-setup-ledger-card");
  const list = $("breakout-setup-list");
  if (!card || !list) return;
  const setups = manager.setups || [];
  card.hidden = setups.length === 0;
  if (card.hidden) return;
  const stateZh = {
    WAIT_BREAKOUT_CONFIRMATION: "尚未確認", BREAKOUT_CONFIRMED: "突破已確認",
    WAIT_RETEST: "等待回踩", ENTRY_READY_BREAKOUT: "突破進場成立",
    ENTRY_READY_RETEST: "回踩進場成立", MISSED_ENTRY: "已錯過",
    INVALIDATED: "已失效", EXPIRED: "已到期",
  };
  const ready = setups.find((s) => String(s.status).startsWith("ENTRY_READY_"));
  $("breakout-setup-summary").textContent = ready
    ? `${ready.direction === "LONG" ? "多單" : "空單"}${ready.entryType === "RETEST" ? "回踩" : "突破"}條件已成立`
    : "舊劇本與新延續劇本分開顯示；尚未成立時不會誤示可進場。";
  list.replaceChildren(...setups.slice(-3).reverse().map((s) => {
    const box = document.createElement("div");
    box.className = "scenario-item";
    const side = s.direction === "LONG" ? "多方" : "空方";
    const confirmed = s.breakoutConfirmedAt ? "已完成" : "尚未完成";
    box.textContent = `${side}劇本 ${s.setupId}｜${Number(s.breakoutTrigger).toFixed(2)} 突破確認：${confirmed}｜目前：${stateZh[s.status] || s.status}｜回踩區 ${Number(s.retestZoneLow).toFixed(2)}–${Number(s.retestZoneHigh).toFixed(2)}｜最大追價 ${Number(s.maxChasePrice).toFixed(2)}`;
    return box;
  }));
}

function renderEntryPlan(plan) {
  const card = $("entry-plan-card");
  if (!card) return;
  if (!plan || plan.status === "NO_SETUP") {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const contradictoryTriggered = plan.status === "ENTRY_TRIGGERED" && !!plan.missing_condition;
  const displayStatus = contradictoryTriggered ? "ENTRY_READY" : plan.status;
  const status = {
    SETUP_WATCH: "機會準備中", ENTRY_READY: "已到觀察區，等待 K 線確認",
    ENTRY_TRIGGERED: "條件完成，可依計畫執行", INVALIDATED: "計畫已取消",
    EXITED: "計畫已結束",
  };
  const direction = plan.direction === "LONG" ? "做多" : plan.direction === "SHORT" ? "做空" : "–";
  $("entry-plan-status").textContent = status[displayStatus] || displayStatus;
  $("entry-plan-direction").textContent = direction;
  $("entry-plan-direction").className = "chip " + (plan.direction === "LONG" ? "good" : plan.direction === "SHORT" ? "bad" : "info");
  const qualityText = plan.entry_quality_score != null
    ? `短線進場品質 ${Number(plan.entry_quality_score).toFixed(0)}/100（不是勝率）` : "";
  $("entry-plan-missing").textContent = [
    plan.missing_condition || (displayStatus === "ENTRY_TRIGGERED" ? "進場條件已完成" : ""),
    qualityText,
  ].filter(Boolean).join("；");
  const price = (value) => value == null ? "–" : Number(value).toFixed(2);
  $("entry-plan-zone").textContent = plan.zone_low == null || plan.zone_high == null ? "–" : `${price(plan.zone_low)}–${price(plan.zone_high)}`;
  $("entry-plan-entry").textContent = price(plan.suggested_entry);
  $("entry-plan-stop").textContent = price(plan.stop_loss);
  $("entry-plan-tp1").textContent = price(plan.take_profit_1);
  $("entry-plan-tp2").textContent = price(plan.take_profit_2);
  $("entry-plan-tp3").textContent = price(plan.take_profit_3);
  $("entry-plan-rr").textContent = plan.risk_reward == null ? "–" : `1 : ${Number(plan.risk_reward).toFixed(2)}`;
  $("entry-plan-trigger").textContent = plan.trigger_condition ? `${plan.trigger_timeframe || "15M"} ${plan.trigger_condition}` : "等待已收盤反轉 K 線";
  $("entry-plan-cancel").textContent = plan.cancel_condition || "–";
  $("entry-plan-expiry").textContent = fmtTs(plan.expires_at);
  card.dataset.status = displayStatus;
  card.dataset.direction = plan.direction || "NONE";
}

function renderQuickAction(a) {
  const n = a.normalized_analysis || {};
  const action = a.decision && a.decision.action;
  const refreshing = !!(a.freshness && a.freshness.candle_refresh_pending);
  const titles = {
    NO_TRADE: "現在先不要交易", WATCH: "先觀察，暫不進場",
    PREPARE_LONG: "等確認後再考慮做多", PREPARE_SHORT: "等確認後再考慮做空",
    LONG: "可依計畫考慮做多", SHORT: "可依計畫考慮做空",
  };
  const why = refreshing ? "新一根 15 分鐘 K 棒已收盤，系統正在重新判斷。"
    : n.marketDataStatus !== "GOOD" ? "行情資料需要再確認。"
    : n.eventDataStatus === "FAILED" ? "事件資料不完整，這次只依技術面判斷。"
    : n.shortTermMomentum === "pullback" ? "大方向未必改變，但短線正在回落。"
    : n.entryReadiness === "avoid_chasing" ? "方向可能正確，但現在追價的風險較高。"
    : (a.decision && a.decision.reason) || "正在整理最新市場訊號。";
  const next = refreshing ? "更新完成前先不要進場"
    : n.entryReadiness === "no_trade" || n.entryReadiness === "wait_confirmation"
    ? "等下一根 15 分鐘 K 棒收盤確認"
    : n.entryReadiness === "avoid_chasing" ? "等價格回到較佳位置，或收盤確認突破"
    : action === "LONG" || action === "SHORT" ? "先設定停損，再依計畫執行"
    : "等條件完成後再判斷";
  $("quick-action-title").textContent = refreshing ? "判斷更新中" : (titles[action] || "先等待確認");
  $("quick-action-why").textContent = why;
  $("quick-action-next").textContent = next;
  $("quick-action-card").dataset.action = action || "WATCH";
}

function collapseSideCard(anchorId, label) {
  const anchor = $(anchorId);
  const card = anchor && anchor.closest(".card");
  if (!card || card.dataset.collapsed) return;
  card.dataset.collapsed = "true";
  const title = card.querySelector(".card-title");
  const details = document.createElement("details");
  details.className = "side-details";
  const summary = document.createElement("summary");
  summary.textContent = label;
  details.append(summary);
  for (const child of [...card.children]) {
    if (child !== title) details.append(child);
  }
  if (title) title.replaceWith(details);
  else card.append(details);
}

function renderNormalized(n) {
  if (!n) return;
  renderRiskPriority(n);
  const trend = { bullish: "偏多", bearish: "偏空", neutral: "中性" };
  const breakout = { confirmed: "已收盤確認", testing: "盤中測試", failed: "突破失敗", none: "無突破" };
  const timing = { favorable: "條件有利", chase: "不宜追價", wait: "等待確認", invalid: "資料無效" };
  $("trend-bias").textContent = trend[n.trendBias] || n.trendBias;
  $("tactical-bias").textContent = trend[n.tacticalBias] || n.tacticalBias;
  const setupLabels = { OBSERVE: "觀望", LONG_WATCH: "留意多方", SHORT_WATCH: "留意空方",
    LONG_READY: "多方條件完成", SHORT_READY: "空方條件完成", NO_CHASE: "方向成立但不追價" };
  $("setup-state").textContent = setupLabels[n.setupState] || n.setupState || "觀望";
  $("breakout-state").textContent = breakout[n.breakoutState] || n.breakoutState;
  $("entry-timing").textContent = timing[n.entryTiming] || n.entryTiming;
  $("bearish-trigger-level").textContent = n.bearishTriggerLevel == null ? "–" : Number(n.bearishTriggerLevel).toFixed(2);
  $("setup-invalidation-level").textContent = n.invalidationLevel == null ? "–" : Number(n.invalidationLevel).toFixed(2);
  $("setup-missing-condition").textContent = n.missingCondition || "無";
  $("setup-next-check").textContent = fmtTs(n.nextCheckTime);
  $("analysis-data-time").textContent = fmtTs(n.marketDataTimestamp);
  $("last-closed-time").textContent = fmtTs(n.lastClosedCandleTimestamp);
  $("analysis-generated-time").textContent = fmtTs(n.generatedAt);
  $("normalized-script").textContent = n.tradingScript || "";
  const regimes = { strong_bullish: "強勢多頭", bullish: "大週期多頭", range: "區間",
                    bearish: "大週期空頭", strong_bearish: "強勢空頭" };
  const momentums = { accelerating: "短線加速", stable: "短線穩定", weakening: "短線降溫",
                      pullback: "短線回調", reversal_risk: "短線反轉風險" };
  $("market-state-chip").textContent = `${regimes[n.marketRegime] || n.marketRegime}｜${momentums[n.shortTermMomentum] || n.shortTermMomentum}`;
  $("technical-bias").textContent = n.technicalBiasLabel;
  $("trend-score").textContent = `${n.trendScore}/100`;
  const readiness = { ready: "條件已具備", wait_confirmation: "等待確認", avoid_chasing: "避免追價", no_trade: "暫停交易" };
  const confidence = { high: "高", medium: "中", low: "低", insufficient: "不足" };
  const support = { none: "無測試", testing_support: "測試支撐", intrabar_breach: "盤中刺破",
                    confirmed_breakdown: "收盤有效跌破", failed_breakdown: "跌破後站回",
                    retest_rejected: "反抽無法站回" };
  $("entry-quality").textContent = `${readiness[n.entryReadiness] || n.entryReadiness}・${n.entryQualityScore}/100`;
  $("data-confidence").textContent = confidence[n.dataConfidence] || n.dataConfidence;
  $("support-state").textContent = support[n.supportState] || n.supportState;
  const setQuality = (id, status) => {
    const el = $(id); el.textContent = qualityZh(status);
    el.className = "chip " + (status === "GOOD" ? "good" : status === "STALE" ? "warn" : "bad");
  };
  setQuality("sys-market-quality", n.marketDataStatus);
  setQuality("sys-event-quality", n.eventDataStatus);
  $("event-data-time").textContent = fmtTs(n.eventDataTimestamp);
  $("quality-summary").textContent = n.marketDataStatus === "GOOD" && n.eventDataStatus === "FAILED"
    ? "行情資料正常；事件資料失效，本次分析未納入事件風險。"
    : `行情資料 ${qualityZh(n.marketDataStatus)}；事件資料 ${qualityZh(n.eventDataStatus)}。`;
  $("risk-label").textContent = n.riskLabel;
  $("risk-label").className = "chip " + (n.riskDirection === "none" ? "good" : "warn");
  $("risk-message").textContent = n.riskMessage || "";
}

function renderRiskPriority(n) {
  const weakness = {
    none: "未偵測到明顯轉弱", early_warning: "早期轉弱警告",
    confirmed: "短線轉弱已確認", accelerating: "短線空方動能加速",
  };
  const regimes = {
    strong_bullish: "強勢多頭", bullish: "大週期偏多", range: "區間",
    bearish: "大週期偏空", strong_bearish: "強勢空頭",
  };
  const contradictions = [];
  const td = n.tradingDecision || {};
  const marketAssessment = td.marketAssessment || {};
  const newEntry = td.newEntryDecision || {};
  const existing = td.existingPositionAssessment || {};
  if (["confirmed", "accelerating"].includes(n.shortTermWeakness) && n.longEntryAllowed) {
    contradictions.push("短線轉弱時仍允許新多單");
  }
  if (n.riskOverride === "protect_existing_long" && /立即買入|強烈買入|續抱加碼/.test(n.tradingScript || "")) {
    contradictions.push("多單保護狀態與交易文案衝突");
  }
  if (n.eventDataStatus === "FAILED" && n.dataConfidence === "high") {
    contradictions.push("事件資料失效但可信度仍為高");
  }
  if (n.entryReadiness === "no_trade" && (n.longEntryAllowed || n.shortEntryAllowed)) {
    contradictions.push("暫停交易時仍允許新進場");
  }
  if (existing.positionTimeframe === "unknown" && existing.action === "exit_confirmed") {
    contradictions.push("持倉週期未知卻要求退出");
  }
  const safe = contradictions.length > 0;
  if (safe) console.error("ANALYSIS_CONSISTENCY_ERROR", contradictions);
  $("priority-data").textContent = n.marketDataStatus !== "GOOD"
    ? `行情 ${n.marketDataStatus}，暫停交易`
    : n.eventDataStatus !== "GOOD"
      ? `行情正常；事件 ${n.eventDataStatus}，事件風險未知`
      : "行情與事件資料正常";
  $("priority-existing-long").textContent = safe
    ? "訊號矛盾，等待確認"
    : (existing.message || n.existingLongGuidance || "缺少持倉背景，無法判定續抱或平倉");
  $("priority-new-long").textContent = !safe && newEntry.longAllowed ? "允許（條件已確認）" : (newEntry.longReason || "暫停");
  $("priority-new-short").textContent = !safe && newEntry.shortAllowed ? "允許（條件已確認）" : (newEntry.shortReason || "暫停");
  const twoSided = { normal: "一般", downside_continuation: "續跌風險", oversold_rebound: "超賣急彈風險", high_whipsaw: "續跌與急彈並存" };
  const reversal = { none: "尚無", oversold_without_reversal: "超賣但未反轉", selling_exhaustion_candidate: "賣壓衰竭候選", reclaim_attempt: "嘗試收復", reversal_confirmed: "反轉已確認", reversal_failed: "收復失敗" };
  $("priority-two-sided").textContent = twoSided[marketAssessment.twoSidedRisk] || marketAssessment.twoSidedRisk || "–";
  $("priority-reversal").textContent = reversal[marketAssessment.reversalState] || marketAssessment.reversalState || "–";
  $("priority-weakness").textContent = weakness[n.shortTermWeakness] || n.shortTermWeakness;
  $("priority-regime").textContent = regimes[n.marketRegime] || n.marketRegime;
  $("priority-message").textContent = safe
    ? "訊號尚未一致，等待下一根 15 分 K 收盤確認。"
    : (n.tradingScript || "等待條件完成。");
  const critical = safe || ["high", "critical"].includes(n.positionRisk)
    || n.riskOverride === "suspend_all_entries";
  $("risk-priority-card").classList.toggle("critical", critical);
}

// ═══ V2 AI 策略面板 ═══════════════════════════════════════
const AI_ACTION_ZH = { Buy: "做多 Buy", Sell: "做空 Sell", Wait: "等待 Wait" };
const AI_BIAS_ZH = { BULLISH: "偏多", BEARISH: "偏空", NEUTRAL: "中性" };

function aiZone(resolved, id) {
  if (!id || !resolved || !resolved[id]) return "–";
  const z = resolved[id];
  if (z.price_low == null) return h`${id}`;
  const lo = Number(z.price_low).toFixed(2), hi = Number(z.price_high).toFixed(2);
  return lo === hi ? h`${lo} <span class="ai-id">${id}</span>`
                   : h`${lo} – ${hi} <span class="ai-id">${id}</span>`;
}

function renderAiStrategy(ai) {
  const box = $("ai-body");
  if (!box) return;
  if (!ai || (!ai.available && !ai.invalid)) {
    box.innerHTML = h`<div class="empty">AI 策略未產生:${(ai && ai.unavailable_reason) || "尚未啟用"}</div>`;
    return;
  }
  if (ai.invalid) {
    box.innerHTML = h`<div class="ai-gate bad">⛔ ${ai.unavailable_reason}</div>`;
    return;
  }
  const act = ai.action || {};
  const tp = ai.trade_plan || {};
  const res = tp.resolved || {};
  const conf = ai.confidence || {};
  const normalized = S.analysis && S.analysis.normalized_analysis;
  const cls = act.type === "Buy" ? "good" : act.type === "Sell" ? "bad" : "warn";

  const analysts = ai.analysts || {};
  const aNames = { macro: "巨集面", technical: "技術面", sentiment: "情緒面" };
  const analystHtml = joinSafe(Object.entries(aNames).map(([k, name]) => {
    const v = analysts[k];
    if (!v) return "";
    const biasCls = v.bias === "BULLISH" ? "good" : v.bias === "BEARISH" ? "bad" : "info";
    return h`<div class="ai-analyst">
      <div class="ai-analyst-head">${name} <span class="chip ${biasCls}">${AI_BIAS_ZH[v.bias] || v.bias} ${v.strength}</span></div>
      <div class="ai-analyst-line">${v.one_line || ""}</div>
    </div>`;
  }));

  const scenarios = joinSafe((ai.scenarios || []).map((s) => h`
    <div class="ai-scenario">
      <div class="ai-scenario-head"><b>${s.name}</b><span class="num">${s.probability_pct}%</span></div>
      <div class="ai-kv"><span>觸發</span><span>${s.trigger}</span></div>
      <div class="ai-kv"><span>應對</span><span>${s.plan}</span></div>
    </div>`));
  const factors = joinSafe((conf.factors || []).map((f) => h`<li>${f}</li>`));
  const broker = (S.analysis && S.analysis.offset_info && S.analysis.offset_info.trading_broker) || "券商";

  box.innerHTML = h`
    ${ai.gate_note ? h`<div class="ai-gate warn">🔒 ${ai.gate_note}</div>` : ""}
    <div class="ai-head">
      <span class="decision-badge ${cls}">${AI_ACTION_ZH[act.type] || act.type}</span>
      <span class="chip info">信心 ${conf.score ?? "–"}/100</span>
      <span class="chip">${ai.market_structure ? ai.market_structure.label : ""}</span>
      ${ai.cache_hit ? trusted('<span class="chip">快取</span>') : ""}
      <span class="ai-meta num">${ai.model || ""}・$${(ai.cost_usd || 0).toFixed(3)}</span>
    </div>
    <div class="ai-oneliner">${ai.one_liner || ""}</div>

    <div class="ai-grid">
      <div class="ai-col">
        <div class="ai-sec-title">市場結構判定</div>
        <p>${ai.market_structure ? ai.market_structure.reason : ""}</p>

        <div class="ai-sec-title">統一市場評估</div>
        <div class="ai-kv"><span>趨勢傾向</span><span>${normalized ? normalized.technicalBiasLabel : "–"}</span></div>
        <div class="ai-kv"><span>進場品質</span><span>${normalized ? normalized.entryReadiness : "–"}</span></div>
        <div class="ai-kv"><span>資料可信度</span><span>${normalized ? normalized.dataConfidence : "–"}</span></div>
        <p class="bias-disclaimer">技術證據傾向不是勝率；AI 不得覆寫統一進場狀態。</p>

        <div class="ai-sec-title">行動</div>
        ${act.type === "Wait" && act.wait_condition ? h`<div class="ai-kv"><span>等什麼</span><span>${act.wait_condition}</span></div>` : ""}
        <div class="ai-kv"><span>下一步觸發</span><span>${act.next_trigger || ""}</span></div>

        <div class="ai-sec-title">交易方案(${broker}掛單價)</div>
        <div class="ai-kv"><span>進場</span><span class="num">${aiZone(res, tp.entry_id)}</span></div>
        <div class="ai-kv"><span>停損</span><span class="num">${aiZone(res, tp.stop_loss_id)}</span></div>
        <div class="ai-kv"><span>TP1</span><span class="num">${aiZone(res, tp.tp1_id)}</span></div>
        <div class="ai-kv"><span>TP2</span><span class="num">${aiZone(res, tp.tp2_id)}</span></div>
        <div class="ai-kv"><span>TP3</span><span class="num">${aiZone(res, tp.tp3_id)}</span></div>
        <div class="ai-kv"><span>失效條件</span><span>${ai.invalidation || ""}</span></div>
      </div>
      <div class="ai-col">
        <div class="ai-sec-title">三情境分析(合計 100%)</div>
        ${scenarios}
        <div class="ai-sec-title">交易理由</div>
        <p>${ai.rationale || ""}</p>
        <div class="ai-sec-title">風險提醒</div>
        <p class="ai-risk">${ai.risk_warning || ""}</p>
        <div class="ai-sec-title">信心因素</div>
        <ul class="ai-factors">${factors}</ul>
        <div class="ai-sec-title">三位分析師</div>
        ${analystHtml}
      </div>
    </div>`;
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
      + (er.event_lockout ? "・鎖定中" : "")
      + (er.post_event_wait ? "・發布後等待確認" : "");
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

function renderEventOutcome(er) {
  const outcomeEl = $("event-outcome");
  if (!outcomeEl) return;
  const resultStatus = er && er.outcome_status || "not_available";
  if (resultStatus === "available") {
    const bias = { bullish_xauusd: "基本面傾向利多黃金", bearish_xauusd: "基本面傾向利空黃金", neutral: "基本面中性" }[er.fundamental_bias] || "基本面方向待確認";
    outcomeEl.hidden = false;
    const title = document.createElement("strong");
    title.textContent = "事件結果";
    outcomeEl.replaceChildren(title, document.createTextNode(`　實際 ${er.actual}／預期 ${er.forecast}`
      + `${er.previous != null ? `／前值 ${er.previous}` : ""}`), document.createElement("br"),
      document.createTextNode(`預期差 ${er.surprise}；${bias}${er.outcome_source ? `（來源：${er.outcome_source}）` : ""}`));
  } else if (er && er.event_phase === "post_release") {
    outcomeEl.hidden = false;
    outcomeEl.textContent = "事件結果：資料來源尚未提供實際值與預期值；本次僅以已收盤 K 棒與跨市場反應確認，不輸出基本面方向。";
  } else {
    outcomeEl.hidden = true;
    outcomeEl.textContent = "";
  }
}

function renderBias(b, n) {
  if (!b) return;
  const tilt = n ? n.trendScore : 50;
  $("bias-bull-fill").style.width = `${tilt}%`;
  $("bias-bear-fill").style.width = `${100 - tilt}%`;
  const strip = (s) => s.replace(/^(STRUCT|LEVEL|MOMO|HTF):/, "");
  const fill = (listId, countId, items) => {
    $(countId).textContent = items.length;
    $(listId).innerHTML = items.length
      ? items.map((x) => h`<li>${strip(x)}</li>`).join("")
      : "<li>目前無已成立條件</li>";
  };
  fill("bias-bull-list", "bias-bull-count", b.bull_evidence || []);
  fill("bias-bear-list", "bias-bear-count", b.bear_evidence || []);
  const invalid = n ? (n.invalidatedEvidence || []) : [];
  $("bias-details-invalidated").hidden = invalid.length === 0;
  $("bias-invalidated-count").textContent = invalid.length;
  $("bias-invalidated-list").innerHTML = invalid.map((x) => h`<li>${x.label}<br><small>${x.level != null ? `失效價位 ${Number(x.level).toFixed(2)}・` : ""}${x.candleTime ? fmtTs(x.candleTime) + "・" : ""}${x.reason || ""}</small></li>`).join("");
  if (b.disclaimer) $("bias-disclaimer").textContent = b.disclaimer;
  const flags = b.chase_flags || [];
  let flagBox = document.getElementById("bias-flags");
  if (!flagBox) {
    flagBox = document.createElement("div");
    flagBox.id = "bias-flags";
    flagBox.className = "bias-flags";
    $("bias-disclaimer").before(flagBox);
  }
  flagBox.innerHTML = flags.map((f) => {
    const key = f.split(":")[0];
    const title = f.split(":").slice(1).join(":");
    return h`<span class="chip warn" title="${title}">${key === "RISK" ? title : (MSG.chase[key] || key)}</span>`;
  }).join("");
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
    el.innerHTML = h`
      <div class="sc-head"><span class="sc-dir">${title}</span>
        <span class="sc-status INVALIDATED">${fatal ? "計算錯誤" : "條件不足"}</span>
        ${createdAge ? h`<span class="sc-meta-age">${createdAge}</span>` : ""}</div>
      <div class="${fatal ? "sc-invalid-fatal" : "sc-invalid"}">
        ${fatal ? "⛔ 停損計算錯誤,已攔截(系統將自動重算)" : "⚠️ 條件不足,等待更好的機會"}<br>
        <small>${shown}</small></div>`;
    return;
  }
  const rp = sc.resolved_prices || {};
  const staleTag = sc.stale
    ? h`<span class="sc-stale-tag" title="${sc.stale_reason || ""}">已過時,等待重算</span>` : "";
  const tag = offset ? h`<span class="tmgm-tag">TMGM 校正 ${offset > 0 ? "+" : ""}${offset}</span>` : "";
  const lv = (id) => {
    const z = rp[id];
    return z ? `${fmt(z.price_low)} – ${fmt(z.price_high)}` : "–";
  };
  const rrList = (sc.risk_reward || []).map((r) => h`<span class="rr-pill">賺賠比 ${r} 倍</span>`);
  const confirmList = (sc.required_confirmations || []).map((c) => h`<li>${c}</li>`);
  const targets = (sc.target_ids || []).map((t) => lv(t)).filter((x) => x !== "–").join(" / ") || "–";
  el.innerHTML = h`
    <div class="sc-head"><span class="sc-dir">${title}</span>
      <span class="sc-status ${sc.status}">${LIFECYCLE_ZH[sc.lifecycle_status] || SC_STATUS_ZH[sc.status] || "未知狀態"}</span>${staleTag}${tag}
      ${createdAge ? h`<span class="sc-meta-age">建立於 ${createdAge}</span>` : ""}</div>
    <div class="${sc.stale ? "sc-body-stale" : ""}">
    <div class="sc-levels">
      <div class="kv"><span>進場區</span><span class="num">${lv(sc.entry_zone_id)}</span></div>
      <div class="kv"><span>賺賠比計算基準價</span><span class="num">${sc.planned_entry == null ? "–" : fmt(sc.planned_entry)}</span></div>
      <div class="kv"><span>停損價</span><span class="num">${sc.stop_loss_price == null ? lv(sc.stop_loss_id) : fmt(sc.stop_loss_price)}</span></div>
      ${sc.stop_source_label ? h`<div class="kv"><span>停損來源</span><span>${sc.stop_source_label}${sc.stop_buffer ? `（緩衝 ${fmt(sc.stop_buffer)}）` : ""}</span></div>` : ""}
      <div class="kv"><span>目標價</span><span class="num">${targets}</span></div>
    </div>
    ${(rrList.length && !sc.stale) ? h`<div class="sc-rr">${joinSafe(rrList)}</div>` : ""}
    ${sc.rr_calculation_basis ? h`<small>${sc.rr_calculation_basis}；已納入目前點差</small>` : ""}
    </div>
    ${sc.setup ? h`<div class="sc-confirm">${sc.setup}</div>` : ""}
    ${confirmList.length ? h`<div class="sc-confirm">還要等這些條件:<ul>${joinSafe(confirmList)}</ul></div>` : ""}`;
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
    S.accounts = ((await getPrivate("/api/accounts", null)) || [])
      .filter((account) => account.strategy_source !== "TEACHER");
    const sel = $("pf-account");
    sel.innerHTML = S.accounts.map((a) =>
      h`<option value="${a.id}"${a.strategy_source === "SELF" ? trusted(" selected") : ""}>${a.name}</option>`).join("");
  } catch (e) { console.warn("accounts load failed", e); }
}

async function loadComparison() {
  const body = $("compare-body");
  try {
    const data = await getPrivate("/api/accounts/comparison", "compare-body");
    if (data === null) return;
    const accs = data.accounts || [];
    if (!accs.length) {
      body.innerHTML = '<div class="empty">尚無帳戶。</div>';
      return;
    }
    const f = (v, suffix = "") => (v == null ? "–" : `${v}${suffix}`);
    const pnlCell = (v) => v == null ? trusted("–")
      : h`<span class="${v >= 0 ? "cmp-pos" : "cmp-neg"}">${v >= 0 ? "+" : ""}${v}</span>`;
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
    const heads = joinSafe(accs.map((a) =>
      h`<th>${a.name}<div class="cmp-src">${a.strategy_source}</div></th>`));
    const bodyRows = joinSafe(rows.map(([label, fn]) =>
      h`<tr><td>${label}</td>${joinSafe(accs.map((a) =>
        h`<td class="num">${fn(a.stats)}</td>`))}</tr>`));
    body.innerHTML = h`
      <table class="hist-table cmp-table"><thead><tr>
        <th>指標</th>${heads}
      </tr></thead><tbody>
        ${bodyRows}
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
    const data = await getPrivate("/api/mentor/signals", "mentor-body");
    if (data === null) return;
    if (!data.has_signals) {
      body.innerHTML = '<div class="empty">目前沒有老師帶單。新增後這裡會顯示老師方向與系統方向的比對。</div>';
      return;
    }
    const alignChip = (a, text) => {
      const cls = a === "ALIGNED" ? "good" : a === "OPPOSITE" ? "bad" : "warn";
      return h`<span class="chip ${cls}">${text}</span>`;
    };
    const sysDir = (d) => d === "LONG" ? "做多" : d === "SHORT" ? "做空" : "無明確方向";
    body.innerHTML = data.signals.map((s) => h`
      <div class="mentor-card">
        <div class="mentor-head">
          <span class="pos-side">${s.direction === "LONG" ? "老師做多" : "老師做空"}</span>
          ${alignChip(s.alignment, s.alignment_text)}
          <button class="btn btn-sm" data-act="mentor-dismiss" data-id="${s.id}">移除</button>
        </div>
        <div class="kv"><span>老師進場價</span><span class="num">${fmt(s.entry_price)}</span></div>
        ${s.stop_loss != null ? h`<div class="kv"><span>老師停損(賠錢出場)</span><span class="num">${fmt(s.stop_loss)}</span></div>` : ""}
        ${(s.targets || []).length ? h`<div class="kv"><span>老師停利(目標價)</span><span class="num">${(s.targets || []).map((t) => fmt(t)).join(" / ")}</span></div>` : ""}
        <div class="kv"><span>系統目前方向</span><span>${sysDir(s.system_direction)}</span></div>
        <div class="kv"><span>與現價差</span><span class="num">${s.entry_vs_current_text || "–"}</span></div>
        ${s.note ? h`<div class="mentor-memo">老師備註:${s.note}</div>` : ""}
      </div>`).join("") +
      h`<div class="bias-disclaimer">${data.note}</div>`;
  } catch (e) {
    body.innerHTML = '<div class="empty">老師帶單載入失敗。</div>';
  }
}

async function loadMentorHistory() {
  const body = $("mentor-history");
  try {
    const data = await getPrivate("/api/mentor/history", "mentor-history");
    if (data === null) return;
    if (!data.trades.length) {
      body.innerHTML = '<div class="empty">尚無歷史紀錄。</div>';
      return;
    }
    const s = data.summary;
    const pnlCls = (v) => (v >= 0 ? "cmp-pos" : "cmp-neg");
    const gapNote = joinSafe((data.known_gaps || []).map((g) =>
      h`<div class="mentor-gap">⚠ 已知資料缺口:${g} —— 這段期間「沒有紀錄」,不代表老師空手</div>`));
    const tradeRows = joinSafe(data.trades.map((t) => h`<tr>
          <td class="${t.direction === "LONG" ? "cmp-pos" : "cmp-neg"}">${t.direction === "LONG" ? "做多" : "做空"}</td>
          <td class="num">${fmt(t.entry_price)} → ${fmt(t.close_price)}</td>
          <td class="num">${fmt(t.points)}</td>
          <td class="num">${fmt(t.lots)}</td>
          <td class="num ${pnlCls(t.pl_usd)}">${t.pl_usd >= 0 ? "+" : ""}${fmt(t.pl_usd)}</td>
          <td class="num mentor-nodata" title="歷史匯入,無停損資料">${t.stop_loss != null ? fmt(t.stop_loss) : "—"}</td>
          <td class="num">${(t.close_time || "").slice(0, 16).replace("T", " ")}</td>
        </tr>`));
    body.innerHTML = h`
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
        ${tradeRows}
      </tbody></table></div>
      <div class="bias-disclaimer">${data.note}</div>`;
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
    const rows = await getPrivate("/api/positions", "position-list");
    if (rows === null) return;
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
  const histList = [
    ...(p.stop_modification_history || []).map((x) =>
      h`<li>${x.time.slice(5, 16).replace("T", " ")} 停損 ${fmt(x.old_stop)} → ${fmt(x.new_stop)}${x.widening ? "(⚠ 擴大)" : ""}</li>`),
    ...(p.partial_exit_history || []).map((x) =>
      h`<li>${x.time.slice(5, 16).replace("T", " ")} 平倉 ${x.percent}% @ ${fmt(x.price)}(R=${x.r_at_exit ?? "–"})</li>`),
  ];
  const targets = (p.planned_targets || []).map((t) => fmt(t)).join(" / ") || "–";
  const eventHold = p.allow_event_hold == null ? "未設定" : p.allow_event_hold ? "允許" : "不允許";
  const normalized = S.analysis && S.analysis.normalized_analysis;
  let positionAdvice = p.recommended_action || "";
  if (p.is_open && normalized) {
    const assessment = normalized.tradingDecision?.existingPositionAssessment;
    if (assessment && assessment.direction === p.side.toLowerCase()) {
      positionAdvice = assessment.message || positionAdvice;
    }
  }
  const actions = h`
    <div class="pos-actions">
      <button class="btn btn-sm btn-warn" data-act="pos-stop" data-id="${p.id}">改出場價</button>
      <button class="btn btn-sm" data-act="pos-partial" data-id="${p.id}">分批平倉</button>
      <button class="btn btn-sm" data-act="pos-context" data-id="${p.id}">補交易背景</button>
      <button class="btn btn-sm btn-danger" data-act="pos-close" data-id="${p.id}">全部平倉</button>
    </div>`;
  return h`
  <div class="pos-card ${p.side.toLowerCase()}" data-id="${p.id}">
    <div class="pos-head">
      <span class="pos-side">${p.side === "LONG" ? "做多" : "做空"}</span>
      <span class="chip info">${accountName(p.account_id)}</span>
      <span class="num">${fmt(p.lot_size)} 手・剩餘 ${p.remaining_percent}%</span>
      ${p.is_open ? "" : trusted('<span class="pos-closed-tag">已平倉</span>')}
      ${pnl != null ? h`<span class="pos-pnl ${pnl >= 0 ? "pos" : "neg"}">${pnl >= 0 ? "+" : ""}${fmt(pnl)} USD</span>` : ""}
    </div>
    <div class="pos-meta">
      <span>進場 <span class="num">${fmt(p.entry_price)}</span></span>
      <span>賠錢出場價 <span class="num">${fmt(p.stop_loss)}</span></span>
      <span>交易週期 <b>${p.position_timeframe || "unknown"}</b></span>
      <span>最大風險 <b>${p.max_loss_usd == null ? "未設定" : "USD " + fmt(p.max_loss_usd)}</b></span>
      <span>重大事件續抱 <b>${eventHold}</b></span>
      <span>目標價 <span class="num">${targets}</span></span>
      <span>開倉 <span class="num">${p.open_time.slice(5, 16).replace("T", " ")}</span></span>
    </div>
    ${p.is_open ? h`
    <div class="pos-row"><div class="lbl"><span>賺賠比進度(回本 → 3 倍)</span>
      <span class="num">${r == null ? "沒設出場價" : fmt(r, 2) + " 倍"}</span></div>
      <div class="progress"><div class="fill" style="width:${rPct + "%"}"></div></div></div>
    ${positionAdvice ? h`<div class="pos-advice">${positionAdvice}</div>` : ""}
    ${p.original_thesis ? h`<div class="pos-advice">原始理由：${p.original_thesis}</div>` : ""}
    ${actions}` : ""}
    ${histList.length ? h`<details class="pos-hist"><summary>操作歷史</summary><ul>${joinSafe(histList)}</ul></details>` : ""}
  </div>`;
}

// ═══ 管理登入 / 私人資料存取 ═══════════════════════════════
// 單一共享登入流程:多個私人請求同時 401 時只跳一次輸入框。token 不寫入任何儲存。
let _loginInflight = null;
function ensureLogin() {
  if (_loginInflight) return _loginInflight;
  _loginInflight = (async () => {
    const token = prompt("請輸入管理 token 以檢視/操作個人資料(登入後不再重複詢問):");
    if (!token) return false;
    try {
      const r = await fetch("/api/admin/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }), credentials: "same-origin",
      });
      if (r.ok) { await onLoggedIn(); return true; }
      return false;
    } catch (e) { return false; }
  })().finally(() => { _loginInflight = null; });
  return _loginInflight;
}

async function refreshAuthState() {
  try {
    const s = await (await fetch("/api/admin/status", { credentials: "same-origin" })).json();
    S.authed = !!s.authenticated;
  } catch (e) { S.authed = false; }
  updateAuthUI();
}

function updateAuthUI() {
  const inBtn = $("admin-login-btn"), outBtn = $("admin-logout-btn");
  if (inBtn) inBtn.hidden = S.authed;
  if (outBtn) outBtn.hidden = !S.authed;
}

// 登入成功:更新 UI、重載私人面板、連上私人 WS
async function onLoggedIn() {
  S.authed = true;
  updateAuthUI();
  connectPrivateWS();
  reloadPrivatePanels();
}

// 登出或 session 過期:清空私人 DOM、關閉私人 WS、回到未登入狀態
function clearPrivate(reason) {
  S.authed = false;
  updateAuthUI();
  if (S.privWs) { try { S.privWs.close(); } catch (e) { /* noop */ } S.privWs = null; }
  for (const id of PRIVATE_PANELS) lockPanel(id, reason);
}

async function doLogout() {
  try { await fetch("/api/admin/logout", { method: "POST", credentials: "same-origin" }); }
  catch (e) { /* noop */ }
  clearPrivate();
}

// 私人面板未登入時的佔位(不殘留舊資料)
function lockPanel(id, reason) {
  const el = $(id);
  if (!el) return;
  el.innerHTML = h`<div class="empty locked">🔒 私人資料,登入後查看${reason ? "(" + reason + ")" : ""}
    <button class="btn btn-sm" data-act="admin-login">登入</button></div>`;
}

function reloadPrivatePanels() {
  const active = document.querySelector(".tab.active");
  const tab = active ? active.dataset.tab : "";
  loadAccounts();
  if (tab === "position") loadPositions();
  else if (tab === "coach") loadCoach();
}

// 私人資料 GET:帶 cookie;未登入 → 鎖定面板(不自動跳登入,由使用者按登入鈕觸發共享流程)。
// 回傳 null 表示未授權(呼叫端顯示鎖定佔位)。
async function getPrivate(url, panelId) {
  if (!S.authed) { if (panelId) lockPanel(panelId); return null; }
  const r = await fetch(url, { credentials: "same-origin" });
  if (r.status === 401 || r.status === 403) { clearPrivate("登入已失效"); return null; }
  if (!r.ok) return null;
  return r.json();
}

async function postJSON(url, body) {
  const send = () => fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body), credentials: "same-origin",
  });
  let r = await send();
  if ((r.status === 401 || r.status === 403) && await ensureLogin()) {
    r = await send();   // 登入成功後重試一次(共享流程)
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.status);
  return data;
}

// 私人 WebSocket:登入後連線;收到分析更新即重載開啟中的私人面板;斷線視為過期。
function connectPrivateWS() {
  if (S.privWs) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/private`);
  S.privWs = ws;
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "analysis") reloadPrivatePanels();
    } catch (err) { /* noop */ }
  };
  ws.onclose = () => {
    if (S.privWs === ws) { S.privWs = null; if (S.authed) clearPrivate("連線已中斷,請重新登入"); }
  };
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

async function actPositionContext(id) {
  const timeframe = prompt("持倉判斷週期（15M / 1H / 4H / 1D）：", "1H");
  if (!timeframe) return;
  const thesis = prompt("原始進場理由（例如：1H 高低點持續墊高）：", "");
  if (thesis == null) return;
  const maxLoss = prompt("最大可承受損失（USD，可留空；若沒有原始停損則建議填寫）：", "");
  const eventHold = prompt("重大事件期間是否允許續抱？輸入 yes / no；留空代表未設定：", "");
  const normalizedEventHold = eventHold == null || eventHold.trim() === ""
    ? null : ["yes", "y", "true", "是"].includes(eventHold.trim().toLowerCase());
  try {
    await postJSON(`/api/positions/${id}/context`, {
      position_timeframe: timeframe.trim().toUpperCase(),
      original_thesis: thesis.trim(),
      max_loss_usd: maxLoss && maxLoss.trim() ? parseFloat(maxLoss) : null,
      allow_event_hold: normalizedEventHold,
    });
  } catch (e) { alert("更新失敗：" + e.message); }
  loadPositions();
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
    const flags = await getPrivate("/api/behavior/flags", "coach-body");
    if (flags === null) return;
    if (!flags.length) {
      body.innerHTML = '<div class="empty">尚無行為標籤。當持倉操作觸發紀律問題(擴大停損、過早平倉…)時會顯示於此。</div>';
      return;
    }
    body.innerHTML = flags.map((f) => h`
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
    const taipeiTime = (value) => new Intl.DateTimeFormat("zh-TW", {
      timeZone: "Asia/Taipei", month: "2-digit", day: "2-digit", hour: "2-digit",
      minute: "2-digit", hour12: false,
    }).format(new Date(value));
    const groups = [];
    for (const row of rows) {
      const key = [row.setup_id || "", row.lifecycle_status || "NO_SETUP", row.action,
        row.trade_status || "WAIT_CONFIRMATION", row.can_enter ? "CAN_ENTER" : "BLOCKED",
        ...(row.blocking_reasons || [])].join("|");
      const last = groups[groups.length - 1];
      if (last && last.key === key) last.rows.push(row);
      else groups.push({ key, rows: [row] });
    }
    const histRows = joinSafe(groups.map((group) => {
      const r = group.rows[0];
      const repeated = group.rows.length > 1 ? `（持續 ${group.rows.length} 次）` : "";
      const tradeStatusZh = {
        READY: "條件成立", WAIT_CONFIRMATION: "等待確認", BLOCKED_RR: "賺賠比不足",
        MISSED_ENTRY: "錯過進場", INVALIDATED: "劇本失效", BLOCKED_DATA: "資料阻擋",
        BLOCKED_EVENT: "事件鎖定", MANAGE: "持倉管理",
      };
      return h`<tr>
        <td class="num">${taipeiTime(r.run_time)}</td>
        <td>${stateZh(r.market_state)}</td>
        <td>${lifecycleZh(r.lifecycle_status || "NO_SETUP")} ${repeated}</td>
        <td><span class="act-pill ${decisionClass(r.action)}">${actionZh(r.action)}</span></td>
        <td><span class="grade-badge g-${r.grade}" title="${gradeZh(r.grade)}" style="width:auto;padding:0 8px;font-size:.75rem">${gradeZh(r.grade)}</span></td>
        <td class="num">${r.signal_score == null ? "未取得" : r.signal_score}</td>
        <td>${tradeStatusZh[r.trade_status] || r.trade_status || "等待確認"}</td>
        <td>${r.can_enter ? "可以考慮進場" : "尚不可進場"}</td>
        <td>${qualityZh(r.quality)}</td>
        <td>${r.blocked_reason || (r.blocking_reasons || []).map(blockReasonZh).join("、") || r.missing_score_reason || "無"}</td>
        <td class="num" title="${r.setup_id || ""}">${r.closed_bars_since_breakout || 0} 根</td></tr>`;
    }));
    body.innerHTML = h`<table class="hist-table"><thead><tr>
      <th>時間（台灣）</th><th>市場狀態</th><th>劇本階段</th><th>決策</th><th>信心</th><th>分數</th><th>交易狀態</th><th>進場許可</th><th>品質</th><th>阻擋原因</th><th>已等待</th>
      </tr></thead><tbody>${histRows}</tbody></table>`;
  } catch (e) {
    body.innerHTML = '<div class="empty">歷史紀錄載入失敗。</div>';
  }
}

async function loadPerformance() {
  const body = $("performance-body");
  try {
    const report = await (await fetch("/api/performance")).json();
    const labels = { overall: "整體", direction: "方向", score_band: "分數區間",
      market_state: "市場狀態", session: "交易時段", setup_state: "戰術狀態",
      signal_mode: "訊號類型", stop_source: "停損來源" };
    const cards = [];
    const eventPerf = report.decision_events || {};
    cards.push(h`<div class="performance-card calibration-card">
      <h4>實際決策事件追蹤</h4>
      <p>只統計曾正式成立的可執行訊號，並扣除記錄到的點差、滑價與費用。</p>
      <div class="performance-metrics">
        <span>事件樣本<b class="num">${eventPerf.sample_size || 0}</b></span>
        <span>1 小時已結算<b class="num">${eventPerf.settled_1h || 0}</b></span>
        <span>正報酬比例<b class="num">${eventPerf.win_rate_1h_pct == null ? "—" : eventPerf.win_rate_1h_pct + "%"}</b></span>
        <span>可供校準<b>${eventPerf.calibration_ready ? "是" : "樣本收集中"}</b></span>
      </div><small>正報酬比例不是未來勝率；未滿 30 筆不會調整正式門檻。</small>
    </div>`);
    const shadow = report.shadow_mode || {};
    cards.push(h`<div class="performance-card calibration-card">
      <h4>影子驗證</h4>
      <p>目前只記錄判斷結果，不影響正式買賣建議。</p>
      <div class="performance-metrics">
        <span>1 小時樣本<b class="num">${shadow.sample_size_1h || 0}</b></span>
        <span>驗證門檻<b class="num">${shadow.minimum_validation_samples || 60}</b></span>
        <span>狀態<b>${shadow.promotion_status === "validated" ? "驗證通過" : shadow.promotion_status === "not_validated" ? "尚未通過" : "收集中"}</b></span>
      </div><small>不會自動調整參數，也不會自動升級成正式訊號。</small>
    </div>`);
    const recommendations = report.calibration_recommendations || [];
    if (recommendations.length) cards.push(h`<div class="performance-card calibration-card">
      <h4>校正建議（僅供人工檢視）</h4>
      ${joinSafe(recommendations.map((item) => h`<p><b>${item.scope}・${item.horizon}</b><br>${item.message}<br><small>${item.walk_forward_status === "validated" ? "樣本外驗證：已通過" : "樣本外驗證：尚未通過（僅供檢視）"}</small></p>`))}
    </div>`);
    for (const [group, rows] of Object.entries(report.groups || {})) {
      for (const row of rows) {
        if ((group === "setup_state" || group === "signal_mode") && row.key === "LIVE") continue;
        cards.push(h`<div class="performance-card">
        <h4>${labels[group] || group}：${row.key} · ${row.horizon}</h4>
        <div class="performance-metrics">
          <span>樣本<b class="num ${row.sufficient_sample ? "" : "sample-low"}">${row.sample_size}</b></span>
          <span>勝率<b class="num">${row.win_rate_pct == null ? "—" : row.win_rate_pct + "%"}</b></span>
          <span>平均報酬<b class="num">${row.average_return_pct == null ? "—" : row.average_return_pct + "%"}</b></span>
          <span>平均順行<b class="num">${row.average_mfe_pct == null ? "—" : row.average_mfe_pct + "%"}</b></span>
          <span>平均逆行<b class="num">${row.average_mae_pct == null ? "—" : row.average_mae_pct + "%"}</b></span>
        </div>${row.sufficient_sample ? "" : '<div class="sample-low">樣本不足，暫不可據此調參</div>'}</div>`);
      }
    }
    body.innerHTML = h`<div class="performance-note">有效訊號 ${report.eligible_signals} 筆；最低可信樣本 ${report.minimum_sample_size} 筆。自動調參：關閉。</div>
      <div class="performance-grid">${joinSafe(cards) || '<div class="empty">尚無已完成的訊號結果</div>'}</div>`;
  } catch (e) {
    body.innerHTML = '<div class="empty">績效資料讀取失敗</div>';
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
      else if (msg.type === "decision_state") renderFinalDecision(msg.data);
      else if (msg.type === "decision_event") {
        if (S.analysis) S.analysis.final_decision_state.latest_event = msg.data;
        renderFinalDecision({ ...(S.analysis && S.analysis.final_decision_state || {}), latest_event: msg.data });
        refreshTelegramStatus();
      }
      else if (msg.type === "analysis_refreshing") {
        $("quick-action-title").textContent = "判斷更新中";
        $("quick-action-why").textContent = "新一根 15 分鐘 K 棒已收盤，系統正在重新判斷。";
        $("quick-action-next").textContent = "更新完成前先不要進場";
        $("quick-action-card").dataset.action = "NO_TRADE";
      }
      else if (msg.type === "candle_closed") loadCandles(S.tf, true).catch(console.error);
    } catch (err) { console.warn("ws message error", err); }
  };
}

/* ═══ 啟動 ═══ */
async function boot() {
  initChart();
  const advancedToggle = $("mobile-advanced-toggle");
  if (advancedToggle) advancedToggle.addEventListener("click", () => {
    const open = document.body.classList.toggle("mobile-advanced-open");
    advancedToggle.setAttribute("aria-expanded", String(open));
    advancedToggle.textContent = open ? "收起進階資訊" : "顯示進階資訊";
  });
  [["trend-bias", "市場細節"], ["technical-bias", "技術證據與分數"],
   ["event-countdown", "事件資訊"], ["sys-provider", "資料與風險細節"]]
    .forEach(([id, label]) => collapseSideCard(id, label));
  document.querySelectorAll(".tf-btn").forEach((b) =>
    b.addEventListener("click", () => switchTF(b.dataset.tf)));
  $("chart-detail-toggle").addEventListener("click", () => {
    S.showAllMarkers = !S.showAllMarkers;
    $("chart-detail-toggle").textContent = S.showAllMarkers ? "只看關鍵標記" : "顯示全部標記";
    applyOverlays().catch(console.error);
  });
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      $("panel-" + t.dataset.tab).classList.add("active");
      if (t.dataset.tab === "history") loadHistory();
      if (t.dataset.tab === "performance") loadPerformance();
      if (t.dataset.tab === "position") loadPositions();
      if (t.dataset.tab === "coach") loadCoach();
    }));

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
        position_timeframe: $("pf-timeframe").value,
        original_thesis: $("pf-thesis").value.trim(),
        max_loss_usd: $("pf-max-loss").value ? parseFloat($("pf-max-loss").value) : null,
        allow_event_hold: $("pf-event-hold").value === "" ? null : $("pf-event-hold").value === "true",
        planned_targets: targets,
        account_id: parseInt($("pf-account").value, 10) || null,
      });
      e.target.reset();
      loadPositions();
    } catch (err) { alert("新增失敗:" + err.message); }
  });

  // 事件委派:動態產生的按鈕改用 data-act(取代 inline onclick,配合嚴格 CSP)
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const id = parseInt(btn.dataset.id, 10);
    const act = btn.dataset.act;
    if (act === "mentor-dismiss") dismissMentor(id);
    else if (act === "pos-stop") actStop(id);
    else if (act === "pos-partial") actPartial(id);
    else if (act === "pos-context") actPositionContext(id);
    else if (act === "pos-close") actClose(id);
    else if (act === "admin-login") ensureLogin();
  });

  $("admin-login-btn").addEventListener("click", () => ensureLogin());
  $("admin-logout-btn").addEventListener("click", () => doLogout());
  $("telegram-test-btn").addEventListener("click", async () => {
    try {
      await api("/api/telegram/test", { method: "POST" });
      $("telegram-delivery").textContent = "通知傳送中";
      setTimeout(refreshTelegramStatus, 1500);
    } catch (err) { alert("測試通知建立失敗：" + err.message); }
  });

  connectWS();
  await refreshAuthState();          // 判斷是否已登入(session cookie),更新 UI
  if (S.authed) { connectPrivateWS(); loadAccounts(); }
  else { for (const id of PRIVATE_PANELS) lockPanel(id); }
  setupOffsetEditor();
  try { renderOffset(await (await fetch("/api/offset")).json()); } catch (e) { /* noop */ }

  try {
    const h = await (await fetch("/health")).json();
    const mk = $("chip-market");
    unskel(mk);
    mk.textContent = h.market_open ? "開盤中" : "休市";
    mk.className = "chip " + (h.market_open ? "good" : "warn");
  } catch (e) { /* noop */ }
  await refreshTelegramStatus();
  setInterval(refreshTelegramStatus, 15000);

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
