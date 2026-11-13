/**
 * ArbitrageX — Cross-Venue Prediction Market Arbitrage Terminal Frontend
 * Connects to FastAPI REST & WebSocket (/ws/stream) endpoints when served by
 * server.py, with exact local Kalshi quadratic fee fallback.
 */

const FALLBACK_MARKETS = [
  {
    id: "kalshi:FED-DEC26-CUT25",
    title: "US Federal Reserve Rate Cut Dec 2026 · Cut >= 25bps",
    category: "economics",
    daysToResolution: 45,
    resolutionDate: "2026-12-16",
    rules: "same",
    rulesAccepted: true,
    rulesReason: "Both resolve on official FOMC target rate announcement.",
    leg1: { venue: "kalshi", side: "yes", price: 0.45, feeRate: 0.0385, depth: 420, link: "https://kalshi.com" },
    leg2: { venue: "polymarket", side: "no", price: 0.49, feeRate: 0.0, depth: 650, link: "https://polymarket.com" }
  },
  {
    id: "kalshi:CPI-Q426-U25",
    title: "US Headline CPI YoY < 2.5% in Q4 2026 · Yes",
    category: "economics",
    daysToResolution: 60,
    resolutionDate: "2026-12-12",
    rules: "same",
    rulesAccepted: true,
    rulesReason: "Both resolve on BLS CPI-U NSA release.",
    leg1: { venue: "kalshi", side: "yes", price: 0.39, feeRate: 0.0427, depth: 380, link: "https://kalshi.com" },
    leg2: { venue: "polymarket", side: "no", price: 0.56, feeRate: 0.0, depth: 520, link: "https://polymarket.com" }
  },
  {
    id: "kalshi:BTC-120K-EOY26",
    title: "Bitcoin >= $120,000 Before 2027 · Yes",
    category: "crypto",
    daysToResolution: 60,
    resolutionDate: "2026-12-31",
    rules: "same",
    rulesAccepted: true,
    rulesReason: "Both resolve on CF Benchmarks BRTI / spot index; Poly framed inversely.",
    leg1: { venue: "kalshi", side: "yes", price: 0.50, feeRate: 0.0350, depth: 520, link: "https://kalshi.com" },
    leg2: { venue: "polymarket", side: "no", price: 0.44, feeRate: 0.0, depth: 780, link: "https://polymarket.com" }
  }
];

const state = {
  viewMode: "landing",
  dataSource: "demo",
  markets: JSON.parse(JSON.stringify(FALLBACK_MARKETS)),
  activeTab: "scanner",
  filterCategory: "all",
  filterMinEdge: 0.0,
  filterVenues: "all",
  sortBy: "edge-desc",
  searchQuery: "",
  liveStreamActive: true,
  audioAlertsEnabled: false,

  replaySummary: {
    initial_capital: 1000.0,
    final_equity: 1748.05,
    realized_pnl: 748.05,
    roi_pct: 74.81,
    total_trades: 29,
    winning_trades: 29,
    win_rate_pct: 100.0,
    sharpe_ratio_annualized: 8.59,
    max_drawdown_pct: 5.64,
    max_drawdown_usd: 82.26,
    total_fees_paid: 499.21,
    total_slippage_cost: 3.02
  },
  botRunning: true,
  botMinEdge: 2.0,
  botMaxSize: 450,
  botTakeProfit: 2.0,
  botPositions: [],
  tradeLedger: [],

  calcMarketId: FALLBACK_MARKETS[0].id,
  calcCapital: 1000.0,
  selectedMarketDetail: null,
  ws: null
};

// ============================================================================
// EXACT FEE & ARBITRAGE MATH
// ============================================================================

function kalshiQuadraticFee(price, contracts) {
  if (price <= 0 || price >= 1 || contracts <= 0) return 0;
  const raw = 0.07 * contracts * price * (1 - price);
  return Math.ceil(Math.round(raw * 1e9) / 1e9 * 100 - 1e-9) / 100;
}

function calculateArbMetrics(market, contracts = 500) {
  const p1 = market.leg1.price;
  const p2 = market.leg2.price;
  const fee1Total = market.leg1.venue === "kalshi"
    ? kalshiQuadraticFee(p1, contracts)
    : contracts * p1 * (market.leg1.feeRate || 0);
  const fee2Total = market.leg2.venue === "kalshi"
    ? kalshiQuadraticFee(p2, contracts)
    : contracts * p2 * (market.leg2.feeRate || 0);

  const f1 = fee1Total / contracts;
  const f2 = fee2Total / contracts;

  const grossCost = p1 + p2;
  const totalFees = f1 + f2;
  const totalNetCost = grossCost + totalFees;

  const netEdgePerPair = 1.0 - totalNetCost;
  const netEdgePct = totalNetCost > 0 ? (netEdgePerPair / totalNetCost) * 100 : 0;

  const days = Math.max(market.daysToResolution || 30, 1);
  const annualizedApy = totalNetCost > 0 && netEdgePct > 0
    ? (Math.pow(1 + netEdgePct / 100, 365 / days) - 1) * 100
    : 0;

  const minDepth = Math.min(market.leg1.depth || 0, market.leg2.depth || 0);
  return {
    grossCost,
    totalFees,
    totalNetCost,
    netEdgePerPair,
    netEdgePct,
    annualizedApy,
    minDepth,
    maxDeployable: minDepth * totalNetCost
  };
}

let audioCtx = null;
function playArbChime() {
  if (!state.audioAlertsEnabled) return;
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = "sine";
    const now = audioCtx.currentTime;
    osc.frequency.setValueAtTime(587.33, now);
    osc.frequency.exponentialRampToValueAtTime(880.0, now + 0.12);
    gain.gain.setValueAtTime(0.06, now);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start(now);
    osc.stop(now + 0.35);
  } catch (_) {}
}

