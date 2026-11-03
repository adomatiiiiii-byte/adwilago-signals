"use strict";

let chart;
let candleSeries;
let maSeries;
let rsiChart;
let rsiSeries;
const DISPLAY_TIME_ZONE = "Africa/Kampala";
const USDJPY_PRICE_FORMAT = { type: "price", precision: 3, minMove: 0.001 };
const LATEST_RANGE_TOLERANCE = 1.5;
let mainChartDataLoaded = false;
let rsiChartDataLoaded = false;
let mainDataLength = 0;
let rsiDataLength = 0;
let mainFollowsLatest = true;
let rsiFollowsLatest = true;
let suppressMainRangeTracking = false;
let suppressRsiRangeTracking = false;

function formatKampalaTimestamp(timestamp, options = {}) {
    if (!timestamp) return "—";
    return new Intl.DateTimeFormat(undefined, {
        ...options,
        timeZone: DISPLAY_TIME_ZONE,
    }).format(new Date(timestamp));
}

function formatChartTime(seconds) {
    return formatKampalaTimestamp(Number(seconds) * 1000, {
        month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
    });
}

function isAtLatest(range, dataLength) {
    return !range || range.to >= dataLength - 1 - LATEST_RANGE_TOLERANCE;
}

function trackChartRange(timeScale, dataLength, updateFollowing, isSuppressed) {
    if (isSuppressed() || dataLength <= 0) return;
    updateFollowing(isAtLatest(timeScale.getVisibleLogicalRange(), dataLength));
}

function formatPrice(value) {
    return typeof value === "number" && Number.isFinite(value) ? value.toFixed(3) : "—";
}

function updateClock() {
    const currentTime = new Intl.DateTimeFormat(
        undefined,
        { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: DISPLAY_TIME_ZONE },
    ).format(new Date());
    const headerClock = document.getElementById("current-time");
    const fixedClock = document.getElementById("fixed-kampala-time");
    if (headerClock) headerClock.textContent = currentTime;
    if (fixedClock) fixedClock.textContent = currentTime;
}

function initializeClock() {
    updateClock();
    window.setInterval(updateClock, 1000);
}

function createChart() {
    const container = document.getElementById("chart");
    if (!container || !window.LightweightCharts) return;
    chart = LightweightCharts.createChart(container, {
        autoSize: true,
        layout: { background: { color: "#111827" }, textColor: "#8490a6" },
        grid: { vertLines: { color: "#1d293b" }, horzLines: { color: "#1d293b" } },
        rightPriceScale: { borderColor: "#263449" },
        timeScale: { borderColor: "#263449", timeVisible: true, secondsVisible: false, tickMarkFormatter: formatChartTime },
    });
    candleSeries = chart.addCandlestickSeries({
        upColor: "#45d6b0", downColor: "#e47c85", borderVisible: false,
        wickUpColor: "#45d6b0", wickDownColor: "#e47c85",
        priceFormat: USDJPY_PRICE_FORMAT,
    });
    maSeries = chart.addLineSeries({ color: "#f5c76a", lineWidth: 2, title: "MA 75", priceFormat: USDJPY_PRICE_FORMAT });
    chart.subscribeCrosshairMove((param) => {
        const tooltip = document.getElementById("chart-tooltip");
        if (!tooltip || !param.time || !param.point || !candleSeries) {
            if (tooltip) tooltip.hidden = true;
            return;
        }
        const candle = param.seriesData.get(candleSeries);
        if (!candle) {
            tooltip.hidden = true;
            return;
        }
        tooltip.innerHTML = `<strong>${formatChartTime(param.time)} Kampala</strong><br>O ${formatPrice(candle.open)} · H ${formatPrice(candle.high)} · L ${formatPrice(candle.low)} · C ${formatPrice(candle.close)}`;
        tooltip.hidden = false;
        tooltip.style.left = `${Math.min(param.point.x + 12, container.clientWidth - tooltip.offsetWidth - 8)}px`;
        tooltip.style.top = `${Math.max(param.point.y - tooltip.offsetHeight - 12, 8)}px`;
    });

    const rsiContainer = document.getElementById("rsi-chart");
    if (rsiContainer) {
        rsiChart = LightweightCharts.createChart(rsiContainer, {
            autoSize: true,
            layout: { background: { color: "#111827" }, textColor: "#8490a6" },
            grid: { vertLines: { color: "#1d293b" }, horzLines: { color: "#1d293b" } },
            rightPriceScale: { borderColor: "#263449", scaleMargins: { top: 0.08, bottom: 0.08 } },
            timeScale: { borderColor: "#263449", timeVisible: true, secondsVisible: false, tickMarkFormatter: formatChartTime },
        });
        rsiSeries = rsiChart.addLineSeries({ color: "#45d6b0", lineWidth: 2, title: "RSI 7" });
    }

    chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
        trackChartRange(
            chart.timeScale(),
            mainDataLength,
            (following) => { mainFollowsLatest = following; },
            () => suppressMainRangeTracking,
        );
    });
    if (rsiChart) {
        rsiChart.timeScale().subscribeVisibleLogicalRangeChange(() => {
            trackChartRange(
                rsiChart.timeScale(),
                rsiDataLength,
                (following) => { rsiFollowsLatest = following; },
                () => suppressRsiRangeTracking,
            );
        });
    }
}

