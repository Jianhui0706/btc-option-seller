import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import vm from "node:vm";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");

test("dashboard navigation promotes TradingView setup beside the teacher workflow", () => {
  const labels = ["仓位", "TV 接入", "策略信号", "信号日志"];
  const routes = ["/", "/tradingview", "/signals", "/signal-log", "/returns"];
  const pages = ["positions", "tradingview", "active-signals", "signal-log"];

  const positions = labels.map((label) => html.indexOf(`<span>${label}</span>`));
  assert.ok(positions.every((position) => position >= 0));
  assert.deepEqual([...positions].sort((a, b) => a - b), positions);
  routes.slice(0, 4).forEach((route) => assert.match(html, new RegExp(`navigateTo\\('${route.replace("/", "\\/")}\\'\\)`)));
  assert.match(html, /view==="history"\?"\/returns":"\/"/);
  pages.forEach((page) => assert.match(html, new RegExp(`id="page-${page}"`)));
});

test("shared overview appears before the primary switcher", () => {
  const hero = html.indexOf('class="dashboard-hero"');
  const metrics = html.indexOf('class="global-metrics"');
  const tabs = html.indexOf('class="main-nav reference-tabs"');
  assert.ok(hero >= 0 && metrics > hero && tabs > metrics);
  assert.match(html, /class="page-switcher"/);
});

test("shared shell keeps five core metrics visible across all tabs", () => {
  assert.match(html, /class="dashboard-hero"/);
  assert.match(html, /class="global-metrics"/);
  ["gEquity", "gAvailable", "gPositions", "gSignals", "gTodayPnl"].forEach(
    (id) => assert.match(html, new RegExp(`id="${id}"`)),
  );
  assert.match(html, /function renderGlobalMetrics\(status,acc,trades,signals,performance\)/);
  assert.match(html, /renderGlobalMetrics\(status,acc,trades,signals,performance\)/);
});

test("current position count excludes empty interrupted workflow records", () => {
  assert.match(html, /function managedPositions\(items\)/);
  assert.match(html, /Number\(t\.parent\?\.qty_total\|\|0\)>0/);
  assert.match(html, /中断流程等待系统清理/);
});

test("each primary page owns the correct operational content", () => {
  const positions = html.split('id="page-positions"')[1].split('id="page-tradingview"')[0];
  const tradingview = html.split('id="page-tradingview"')[1].split('id="page-active-signals"')[0];
  const activeSignals = html.split('id="page-active-signals"')[1].split('id="page-signal-log"')[0];
  const signalLog = html.split('id="page-signal-log"')[1].split('id="forceModal"')[0];
  const positionLog = positions;

  assert.match(positions, /id="activeTrades"/);
  assert.match(positions, /id="positionsTable"/);
  assert.match(tradingview, /id="tvSetupWebhook"/);
  assert.match(tradingview, /id="tvAlertMessage"/);
  assert.match(tradingview, /四步完成接入/);
  assert.match(activeSignals, /id="strategyRoster"/);
  assert.match(activeSignals, /id="strategyInspector"/);
  assert.doesNotMatch(activeSignals, /id="journeySummary"/);
  assert.doesNotMatch(activeSignals, /id="tvSignals"/);
  assert.match(signalLog, /id="tvSignals"/);
  assert.match(signalLog, /id="webhookUrl"/);
  assert.match(positionLog, /id="pnlCurveCanvas"/);
  assert.match(positionLog, /id="eventTimeline"/);
  assert.match(positionLog, /id="tradesTable"/);
  assert.match(positionLog, /id="hedgesTable"/);
  assert.match(positionLog, /id="hedgeLedgerSummary"/);
  assert.doesNotMatch(html, /hedgeLedgerTable/);
  assert.doesNotMatch(positionLog, /对冲周期汇总/);
  assert.match(positionLog, /id="hedgeExecutionTable"/);
  assert.match(positionLog, /账户级对冲账本/);
  assert.match(positionLog, /执行阶段/);
  assert.match(positionLog, /Deribit 式委托 \/ 成交记录/);
  assert.match(html, /function renderStrategyCenter\(data\)/);
});