function showToast(message, type = "success") {
  const container = document.getElementById("toast-container");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.innerHTML = `<span style="font-size:16px;">${type === "success" ? "⚡" : "ℹ️"}</span><span>${escapeHtml(message)}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(50px)";
    toast.style.transition = "all 0.3s ease";
    setTimeout(() => toast.remove(), 300);
  }, 3200);
}

function appendBotLog(message, level = "info") {
  const terminal = document.getElementById("bot-terminal-logs");
  if (!terminal) return;
  const ts = new Date().toISOString().substring(11, 19);
  const line = document.createElement("div");
  line.className = `log-line ${level}`;
  line.textContent = `[${ts}] ${message}`;
  terminal.appendChild(line);
  while (terminal.children.length > 60) {
    terminal.removeChild(terminal.firstChild);
  }
  terminal.scrollTop = terminal.scrollHeight;
}

function escapeHtml(str) {
  return String(str).replace(/[&<>'"]/g, tag => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  }[tag] || tag));
}

// ============================================================================
// VIEW & TAB NAVIGATION
// ============================================================================

function setViewMode(mode) {
  state.viewMode = mode;
  const landingCont = document.getElementById("landing-page-container");
  const terminalCont = document.getElementById("terminal-workspace-container");
  const siteNavLinks = document.getElementById("site-nav-links");
  const terminalNavTabs = document.getElementById("terminal-nav-tabs");
  const toggleText = document.getElementById("toggle-view-text");
  const dataSourceSwitcher = document.getElementById("data-source-switcher");

  if (mode === "terminal") {
    document.body.className = "mode-terminal";
    landingCont.classList.add("hidden");
    terminalCont.classList.remove("hidden");
    siteNavLinks.classList.add("hidden");
    terminalNavTabs.classList.remove("hidden");
    dataSourceSwitcher.classList.remove("hidden");
    if (toggleText) toggleText.textContent = "Back to Overview";
    renderMetricsBar();
    renderScannerView();
    updateCalculatorDisplay();
    renderBotPositions();
    renderTradeLedger();
  } else {
    document.body.className = "mode-landing";
    landingCont.classList.remove("hidden");
    terminalCont.classList.add("hidden");
    siteNavLinks.classList.remove("hidden");
    terminalNavTabs.classList.add("hidden");
    dataSourceSwitcher.classList.add("hidden");
    if (toggleText) toggleText.textContent = "Launch Terminal";
  }
}

function switchTab(tabId) {
  state.activeTab = tabId;
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.tab === tabId);
  });
  document.querySelectorAll(".view-panel").forEach(panel => {
    panel.classList.toggle("active", panel.id === `view-${tabId}`);
  });
}

// ============================================================================
// SERVER DATA & WEBSOCKET INTEGRATION
// ============================================================================

async function hydrateFromServer() {
  try {
    const [mRes, pRes, bRes] = await Promise.all([
      fetch("/api/markets"),
      fetch("/api/paper-account"),
      fetch("/api/bot/status")
    ]);
    if (mRes.ok) {
      const mJson = await mRes.json();
      if (mJson.markets && mJson.markets.length > 0) {
        state.markets = mJson.markets;
        state.calcMarketId = mJson.markets[0].id;
      }
    }
    if (pRes.ok) {
      const pJson = await pRes.json();
      if (pJson.summary) state.replaySummary = pJson.summary;
      if (Array.isArray(pJson.trades)) state.tradeLedger = pJson.trades;
      if (Array.isArray(pJson.open_positions)) state.botPositions = pJson.open_positions;
    }
    if (bRes.ok) {
      const bJson = await bRes.json();
      state.botRunning = Boolean(bJson.bot_running);
      if (bJson.config) {
        state.botMinEdge = bJson.config.min_entry_edge_pct;
        state.botMaxSize = bJson.config.max_position_usd;
        state.botTakeProfit = bJson.config.take_profit_pct;
      }
    }
  } catch (_) {
    // Graceful static-file fallback if opened without server.py
  }

  populateCalculatorMarketSelector();
  renderSummaryHeaders();
  renderMetricsBar();
  renderScannerView();
  updateCalculatorDisplay();
  renderBotPositions();
  renderTradeLedger();
}

function connectWebSocket() {
  if (window.location.protocol === "file:") return;
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  try {
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/stream`);
    state.ws = ws;
    ws.onmessage = (event) => {
      if (!state.liveStreamActive || state.dataSource === "live") return;
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "book_tick" && msg.market) {
          const idx = state.markets.findIndex(m => m.id === msg.market.id);
          if (idx >= 0) {
            state.markets[idx] = msg.market;
            renderMetricsBar();
            renderScannerView();
            if (state.calcMarketId === msg.market.id) updateCalculatorDisplay();
            const row = document.querySelector(`tr[data-id="${msg.market.id}"]`);
            if (row) {
              row.classList.add("row-flash");
              setTimeout(() => row.classList.remove("row-flash"), 900);
            }
          }
        }
      } catch (_) {}
    };
    ws.onclose = () => {
      setTimeout(() => {
        if (state.liveStreamActive) connectWebSocket();
      }, 4000);
    };
  } catch (_) {}
}