function formatTimestamp(timestamp) {
    if (!timestamp) return "—";
    return new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium", timeStyle: "short", timeZone: DISPLAY_TIME_ZONE,
    }).format(new Date(timestamp));
}

function formatUtcTimestamp(timestamp) {
    if (!timestamp) return "--";
    return new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium", timeStyle: "short", timeZone: "UTC",
    }).format(new Date(timestamp));
}

function setConnectionState(data) {
    const connected = data.status === "connected";
    const error = data.rest_status === "error" || data.status === "error";
    const stale = connected && data.rest_freshness === "stale";
    const label = error ? "Error" : stale ? "Stale" : connected ? "Connected" : "Disconnected";
    const badge = document.getElementById("connection-badge");
    badge.textContent = `REST ${label.toLowerCase()}`;
    badge.className = `badge ${connected && !stale ? "badge-success" : error ? "badge-error" : "badge-warning"}`;
    document.getElementById("connection-detail").innerHTML = `<i class="status-dot ${connected && !stale ? "" : "muted"}"></i> ${label}`;
    setWebSocketState(data.websocket_status || "disconnected", data.websocket_error);
    document.getElementById("app-status-text").textContent = `Market data ${label.toLowerCase()}`;
    document.getElementById("app-status-dot").className = `status-dot ${connected ? "" : "muted"}`;
    document.getElementById("summary-status").textContent = label;
    document.getElementById("summary-status-detail").textContent = connected ? "Exness market feed active" : "Market feed unavailable";
    document.getElementById("chart-badge").textContent = connected ? "Live Exness candles" : label;

    const notice = document.getElementById("connection-notice");
    notice.querySelector("p").textContent = stale
        ? "Candle data is stale. The dashboard is retaining the last official Exness candles until a fresh REST update succeeds."
        : connected
        ? "Candles below are official completed M5 data from Exness. Tick prices update separately when the stream is connected."
        : (data.rest_error || data.error || "Live market data is not available. No placeholder values are being displayed.");
    notice.className = `notice ${connected ? "notice-success" : ""}`;
    document.getElementById("chart-disclaimer").textContent = connected
        ? "Genuine USDJPY M5 candles · Exness"
        : "No genuine market data available · chart intentionally empty";
}

function setWebSocketState(status, error) {
    const connected = status === "connected";
    const label = connected ? "Connected" : status === "connecting" ? "Connecting" : "Disconnected";
    const badge = document.getElementById("websocket-badge");
    badge.textContent = `WS ${label.toLowerCase()}`;
    badge.className = `badge ${connected ? "badge-success" : "badge-warning"}`;
    document.getElementById("websocket-detail").innerHTML = `<i class="status-dot ${connected ? "" : "muted"}"></i> ${error || label}`;
}

function applyTick(data) {
    if (typeof data.bid !== "number") return;
    document.getElementById("current-price").textContent = formatPrice(data.bid);
    document.getElementById("current-price").className = "";
    document.getElementById("last-tick").textContent = formatTimestamp(data.timestamp);
    document.getElementById("last-tick").className = "";
    document.getElementById("freshness").textContent = `Live tick: ${formatTimestamp(data.timestamp)}`;
    setWebSocketState("connected");
}

function formatIndicatorValue(value) {
    return formatPrice(value);
}