test("hedge history distinguishes planned orders from actual fills", () => {
  assert.match(html, /function renderHedgeLedger\(data,status,acc\)/);
  assert.match(html, /\/api\/hedge_ledger\?days=14&limit=200/);
  assert.match(html, /实际成交/);
  assert.match(html, /成交率/);
  assert.match(html, /部分成交/);
  assert.match(html, /触发来源/);
  assert.match(html, /function renderHedgeExecutionRecords\(rows\)/);
  assert.match(html, /第 1 次重试/);
  assert.match(html, /初始等分/);
  assert.match(html, /本次实际成交/);
  assert.match(html, /执行阶段 \/ 五组/);
  assert.match(html, /`G1\/5～G5\/5`/);
  assert.match(html, /function hedgeExecutionPhase\(row\)/);
  assert.match(html, /开对冲 · \$\{group\}/);
  assert.match(html, /维持对冲 · 自动调仓/);
  assert.match(html, /关对冲 · \$\{group\}/);
  assert.match(html, /关对冲 · 自动归零/);
  assert.doesNotMatch(html, /组外 · 自动/);
});

test("hedge execution phases distinguish open, maintenance and close orders", () => {
  const logic = html.slice(
    html.indexOf("function hedgeExecutionPhase"),
    html.indexOf("function renderHedgeExecutionRecords"),
  );
  const context = {};
  vm.runInNewContext(logic, context);

  const phase = (row) => JSON.parse(JSON.stringify(context.hedgeExecutionPhase(row)));
  assert.deepEqual(phase({action: "REBALANCE", group_index: 1, group_count: 5}), {
    label: "开对冲 · G1/5",
    className: "open",
  });
  assert.deepEqual(phase({action: "REBALANCE", group_index: null}), {
    label: "维持对冲 · 自动调仓",
    className: "maintain",
  });
  assert.deepEqual(phase({action: "RELEASE", group_index: 4, group_count: 5}), {
    label: "关对冲 · G4/5",
    className: "close",
  });
  assert.deepEqual(phase({action: "CLOSE", group_index: null}), {
    label: "关对冲 · 自动归零",
    className: "close",
  });
});

test("hedge execution ledger refreshes every second while its tab is open", () => {
  assert.match(html, /function refreshHedgeLedgerFast\(\)/);
  assert.match(html, /activeOpsTab!=="hedges"/);
  assert.match(html, /api\("\/api\/hedge_ledger\?days=14&limit=200"\)/);
  assert.match(html, /setInterval\(refreshHedgeLedgerFast,1000\)/);
});

test("realized pnl curve supports zooming and panning into dense history", () => {
  ["curveZoomOut", "curveZoomSlider", "curveZoomLevel", "curveZoomIn", "curveZoomReset"].forEach(
    (id) => assert.match(html, new RegExp(`id="${id}"`)),
  );
  assert.match(html, /滚轮 \/ 滑杆连续缩放 · 拖动平移 · 双指拉伸/);
  assert.match(html, /function curveViewport\(rows\)/);
  assert.match(html, /function curvePointAt\(rows,index\)/);
  assert.match(html, /function setCurveZoom\(next,anchor=\.5\)/);
  assert.match(html, /requestAnimationFrame\(tick\)/);
  assert.match(html, /Math\.exp\(-event\.deltaY\*\.0008\)/);
  assert.match(html, /Math\.pow\(32,Number\(this\.value\)\/100\)/);
  assert.match(html, /canvas\.onwheel=event=>/);
  assert.match(html, /canvas\.onpointerdown=event=>/);
  assert.match(html, /canvas\.onpointermove=event=>/);
  assert.match(html, /curvePinch\.zoom\*distance/);
  assert.match(html, /includeZero=curveZoom<=1/);
  assert.match(html, /detailedTicks=maxTs-minTs<=2\*86400\*1000/);
});

test("curve viewport reveals a movable subset when zoomed", () => {
  const logic = html.slice(
    html.indexOf("function curveClamp"),
    html.indexOf("function updateCurveZoomControls"),
  );
  const context = {Math, curveZoom: 4, curvePan: 0.5};
  vm.runInNewContext(logic, context);

  const result = context.curveViewport(Array.from({length: 100}, (_, id) => id));

  assert.equal(result.visibleCount, 26);
  assert.equal(result.start, 37.125);
  assert.equal(result.end, 61.875);
  assert.deepEqual(Array.from(result.rows, row => row._curveIndex), Array.from({length: 28}, (_, id) => id + 36));
});

test("active strategy card explains why delta hedge is or is not trading", () => {
  assert.match(html, /function renderHedgeDiagnostic\(d\)/);
  assert.match(html, /Delta 对冲诊断/);
  assert.match(html, /priceLabel/);
  assert.match(html, /实时指数/);
  assert.match(html, /review_bar_minutes/);
  assert.match(html, /提前激活价/);
  assert.match(html, /d\.activation_price/);
  assert.match(html, /d\.reason_code/);
  assert.match(html, /期权 Delta/);
  assert.match(html, /永续 Delta/);
  assert.match(html, /衍生品净 Delta/);
  assert.match(html, /调仓阈值/);
  assert.match(html, /t\.hedge_diagnostic/);
});