async function fetchLivePolymarketData() {
  try {
    showToast("Querying Polymarket Gamma API...", "info");
    const res = await fetch("/api/live-polymarket");
    const json = await res.json();
    if (json.markets && json.markets.length > 0) {
      state.markets = json.markets;
      state.calcMarketId = json.markets[0].id;
      populateCalculatorMarketSelector();
      renderMetricsBar();
      renderScannerView();
      updateCalculatorDisplay();
      showToast(
        json.success
          ? `Loaded ${json.markets.length} live markets from Polymarket Gamma API`
          : "Live Polymarket API unreachable; using simulated L2 fallback",
        json.success ? "success" : "info"
      );
    }
  } catch (_) {
    showToast("Live query offline; using simulated L2 feed", "info");
  }
}

async function loadSimulationData() {
  try {
    const res = await fetch("/api/markets");
    const json = await res.json();
    if (json.markets && json.markets.length > 0) {
      state.markets = json.markets;
      state.calcMarketId = json.markets[0].id;
    }
  } catch (_) {
    state.markets = JSON.parse(JSON.stringify(FALLBACK_MARKETS));
  }
  populateCalculatorMarketSelector();
  renderMetricsBar();
  renderScannerView();
  updateCalculatorDisplay();
  showToast("Switched to Simulated L2 Order-Book Feed");
}

// ============================================================================
// RENDERERS
// ============================================================================

function renderSummaryHeaders() {
  const s = state.replaySummary;
  const headerBankroll = document.getElementById("header-bankroll");
  if (headerBankroll) headerBankroll.textContent = `$${Number(s.final_equity).toFixed(2)}`;

  const lInit = document.getElementById("landing-stat-init");
  const lFinal = document.getElementById("landing-stat-final");
  const lSharpe = document.getElementById("landing-stat-sharpe");
  const lTrades = document.getElementById("landing-stat-trades");

  if (lInit) lInit.textContent = `$${Number(s.initial_capital).toLocaleString(undefined, { minimumFractionDigits: 2 })}`;
  if (lFinal) lFinal.textContent = `$${Number(s.final_equity).toLocaleString(undefined, { minimumFractionDigits: 2 })}`;
  if (lSharpe) lSharpe.textContent = Number(s.sharpe_ratio_annualized).toFixed(2);
  if (lTrades) lTrades.textContent = `${s.winning_trades} / ${s.total_trades}`;
}

function renderMetricsBar() {
  const enriched = state.markets.map(m => ({ market: m, metrics: calculateArbMetrics(m) }));
  const positiveArbs = enriched.filter(e => e.metrics.netEdgePct > 0);
  const bestEdgeItem = [...positiveArbs].sort((a, b) => b.metrics.netEdgePct - a.metrics.netEdgePct)[0];
  const s = state.replaySummary;

  const countEl = document.getElementById("metric-active-arbs");
  const tabCountEl = document.getElementById("tab-opp-count");
  const edgeEl = document.getElementById("metric-highest-edge");
  const bestMarketEl = document.getElementById("metric-best-market");
  const finalEqEl = document.getElementById("metric-final-equity");
  const sharpeEl = document.getElementById("metric-sharpe-badge");
  const ddFooter = document.getElementById("metric-dd-footer");
  const pnlEl = document.getElementById("metric-total-pnl");
  const winEl = document.getElementById("metric-win-rate");
  const feesFooter = document.getElementById("metric-fees-footer");

  if (countEl) countEl.textContent = positiveArbs.length;
  if (tabCountEl) tabCountEl.textContent = positiveArbs.length;
  if (edgeEl && bestEdgeItem) edgeEl.textContent = `+${bestEdgeItem.metrics.netEdgePct.toFixed(2)}%`;
  if (bestMarketEl && bestEdgeItem) bestMarketEl.textContent = bestEdgeItem.market.title;
  if (finalEqEl) finalEqEl.textContent = `$${Number(s.final_equity).toLocaleString(undefined, { minimumFractionDigits: 2 })}`;
  if (sharpeEl) sharpeEl.textContent = `Sharpe ${Number(s.sharpe_ratio_annualized).toFixed(2)}`;
  if (ddFooter) ddFooter.textContent = `60d Sim Replay · Max MTM DD -${Number(s.max_drawdown_pct).toFixed(2)}%`;
  if (pnlEl) pnlEl.textContent = `+$${Number(s.realized_pnl).toFixed(2)}`;
  if (winEl) winEl.textContent = `${s.winning_trades}/${s.total_trades} (${Number(s.win_rate_pct).toFixed(0)}%)`;
  if (feesFooter) feesFooter.textContent = `$${Number(s.total_fees_paid).toFixed(2)} fees · $${Number(s.total_slippage_cost).toFixed(2)} L2 slippage`;
}

function getFilteredAndSortedMarkets() {
  let list = state.markets.map(m => ({ market: m, metrics: calculateArbMetrics(m) }));
  if (state.filterCategory !== "all") {
    list = list.filter(item => (item.market.category || "").toLowerCase().includes(state.filterCategory));
  }
  if (state.filterMinEdge > 0) {
    list = list.filter(item => item.metrics.netEdgePct >= state.filterMinEdge);
  }
  if (state.filterVenues !== "all") {
    const [v1, v2] = state.filterVenues.split("-");
    list = list.filter(item => {
      const pair = [item.market.leg1.venue, item.market.leg2.venue];
      return pair.includes(v1) && pair.includes(v2);
    });
  }
  if (state.searchQuery.trim() !== "") {
    const q = state.searchQuery.toLowerCase();
    list = list.filter(item => item.market.title.toLowerCase().includes(q));
  }
  list.sort((a, b) => {
    switch (state.sortBy) {
      case "apy-desc": return b.metrics.annualizedApy - a.metrics.annualizedApy;
      case "profit-desc": return b.metrics.minDepth - a.metrics.minDepth;
      case "date-asc": return a.market.daysToResolution - b.market.daysToResolution;
      default: return b.metrics.netEdgePct - a.metrics.netEdgePct;
    }
  });
  return list;
}