function formatRsiValue(value) {
    return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "—";
}

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function updateTodaysSignals(data) {
    const records = Array.isArray(data.today_signals) ? data.today_signals : [];
    const body = document.getElementById("today-signals-body");
    const count = document.getElementById("today-signals-count");
    if (!body || !count) return;
    count.textContent = `${records.length} signal${records.length === 1 ? "" : "s"}`;
    if (records.length === 0) {
        body.innerHTML = `<tr><td colspan="6" class="empty-table"><span>⌁</span><strong>No signals in today's session</strong><small>Qualifying signals will appear here from 07:00 AM to 06:00 PM Kampala time.</small></td></tr>`;
        return;
    }
    body.innerHTML = records.map((record) => {
        const bullish = record.direction === "bullish";
        return `<tr class="signal-history-row ${bullish ? "row-bullish" : "row-bearish"}">
            <td><span class="direction-label ${bullish ? "direction-bullish" : "direction-bearish"}">${escapeHtml(record.direction).toUpperCase()}</span></td>
            <td>${escapeHtml(formatTimestamp(record.candle_timestamp))} Kampala</td>
            <td>${escapeHtml(record.market)}</td>
            <td>M5</td>
            <td>${escapeHtml(record.strategy)}</td>
            <td>${formatPrice(record.price)}</td>
        </tr>`;
    }).join("");
}

function updateSignalPresentation(data) {
    const signal = data.current_signal;
    const badge = document.getElementById("current-signal-badge");
    const state = document.getElementById("current-signal-state");
    const icon = document.getElementById("current-signal-icon");
    const title = document.getElementById("current-signal-title");
    const description = document.getElementById("current-signal-description");
    const details = document.getElementById("current-signal-details");
    const body = document.getElementById("signal-history-body");
    const count = document.getElementById("signal-history-count");
    if (!badge || !state || !icon || !title || !description || !details || !body || !count) return;
    updateTodaysSignals(data);

    if (!signal) {
        badge.textContent = "No active signal";
        badge.className = "badge badge-neutral";
        state.className = "signal-state";
        icon.textContent = "—";
        title.textContent = "NO ACTIVE SIGNAL";
        description.textContent = "The latest evaluated completed candle did not produce a confirmed signal.";
        details.hidden = true;
        details.innerHTML = "";
    } else {
        const bullish = signal.direction === "bullish";
        const direction = bullish ? "BULLISH" : "BEARISH";
        badge.textContent = direction;
        badge.className = `badge ${bullish ? "badge-bullish" : "badge-bearish"}`;
        state.className = `signal-state signal-${signal.direction}`;
        icon.textContent = bullish ? "↑" : "↓";
        title.textContent = direction;
        description.textContent = `${signal.strategy} confirmed on ${formatTimestamp(signal.candle_timestamp)} Kampala time.`;
        details.innerHTML = `
            <div><span>Market / timeframe</span><strong>${escapeHtml(signal.market)} · ${escapeHtml(signal.timeframe)}</strong></div>
            <div><span>Completed candle close</span><strong>${formatPrice(signal.price)}</strong></div>
            <div class="signal-condition-list"><span>Confirmed conditions</span><ul>${(signal.conditions || []).map((condition) => `<li>${escapeHtml(condition.label)}: ${formatPrice(condition.actual)} ${escapeHtml(condition.operator)} ${formatPrice(condition.reference)}</li>`).join("")}</ul></div>`;
        details.hidden = false;
    }

    const history = Array.isArray(data.signal_history) ? data.signal_history : [];
    count.textContent = `${history.length} signal${history.length === 1 ? "" : "s"}`;
    if (history.length === 0) {
        body.innerHTML = `<tr><td colspan="7" class="empty-table"><span>⌁</span><strong>No signals generated</strong><small>Only confirmed signals are shown here.</small></td></tr>`;
        return;
    }
    body.innerHTML = history.map((record) => {
        const bullish = record.direction === "bullish";
        return `<tr class="signal-history-row ${bullish ? "row-bullish" : "row-bearish"}">
            <td><span class="direction-label ${bullish ? "direction-bullish" : "direction-bearish"}">${escapeHtml(record.direction).toUpperCase()}</span></td>
            <td>${escapeHtml(formatTimestamp(record.candle_timestamp))} Kampala</td>
            <td>${formatPrice(record.price)}</td>
            <td>${escapeHtml(record.strategy)}</td>
            <td>${escapeHtml(record.market)}</td>
            <td>${escapeHtml(record.status)}</td>
            <td><button type="button" class="signal-evidence-button" data-signal-id="${Number(record.id)}">Inspect</button></td>
        </tr>`;
    }).join("");
}

function renderEvidenceUnavailable(message) {
    const status = document.getElementById("evidence-status");
    const content = document.getElementById("signal-evidence-content");
    if (!status || !content) return;
    status.textContent = "Unavailable";
    status.className = "badge badge-warning";
    content.innerHTML = `<p class="evidence-placeholder">${escapeHtml(message)}</p>`;
}

