import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");

test("dashboard navigation promotes TradingView setup beside the teacher workflow", () => {
  const labels = ["仓位", "TV 接入", "策略信号", "信号日志", "仓位日志"];
  const routes = ["/", "/tradingview", "/signals", "/signal-log", "/returns"];
  const pages = ["positions", "tradingview", "active-signals", "signal-log", "position-log"];

  const positions = labels.map((label) => html.indexOf(`<span>${label}</span>`));
  assert.ok(positions.every((position) => position >= 0));
  assert.deepEqual([...positions].sort((a, b) => a - b), positions);
  routes.forEach((route) => assert.match(html, new RegExp(`navigateTo\\('${route.replace("/", "\\/")}\\'\\)`)));
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

test("each primary page owns the correct operational content", () => {
  const positions = html.split('id="page-positions"')[1].split('id="page-tradingview"')[0];
  const tradingview = html.split('id="page-tradingview"')[1].split('id="page-active-signals"')[0];
  const activeSignals = html.split('id="page-active-signals"')[1].split('id="page-signal-log"')[0];
  const signalLog = html.split('id="page-signal-log"')[1].split('id="page-position-log"')[0];
  const positionLog = html.split('id="page-position-log"')[1].split('id="forceModal"')[0];

  assert.match(positions, /id="activeTrades"/);
  assert.match(positions, /id="positionsTable"/);
  assert.match(tradingview, /id="tvSetupWebhook"/);
  assert.match(tradingview, /id="tvAlertMessage"/);
  assert.match(tradingview, /四步完成接入/);
  assert.match(activeSignals, /id="strategyRoster"/);
  assert.match(activeSignals, /id="strategySignalList"/);
  assert.match(activeSignals, /不重复计算任何策略指标/);
  assert.doesNotMatch(activeSignals, /id="journeySummary"/);
  assert.doesNotMatch(activeSignals, /id="tvSignals"/);
  assert.match(signalLog, /id="tvSignals"/);
  assert.match(signalLog, /id="webhookUrl"/);
  assert.match(positionLog, /id="pnlChart"/);
  assert.match(positionLog, /id="eventTimeline"/);
  assert.match(positionLog, /id="tradesTable"/);
  assert.match(positionLog, /id="hedgesTable"/);
  assert.match(html, /function renderStrategyCenter\(data\)/);
});

test("active strategy card explains why delta hedge is or is not trading", () => {
  assert.match(html, /function renderHedgeDiagnostic\(d\)/);
  assert.match(html, /Delta 对冲诊断/);
  assert.match(html, /3分钟收盘/);
  assert.match(html, /BEP 激活线/);
  assert.match(html, /期权 Delta/);
  assert.match(html, /永续 Delta/);
  assert.match(html, /衍生品净 Delta/);
  assert.match(html, /调仓阈值/);
  assert.match(html, /t\.hedge_diagnostic/);
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