function renderScannerView() {
  const items = getFilteredAndSortedMarkets();
  const tableBody = document.getElementById("arb-table-body");
  const cardsContainer = document.getElementById("cards-view-container");
  const noResults = document.getElementById("no-results-state");

  if (items.length === 0) {
    if (noResults) noResults.classList.remove("hidden");
    if (tableBody) tableBody.innerHTML = "";
    if (cardsContainer) cardsContainer.innerHTML = "";
    return;
  }
  if (noResults) noResults.classList.add("hidden");

  if (tableBody) {
    tableBody.innerHTML = items.map(({ market, metrics }) => {
      const isPositive = metrics.netEdgePct > 0;
      return `
        <tr data-id="${escapeHtml(market.id)}">
          <td class="col-market">
            <div class="market-meta-box">
              <span class="market-title-link" onclick="openMarketDetail('${escapeHtml(market.id)}')">${escapeHtml(market.title)}</span>
              <div class="market-tags">
                <span class="category-tag">${escapeHtml(market.category)}</span>
                <span class="expiry-tag">rules: ${escapeHtml(market.rules || "same")} · ${market.daysToResolution}d</span>
              </div>
            </div>
          </td>
          <td class="col-leg">
            <div class="leg-display">
              <span class="venue-badge ${market.leg1.venue}">${market.leg1.venue}</span>
              <span class="contract-badge ${market.leg1.side}">${market.leg1.side.toUpperCase()}</span>
              <span class="price-tag">$${market.leg1.price.toFixed(2)}</span>
            </div>
          </td>
          <td class="col-leg">
            <div class="leg-display">
              <span class="venue-badge ${market.leg2.venue}">${market.leg2.venue}</span>
              <span class="contract-badge ${market.leg2.side}">${market.leg2.side.toUpperCase()}</span>
              <span class="price-tag">$${market.leg2.price.toFixed(2)}</span>
            </div>
          </td>
          <td class="col-cost font-mono">$${metrics.grossCost.toFixed(2)}</td>
          <td class="col-fee font-mono text-muted">$${metrics.totalFees.toFixed(4)}</td>
          <td class="col-edge"><span class="edge-badge">${isPositive ? "+" : ""}${metrics.netEdgePct.toFixed(2)}%</span></td>
          <td class="col-apy"><span class="apy-badge">+${metrics.annualizedApy.toFixed(1)}% APY</span></td>
          <td class="col-liquidity">
            <div class="liquidity-box">
              <span class="depth-amount font-mono">${metrics.minDepth.toLocaleString()} cts</span>
              <div class="depth-bar-wrap">
                <div class="depth-bar-fill" style="width: ${Math.min(100, (metrics.minDepth / 800) * 100)}%"></div>
              </div>
            </div>
          </td>
          <td class="col-actions">
            <div class="action-buttons-cell">
              <button class="action-btn-sm primary" onclick="loadMarketIntoCalculator('${escapeHtml(market.id)}')">Calc</button>
              <button class="action-btn-sm secondary" onclick="executeQuickBotTrade('${escapeHtml(market.id)}')">Sim</button>
            </div>
          </td>
        </tr>
      `;
    }).join("");
  }

  if (cardsContainer) {
    cardsContainer.innerHTML = items.map(({ market, metrics }) => `
      <div class="arb-card" data-id="${escapeHtml(market.id)}">
        <div class="card-top">
          <div class="card-market-title" onclick="openMarketDetail('${escapeHtml(market.id)}')">${escapeHtml(market.title)}</div>
          <span class="category-tag">${escapeHtml(market.category)}</span>
        </div>
        <div class="card-legs-container">
          <div class="card-leg-row">
            <div class="leg-display">
              <span class="venue-badge ${market.leg1.venue}">${market.leg1.venue}</span>
              <span class="contract-badge ${market.leg1.side}">${market.leg1.side.toUpperCase()}</span>
            </div>
            <span class="price-tag">$${market.leg1.price.toFixed(2)}</span>
          </div>
          <div class="card-leg-row">
            <div class="leg-display">
              <span class="venue-badge ${market.leg2.venue}">${market.leg2.venue}</span>
              <span class="contract-badge ${market.leg2.side}">${market.leg2.side.toUpperCase()}</span>
            </div>
            <span class="price-tag">$${market.leg2.price.toFixed(2)}</span>
          </div>
        </div>
        <div class="card-metrics-row">
          <div>
            <div class="text-muted" style="font-size:11px;">NET EDGE</div>
            <div class="edge-badge" style="font-size:14px;">+${metrics.netEdgePct.toFixed(2)}%</div>
          </div>
          <div style="text-align:right;">
            <div class="text-muted" style="font-size:11px;">EST. APY</div>
            <div class="apy-badge">+${metrics.annualizedApy.toFixed(1)}%</div>
          </div>
        </div>
        <div class="card-actions-row">
          <button class="action-btn-sm primary" onclick="loadMarketIntoCalculator('${escapeHtml(market.id)}')">Calculate</button>
          <button class="action-btn-sm secondary" onclick="openMarketDetail('${escapeHtml(market.id)}')">Book</button>
          <button class="action-btn-sm secondary" onclick="executeQuickBotTrade('${escapeHtml(market.id)}')">Sim Fill</button>
        </div>
      </div>
    `).join("");
  }
}