function renderSignalEvidence(payload) {
    const status = document.getElementById("evidence-status");
    const content = document.getElementById("signal-evidence-content");
    if (!status || !content) return;
    if (!payload.available || !payload.evidence) {
        renderEvidenceUnavailable(payload.message || "Historical evidence unavailable");
        return;
    }
    const evidence = payload.evidence;
    const signal = payload.signal || {};
    const bullish = signal.direction === "bullish";
    const direction = bullish ? "BULLISH" : "BEARISH";
    const conditions = bullish
        ? [
            `Current close ${formatPrice(evidence.current_close)} > previous open ${formatPrice(evidence.previous_open)}`,
            `Current open ${formatPrice(evidence.current_open)} > previous SMA75 ${formatPrice(evidence.previous_ma75)}`,
            `Previous RSI7 rounded ${Number(evidence.previous_rsi7_rounded).toFixed(2)} < 40`,
        ]
        : [
            `Current close ${formatPrice(evidence.current_close)} < previous open ${formatPrice(evidence.previous_open)}`,
            `Current open ${formatPrice(evidence.current_open)} < previous SMA75 ${formatPrice(evidence.previous_ma75)}`,
            `Previous RSI7 rounded ${Number(evidence.previous_rsi7_rounded).toFixed(2)} > 60`,
        ];
    const passed = [evidence.condition_1_passed, evidence.condition_2_passed, evidence.condition_3_passed];
    status.textContent = `${direction} evidence`;
    status.className = `badge ${bullish ? "badge-bullish" : "badge-bearish"}`;
    content.innerHTML = `
        <div class="evidence-summary"><div><span>Signal direction</span><strong>${direction}</strong></div><div><span>Signal timestamp</span><strong>${escapeHtml(formatTimestamp(signal.candle_timestamp))} Kampala</strong></div><div><span>Evaluation timestamp</span><strong>${escapeHtml(formatUtcTimestamp(evidence.evaluation_timestamp))} UTC</strong></div></div>
        <div class="evidence-columns">
            <div><h4>Current completed candle</h4><p>Timestamp: ${escapeHtml(formatTimestamp(evidence.current_candle_timestamp))} Kampala</p><p>O ${formatPrice(evidence.current_open)} · H ${formatPrice(evidence.current_high)} · L ${formatPrice(evidence.current_low)} · C ${formatPrice(evidence.current_close)}</p></div>
            <div><h4>Previous completed candle</h4><p>Timestamp: ${escapeHtml(formatTimestamp(evidence.previous_candle_timestamp))} Kampala</p><p>O ${formatPrice(evidence.previous_open)} · H ${formatPrice(evidence.previous_high)} · L ${formatPrice(evidence.previous_low)} · C ${formatPrice(evidence.previous_close)}</p></div>
            <div><h4>Previous candle indicators</h4><p>SMA75: ${formatPrice(evidence.previous_ma75)}</p><p>RSI7 raw: ${escapeHtml(String(evidence.previous_rsi7_raw))}</p><p>RSI7 rounded: ${Number(evidence.previous_rsi7_rounded).toFixed(2)}</p></div>
        </div>
        <div class="evidence-conditions"><h4>${direction} conditions</h4><ol>${conditions.map((condition, index) => `<li><span>${escapeHtml(condition)}</span><strong class="${passed[index] ? "condition-pass" : "condition-fail"}">${passed[index] ? "PASSED" : "FAILED"}</strong></li>`).join("")}</ol></div>`;
}

let evidenceRequestNumber = 0;
async function loadSignalEvidence(signalId) {
    const status = document.getElementById("evidence-status");
    const content = document.getElementById("signal-evidence-content");
    if (!status || !content) return;
    const requestNumber = ++evidenceRequestNumber;
    status.textContent = "Loading...";
    status.className = "badge badge-neutral";
    content.innerHTML = "<p class=\"evidence-placeholder\">Loading saved evidence...</p>";
    try {
        const response = await fetch(`/api/signals/${encodeURIComponent(signalId)}/evidence`, { cache: "no-store" });
        const payload = await response.json();
        if (requestNumber !== evidenceRequestNumber) return;
        if (!response.ok) throw new Error(payload.detail || "Evidence could not be retrieved.");
        renderSignalEvidence(payload);
    } catch (error) {
        if (requestNumber !== evidenceRequestNumber) return;
        status.textContent = "Error";
        status.className = "badge badge-error";
        content.innerHTML = `<p class="evidence-placeholder">${escapeHtml(error.message || "Evidence could not be retrieved.")}</p>`;
    }
}