test("current positions show the live Deribit hedge position", () => {
  assert.match(html, /class="panel live-hedge-panel"/);
  assert.match(html, /live-hedge-panel[\s\S]*actual-position-panel/);
  assert.match(html, /一键建立测试对冲，随后按 β 恢复线自动平仓/);
  assert.match(html, /id="liveHedgePosition"/);
  assert.match(html, /实时对冲持仓/);
  assert.match(html, /id="hedgePositionBadge"/);
  assert.match(html, /id="hedgePositionUsd"/);
  assert.match(html, /id="hedgePositionDelta"/);
  assert.match(html, /id="hedgePositionManaged"/);
  assert.match(html, /function renderLiveHedgePosition\(status,acc\)/);
  assert.match(html, /actual=Number\(acc\.perp_usd\|\|0\)/);
  assert.match(html, /t\.hedge\?\.futures_usd/);
  assert.match(html, /已开对冲/);
  assert.match(html, /未开对冲/);
  assert.match(html, /renderLiveHedgePosition\(status,acc\)/);
  assert.match(html, /期权持仓与未成交订单/);
  assert.doesNotMatch(html, /账户永续仓位/);
  assert.doesNotMatch(html, /id="accountPerp"/);
  assert.doesNotMatch(html, /id="managedPerp"/);
});

test("five-page shell is responsive and keeps danger actions explicit", () => {
  assert.match(html, /\.main-nav\{display:grid;grid-template-columns:repeat\(5,minmax\(0,1fr\)\)/);
  assert.match(html, /\.global-metrics\{display:grid;grid-template-columns:repeat\(5,minmax\(0,1fr\)\)/);
  assert.match(html, /@media\(max-width:700px\)[\s\S]*\.global-metrics\{grid-template-columns:repeat\(2,1fr\)/);
  assert.match(html, /class="btn btn-danger hide-tablet" onclick="openForceDialog\(\)"/);
  assert.match(html, /@media\(max-width:1120px\)[^}]*[\s\S]*?\.mobile-only\{display:grid\}/);
  assert.match(html, /function enhanceMobileTables\(\)/);
  assert.match(html, /@media\(max-width:700px\)[\s\S]*\.data-table tr\{display:block/);
});

test("tab switcher sticks independently below the scrolling overview", () => {
  assert.match(html, /\.topbar\{[^}]*position:static/);
  assert.match(html, /\.page-switcher\{[^}]*position:sticky;top:0;z-index:20/);
  assert.match(html, /@media\(max-width:1120px\)[\s\S]*\.mobile-only\{display:grid\}/);
  assert.match(html, /onclick="openForceDialog\(\)"/);
});

test("mobile safety menu remains inside the sticky tab switcher", () => {
  const switcher = html.indexOf('class="page-switcher"');
  const mobileMenu = html.indexOf('class="btn icon-btn mobile-only primary-mobile-menu"');
  assert.ok(switcher >= 0 && mobileMenu > switcher);
  assert.match(html, /@media\(max-width:1120px\)[^}]*[\s\S]*?\.topbar-command \.actions\{display:none\}/);
  assert.match(html, /@media\(max-width:700px\)[\s\S]*\.page-switcher \.topbar-command\{display:grid;grid-template-columns:minmax\(0,1fr\) 40px/);
});

test("hedge ledger exposes immediate retries and degraded recovery", () => {
  assert.match(html, /HEDGE_RETRYING/);
  assert.match(html, /HEDGE_DEGRADED/);
  assert.match(html, /row\.group_attempt_no/);
  assert.match(html, /row\.cycle_attempt_no/);
  assert.match(html, /row\.cycle_max_attempts/);
  assert.match(html, /15 秒后重试/);
  assert.match(html, /Deribit 式委托 \/ 成交记录/);
  assert.match(html, /Maker \+ Taker/);
  assert.match(html, /row\.requested_usd/);
  assert.match(html, /row\.filled_usd/);
  assert.match(html, /row\.liquidity_role/);
});

test("hedge summary excludes inactive strategies from its action status", () => {
  assert.match(html, /function activeHedgeAccountStatus\(data,status\)/);
  assert.match(html, /t\.hedge\.state!=="HEDGE_INACTIVE"/);
  assert.match(html, /latest\.residual_delta/);
  assert.match(html, /已激活策略对冲/);
  assert.match(html, /账户实际净 Delta/);
  assert.match(html, /待自动调仓/);
});