function populateCalculatorMarketSelector() {
  const calcSelect = document.getElementById("calc-market-selector");
  if (calcSelect) {
    calcSelect.innerHTML = state.markets.map(m => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.title)}</option>`).join("");
  }
}

async function updateCalculatorDisplay(syncInputsFromMarket = true) {
  const market = state.markets.find(m => m.id === state.calcMarketId) || state.markets[0];
  if (!market) return;

  const selector = document.getElementById("calc-market-selector");
  if (selector && selector.value !== market.id) selector.value = market.id;

  const leg1PriceEl = document.getElementById("calc-leg1-price");
  const leg2PriceEl = document.getElementById("calc-leg2-price");
  const daysInput = document.getElementById("calc-days-input");

  if (syncInputsFromMarket) {
    if (leg1PriceEl) leg1PriceEl.value = market.leg1.price;
    if (leg2PriceEl) leg2PriceEl.value = market.leg2.price;
    if (daysInput) daysInput.value = market.daysToResolution;
  }

  const capital = state.calcCapital;
  const p1 = parseFloat(leg1PriceEl ? leg1PriceEl.value : market.leg1.price) || market.leg1.price;
  const p2 = parseFloat(leg2PriceEl ? leg2PriceEl.value : market.leg2.price) || market.leg2.price;
  const days = Math.max(parseInt(daysInput ? daysInput.value : market.daysToResolution, 10) || 30, 1);

  let result = null;
  try {
    const resp = await fetch("/api/calculate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        capital,
        leg1_price: p1,
        leg2_price: p2,
        leg1_venue: market.leg1.venue,
        leg2_venue: market.leg2.venue,
        days_to_resolution: days,
        market_id: market.id
      })
    });
    if (resp.ok) result = await resp.json();
  } catch (_) {}

  if (!result) {
    const unitEst = p1 + p2 + kalshiQuadraticFee(p1, 100) / 100;
    const contracts = unitEst > 0 ? Math.floor(capital / unitEst) : 0;
    const fee1 = kalshiQuadraticFee(p1, contracts);
    const fee2 = 0.0;
    const l1Cost = contracts * p1;
    const l2Cost = contracts * p2;
    const deployed = l1Cost + l2Cost + fee1 + fee2;
    const gross = contracts * 1.0;
    const net = gross - deployed;
    const roi = deployed > 0 ? (net / deployed) * 100 : 0;
    const apy = roi > 0 ? (Math.pow(1 + roi / 100, 365 / days) - 1) * 100 : 0;
    result = {
      contracts,
      leg1_notional: l1Cost,
      leg2_notional: l2Cost,
      fee1_usd: fee1,
      fee2_usd: fee2,
      slippage_total: 0,
      total_deployed: deployed,
      gross_payout: gross,
      net_profit: net,
      roi_percent: roi,
      apy_percent: apy
    };
  }

  const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };
  const sign = result.net_profit >= 0 ? "+" : "-";
  setText("calc-capital-display", `$${capital.toLocaleString(undefined, { minimumFractionDigits: 2 })}`);
  setText("calc-guaranteed-profit", `${sign}$${Math.abs(result.net_profit).toFixed(2)}`);
  setText("calc-roi-percent", `${result.roi_percent >= 0 ? "+" : ""}${result.roi_percent.toFixed(2)}%`);
  setText("calc-apy-percent", `+${result.apy_percent.toFixed(1)}%`);
  setText("calc-slippage-usd", `$${Number(result.slippage_total || 0).toFixed(2)}`);

  setText("plan-venue-1", market.leg1.venue.toUpperCase());
  setText("plan-shares-1", `${result.contracts.toLocaleString()} contracts`);
  setText("plan-price-1", `$${p1.toFixed(2)}`);
  setText("plan-cost-1", `$${Number(result.leg1_notional).toFixed(2)}`);
  setText("plan-fee-1", `$${Number(result.fee1_usd).toFixed(2)}`);

  setText("plan-venue-2", market.leg2.venue.toUpperCase());
  setText("plan-shares-2", `${result.contracts.toLocaleString()} contracts`);
  setText("plan-price-2", `$${p2.toFixed(2)}`);
  setText("plan-cost-2", `$${Number(result.leg2_notional).toFixed(2)}`);
  setText("plan-fee-2", `$${Number(result.fee2_usd).toFixed(2)}`);

  const formattedPayout = `$${Number(result.gross_payout).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const formattedNet = `${sign}$${Math.abs(result.net_profit).toFixed(2)}`;
  setText("matrix-yes-leg1", formattedPayout);
  setText("matrix-yes-leg2", "$0.00");
  setText("matrix-yes-gross", formattedPayout);
  setText("matrix-yes-net", formattedNet);
  setText("matrix-no-leg1", "$0.00");
  setText("matrix-no-leg2", formattedPayout);
  setText("matrix-no-gross", formattedPayout);
  setText("matrix-no-net", formattedNet);
}

function renderBotPositions() {
  const body = document.getElementById("bot-positions-body");
  if (!body) return;
  if (state.botPositions.length === 0) {
    body.innerHTML = `<tr><td colspan="8" style="text-align:center; color:var(--text-muted); padding:24px;">No active interactive sandbox positions. Click "Sim" on any scanner row to simulate a fill.</td></tr>`;
    return;
  }
  body.innerHTML = state.botPositions.map(pos => `
    <tr>
      <td><strong>${escapeHtml(pos.title)}</strong></td>
      <td><span class="badge-pill active">${escapeHtml(pos.direction)}</span></td>
      <td class="font-mono text-muted">${escapeHtml(pos.entryTime)}</td>
      <td class="font-mono">$${Number(pos.capital).toFixed(2)} (${pos.contracts} pairs)</td>
      <td class="font-mono text-profit">+$${Number(pos.lockedProfit).toFixed(2)}</td>
      <td class="font-mono text-profit">+${Number(pos.roi).toFixed(2)}%</td>
      <td><span class="status-indicator-tag green">${escapeHtml(pos.status)}</span></td>
      <td><button class="action-btn-sm secondary" onclick="closeBotPosition('${escapeHtml(pos.id)}')">Unwind</button></td>
    </tr>
  `).join("");
}

function renderTradeLedger() {
  const body = document.getElementById("history-ledger-body");
  const countEl = document.getElementById("ledger-count");
  if (countEl) countEl.textContent = state.tradeLedger.length;
  if (!body) return;

  body.innerHTML = state.tradeLedger.map(t => {
    const yesLeg = `${(t.yes_venue || "kalshi").toUpperCase()} YES @ $${Number(t.yes_entry || 0).toFixed(3)} (${t.contracts})`;
    const noLeg = `${(t.no_venue || "polymarket").toUpperCase()} NO @ $${Number(t.no_entry || 0).toFixed(3)} (${t.contracts})`;
    const feeSlip = Number(t.total_fees_usd || 0) + Number(t.slippage_usd || 0);
    return `
      <tr>
        <td class="font-mono"><strong>${escapeHtml(t.trade_id)}</strong></td>
        <td>${escapeHtml(t.market_name)}</td>
        <td><span class="font-mono text-muted">${escapeHtml(yesLeg)}</span></td>
        <td><span class="font-mono text-muted">${escapeHtml(noLeg)}</span></td>
        <td class="font-mono">$${Number(t.deployed_capital).toFixed(2)}</td>
        <td class="font-mono">$${Number(t.gross_payout).toFixed(2)}</td>
        <td class="font-mono text-muted">$${feeSlip.toFixed(2)}</td>
        <td class="font-mono text-profit"><strong>+$${Number(t.pnl_usd).toFixed(2)}</strong></td>
        <td class="font-mono text-profit">+${Number(t.roi_pct).toFixed(2)}%</td>
        <td><span class="status-indicator-tag green">${escapeHtml(t.exit_reason)}</span></td>
      </tr>
    `;
  }).join("");
}

// ============================================================================
// WINDOW ACTIONS
// ============================================================================

window.loadMarketIntoCalculator = function(marketId) {
  state.calcMarketId = marketId;
  setViewMode("terminal");
  switchTab("calculator");
  updateCalculatorDisplay(true);
};

window.openMarketDetail = function(marketId) {
  const market = state.markets.find(m => m.id === marketId);
  if (!market) return;
  state.selectedMarketDetail = market;

  const setText = (id, txt) => {
    const el = document.getElementById(id);
    if (el) el.textContent = txt;
  };
  setText("modal-market-title", market.title);
  setText("modal-category-badge", (market.category || "market").toUpperCase());
  setText("modal-market-id", `PAIR-ID: ${market.id}`);
  setText("modal-kalshi-ask", `$${market.leg1.price.toFixed(2)}`);
  setText("modal-kalshi-bid", `$${Math.max(0.01, market.leg1.price - 0.01).toFixed(2)}`);
  setText("modal-kalshi-ask-vol", `${market.leg1.depth} contracts`);
  setText("modal-kalshi-bid-vol", `${market.leg1.depth} contracts`);
  setText("modal-poly-ask", `$${market.leg2.price.toFixed(2)}`);
  setText("modal-poly-bid", `$${Math.max(0.01, market.leg2.price - 0.01).toFixed(2)}`);
  setText("modal-poly-ask-vol", `${market.leg2.depth} contracts`);
  setText("modal-poly-bid-vol", `${market.leg2.depth} contracts`);
  setText("modal-rules-verdict", market.rules || "same");
  setText("modal-rules-reason", market.rulesReason || "Settlement rules verified compatible.");

  const modal = document.getElementById("market-detail-modal");
  if (modal) modal.classList.remove("hidden");
};

window.executeQuickBotTrade = async function(marketId) {
  try {
    const res = await fetch("/api/bot/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ market_id: marketId, capital: state.botMaxSize })
    });
    const json = await res.json();
    if (!res.ok) {
      showToast(json.detail || "Unable to simulate trade", "info");
      return;
    }
    state.botPositions.unshift(json.position);
    renderBotPositions();
    appendBotLog(`SIM ENTRY [${marketId}]: ${json.position.contracts} pairs @ $${json.position.capital.toFixed(2)} (fees $${json.position.fees.toFixed(2)}, locked +$${json.position.lockedProfit.toFixed(2)})`, "trade");
    showToast(`Simulated paper fill on ${json.position.title} (+$${json.position.lockedProfit.toFixed(2)})`);
    playArbChime();
  } catch (_) {
    showToast("Server unavailable for interactive simulation", "info");
  }
};