function initializeEvidenceSelection() {
    const body = document.getElementById("signal-history-body");
    if (!body) return;
    body.addEventListener("click", (event) => {
        const button = event.target.closest(".signal-evidence-button");
        if (button) loadSignalEvidence(button.dataset.signalId);
    });
}

function updateMainChartData(candles, points) {
    if (!chart || !candleSeries) return;
    const firstLoad = !mainChartDataLoaded;
    const shouldFollow = firstLoad || mainFollowsLatest;
    mainDataLength = candles.length;
    suppressMainRangeTracking = true;
    candleSeries.setData(candles);
    maSeries.setData(points.filter((point) => point.ma !== null).map((point) => ({ time: point.time, value: point.ma })));
    suppressMainRangeTracking = false;

    if (firstLoad) {
        chart.timeScale().fitContent();
        mainFollowsLatest = true;
        mainChartDataLoaded = true;
    } else if (shouldFollow) {
        chart.timeScale().scrollToRealTime();
    }
}

function updateRsiChartData(points) {
    if (!rsiChart || !rsiSeries) return;
    const rsiPoints = points.filter((point) => point.rsi !== null);
    const firstLoad = !rsiChartDataLoaded && rsiPoints.length > 0;
    const shouldFollow = firstLoad || rsiFollowsLatest;
    rsiDataLength = rsiPoints.length;
    suppressRsiRangeTracking = true;
    rsiSeries.setData(rsiPoints.map((point) => ({ time: point.time, value: point.rsi })));
    suppressRsiRangeTracking = false;

    if (firstLoad) {
        rsiChart.timeScale().fitContent();
        rsiFollowsLatest = true;
        rsiChartDataLoaded = true;
    } else if (shouldFollow && rsiPoints.length > 0) {
        rsiChart.timeScale().scrollToRealTime();
    }
}

function updateIndicatorCharts(data) {
    const points = Array.isArray(data.indicators) ? data.indicators : [];
    if (rsiSeries) updateRsiChartData(points);
    const latest = data.latest_indicators || {};
    document.getElementById("latest-ma").textContent = formatIndicatorValue(latest.ma);
    document.getElementById("latest-rsi").textContent = formatRsiValue(latest.rsi);
}

function applyMarketData(data) {
    setConnectionState(data);
    updateSignalPresentation(data);
    if (data.status !== "connected") return;
    if (typeof data.price === "number") {
        document.getElementById("current-price").textContent = formatPrice(data.price);
        document.getElementById("current-price").className = "";
    }
    document.getElementById("last-candle").textContent = formatTimestamp(data.latest_candle_timestamp);
    document.getElementById("last-candle").className = "";
    document.getElementById("freshness").textContent = `Last candle update: ${formatTimestamp(data.last_successful_update)} (${data.price_source.replaceAll("_", " ")})`;
    if (candleSeries && Array.isArray(data.chart_candles)) {
        updateMainChartData(data.chart_candles, Array.isArray(data.indicators) ? data.indicators : []);
    }
    updateIndicatorCharts(data);
}

async function loadMarketData() {
    try {
        const response = await fetch("/api/market-data", { cache: "no-store" });
        applyMarketData(await response.json());
    } catch (error) {
        setConnectionState({ status: "error", error: "The dashboard could not reach its market-data endpoint." });
    }
}

async function loadSignalData() {
    try {
        const response = await fetch("/api/signals", { cache: "no-store" });
        updateSignalPresentation(await response.json());
    } catch (error) {
        // Market-data updates remain the source of truth when this read-only poll is unavailable.
    }
}

function connectDashboardStream() {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const stream = new WebSocket(`${protocol}://${window.location.host}/ws/market-data`);
    stream.onopen = () => setWebSocketState("connecting");
    stream.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === "tick") applyTick(data);
        else applyMarketData(data);
    };
    stream.onerror = () => setWebSocketState("disconnected", "Stream error");
    stream.onclose = () => {
        setWebSocketState("disconnected", "Reconnecting…");
        window.setTimeout(connectDashboardStream, 2000);
    };
}

initializeClock();
createChart();
initializeEvidenceSelection();
loadMarketData();
loadSignalData();
connectDashboardStream();
setInterval(loadMarketData, 15000);
setInterval(loadSignalData, 15000);