window.closeBotPosition = async function(positionId) {
  try {
    const res = await fetch("/api/bot/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ market_id: positionId, mode: "early_convergence" })
    });
    if (res.ok) {
      const json = await res.json();
      state.botPositions = state.botPositions.filter(p => p.id !== positionId);
      state.tradeLedger.unshift(json.trade);
      renderBotPositions();
      renderTradeLedger();
      appendBotLog(`SIM UNWIND [${json.trade.trade_id}]: Realized +$${Number(json.trade.pnl_usd).toFixed(2)}`, "success");
      showToast(`Unwound simulated position (+$${Number(json.trade.pnl_usd).toFixed(2)})`);
    }
  } catch (_) {}
};

function startClock() {
  setInterval(() => {
    const el = document.getElementById("ticker-clock");
    if (el) el.textContent = `UTC ${new Date().toISOString().substring(11, 19)}`;
  }, 1000);
}

// ============================================================================
// INITIALIZATION
// ============================================================================

document.addEventListener("DOMContentLoaded", async () => {
  await hydrateFromServer();
  connectWebSocket();
  startClock();

  const params = new URLSearchParams(window.location.search);
  if (params.get("view") === "terminal") {
    setViewMode("terminal");
    const tab = params.get("tab");
    if (tab) switchTab(tab);
  }

  const toggleViewBtn = document.getElementById("toggle-view-btn");
  if (toggleViewBtn) {
    toggleViewBtn.addEventListener("click", () => {
      setViewMode(state.viewMode === "landing" ? "terminal" : "landing");
    });
  }
  const heroLaunchBtn = document.getElementById("hero-launch-terminal-btn");
  if (heroLaunchBtn) heroLaunchBtn.addEventListener("click", () => setViewMode("terminal"));
  document.querySelectorAll(".launch-terminal-trigger").forEach(btn => {
    btn.addEventListener("click", () => setViewMode("terminal"));
  });

  const brandLink = document.getElementById("brand-home-link");
  if (brandLink) brandLink.addEventListener("click", (e) => { e.preventDefault(); setViewMode("landing"); });

  const footerLink = document.getElementById("footer-link-home");
  if (footerLink) footerLink.addEventListener("click", (e) => { e.preventDefault(); setViewMode("landing"); window.scrollTo({ top: 0, behavior: "smooth" }); });

  const srcDemoBtn = document.getElementById("source-demo-btn");
  const srcLiveBtn = document.getElementById("source-live-btn");
  if (srcDemoBtn && srcLiveBtn) {
    srcDemoBtn.addEventListener("click", () => {
      srcDemoBtn.classList.add("active");
      srcLiveBtn.classList.remove("active");
      state.dataSource = "demo";
      loadSimulationData();
    });
    srcLiveBtn.addEventListener("click", () => {
      srcLiveBtn.classList.add("active");
      srcDemoBtn.classList.remove("active");
      state.dataSource = "live";
      fetchLivePolymarketData();
    });
  }

  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  document.querySelectorAll("#category-filter-group .pill-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#category-filter-group .pill-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.filterCategory = btn.dataset.category;
      renderScannerView();
    });
  });

  document.querySelectorAll("#edge-filter-group .filter-opt-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#edge-filter-group .filter-opt-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.filterMinEdge = parseFloat(btn.dataset.minEdge);
      renderScannerView();
    });
  });

  const venueSel = document.getElementById("venue-select");
  if (venueSel) venueSel.addEventListener("change", (e) => { state.filterVenues = e.target.value; renderScannerView(); });
  const sortSel = document.getElementById("sort-select");
  if (sortSel) sortSel.addEventListener("change", (e) => { state.sortBy = e.target.value; renderScannerView(); });

  const searchInput = document.getElementById("market-search-input");
  const clearBtn = document.getElementById("search-clear-btn");
  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      state.searchQuery = e.target.value;
      if (clearBtn) clearBtn.classList.toggle("hidden", state.searchQuery === "");
      renderScannerView();
    });
  }
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      state.searchQuery = "";
      if (searchInput) searchInput.value = "";
      clearBtn.classList.add("hidden");
      renderScannerView();
    });
  }

  const resetFiltersBtn = document.getElementById("reset-filters-btn");
  if (resetFiltersBtn) {
    resetFiltersBtn.addEventListener("click", () => {
      state.filterCategory = "all";
      state.filterMinEdge = 0;
      state.searchQuery = "";
      if (searchInput) searchInput.value = "";
      renderScannerView();
    });
  }

  const btnTable = document.getElementById("view-mode-table");
  const btnCards = document.getElementById("view-mode-cards");
  const tableCont = document.getElementById("table-view-container");
  const cardsCont = document.getElementById("cards-view-container");
  if (btnTable && btnCards) {
    btnTable.addEventListener("click", () => {
      btnTable.classList.add("active");
      btnCards.classList.remove("active");
      tableCont.classList.remove("hidden");
      cardsCont.classList.add("hidden");
    });
    btnCards.addEventListener("click", () => {
      btnCards.classList.add("active");
      btnTable.classList.remove("active");
      tableCont.classList.add("hidden");
      cardsCont.classList.remove("hidden");
    });
  }

  const soundBtn = document.getElementById("sound-toggle-btn");
  const soundOn = document.getElementById("sound-icon-on");
  const soundOff = document.getElementById("sound-icon-off");
  if (soundBtn) {
    soundBtn.addEventListener("click", () => {
      state.audioAlertsEnabled = !state.audioAlertsEnabled;
      if (soundOn) soundOn.classList.toggle("hidden", !state.audioAlertsEnabled);
      if (soundOff) soundOff.classList.toggle("hidden", state.audioAlertsEnabled);
      showToast(state.audioAlertsEnabled ? "Audio alerts enabled" : "Audio alerts muted", "info");
    });
  }

  const streamToggle = document.getElementById("live-stream-toggle");
  if (streamToggle) {
    streamToggle.addEventListener("change", (e) => {
      state.liveStreamActive = e.target.checked;
      showToast(state.liveStreamActive ? "WebSocket sim stream active" : "Stream paused", "info");
    });
  }

  const calcSelect = document.getElementById("calc-market-selector");
  if (calcSelect) {
    calcSelect.addEventListener("change", (e) => {
      state.calcMarketId = e.target.value;
      updateCalculatorDisplay(true);
    });
  }

  const capInput = document.getElementById("calc-capital-input");
  if (capInput) {
    capInput.addEventListener("input", (e) => {
      state.calcCapital = Math.max(10, parseFloat(e.target.value) || 0);
      updateCalculatorDisplay(false);
    });
  }

  document.querySelectorAll("#calc-quick-pills .pill-btn-sm").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#calc-quick-pills .pill-btn-sm").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      state.calcCapital = parseFloat(btn.dataset.amount);
      if (capInput) capInput.value = state.calcCapital;
      updateCalculatorDisplay(false);
    });
  });

  const recalcBtn = document.getElementById("calc-recalculate-btn");
  if (recalcBtn) {
    recalcBtn.addEventListener("click", () => {
      updateCalculatorDisplay(false);
      showToast("Recalculated L2 sizing & Kalshi quadratic fees");
    });
  }

  const sendToBotBtn = document.getElementById("send-to-bot-btn");
  if (sendToBotBtn) {
    sendToBotBtn.addEventListener("click", async () => {
      await executeQuickBotTrade(state.calcMarketId);
      switchTab("bot");
    });
  }

  const botRunBtn = document.getElementById("bot-toggle-run-btn");
  if (botRunBtn) {
    botRunBtn.addEventListener("click", async () => {
      try {
        const res = await fetch("/api/bot/toggle", { method: "POST" });
        const json = await res.json();
        state.botRunning = Boolean(json.bot_running);
      } catch (_) {
        state.botRunning = !state.botRunning;
      }
      botRunBtn.textContent = state.botRunning ? "⏸ Pause Bot" : "▶ Resume Bot";
      botRunBtn.className = state.botRunning ? "action-btn warning" : "action-btn success";
      showToast(state.botRunning ? "Paper bot resumed" : "Paper bot paused", "info");
    });
  }

  const minEdgeSlider = document.getElementById("bot-min-edge-slider");
  const minEdgeDisplay = document.getElementById("bot-min-edge-display");
  if (minEdgeSlider) {
    minEdgeSlider.addEventListener("input", (e) => {
      state.botMinEdge = parseFloat(e.target.value);
      if (minEdgeDisplay) minEdgeDisplay.textContent = `${state.botMinEdge.toFixed(1)}%`;
    });
  }
  const maxSizeInput = document.getElementById("bot-max-size-input");
  const maxSizeDisplay = document.getElementById("bot-max-size-display");
  if (maxSizeInput) {
    maxSizeInput.addEventListener("input", (e) => {
      state.botMaxSize = parseFloat(e.target.value) || 450;
      if (maxSizeDisplay) maxSizeDisplay.textContent = `$${state.botMaxSize}`;
    });
  }
  const tpSlider = document.getElementById("bot-take-profit-slider");
  const tpDisplay = document.getElementById("bot-take-profit-display");
  if (tpSlider) {
    tpSlider.addEventListener("input", (e) => {
      state.botTakeProfit = parseFloat(e.target.value);
      if (tpDisplay) tpDisplay.textContent = `${state.botTakeProfit.toFixed(1)}%`;
    });
  }

  const saveCfgBtn = document.getElementById("save-bot-config-btn");
  if (saveCfgBtn) {
    saveCfgBtn.addEventListener("click", async () => {
      try {
        await fetch("/api/bot/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            min_entry_edge_pct: state.botMinEdge,
            max_position_usd: state.botMaxSize,
            take_profit_pct: state.botTakeProfit
          })
        });
        showToast("Bot parameters synced to FastAPI server");
      } catch (_) {
        showToast("Updated local bot parameters", "info");
      }
    });
  }

  const clearLogsBtn = document.getElementById("clear-logs-btn");
  if (clearLogsBtn) {
    clearLogsBtn.addEventListener("click", () => {
      const term = document.getElementById("bot-terminal-logs");
      if (term) term.innerHTML = "";
    });
  }

  const closeDetailBtn = document.getElementById("close-detail-modal-btn");
  if (closeDetailBtn) {
    closeDetailBtn.addEventListener("click", () => {
      const modal = document.getElementById("market-detail-modal");
      if (modal) modal.classList.add("hidden");
    });
  }
  const modalOpenCalc = document.getElementById("modal-open-calc-btn");
  if (modalOpenCalc) {
    modalOpenCalc.addEventListener("click", () => {
      document.getElementById("market-detail-modal").classList.add("hidden");
      if (state.selectedMarketDetail) loadMarketIntoCalculator(state.selectedMarketDetail.id);
    });
  }
  const modalExecBot = document.getElementById("modal-exec-bot-btn");
  if (modalExecBot) {
    modalExecBot.addEventListener("click", async () => {
      document.getElementById("market-detail-modal").classList.add("hidden");
      if (state.selectedMarketDetail) {
        await executeQuickBotTrade(state.selectedMarketDetail.id);
        switchTab("bot");
      }
    });
  }

  const exportBtn = document.getElementById("export-csv-btn");
  if (exportBtn) {
    exportBtn.addEventListener("click", () => {
      const rows = [
        ["Trade ID", "Market", "Contracts", "Deployed USD", "Gross Payout USD", "Fees USD", "Slippage USD", "Net PnL USD", "ROI %", "Exit Reason"],
        ...state.tradeLedger.map(t => [
          t.trade_id, `"${t.market_name}"`, t.contracts, t.deployed_capital,
          t.gross_payout, t.total_fees_usd, t.slippage_usd, t.pnl_usd, t.roi_pct, t.exit_reason
        ])
      ];
      const uri = encodeURI("data:text/csv;charset=utf-8," + rows.map(r => r.join(",")).join("\n"));
      const a = document.createElement("a");
      a.href = uri;
      a.download = `arbitragex_1k_paper_replay_ledger.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      showToast("Exported $1,000 paper replay ledger CSV");
    });
  }

  document.querySelectorAll(".faq-question").forEach(q => {
    q.addEventListener("click", () => q.parentElement.classList.toggle("open"));
  });
});
