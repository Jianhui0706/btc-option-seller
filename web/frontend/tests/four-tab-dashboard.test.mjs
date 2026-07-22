import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");

test("dashboard navigation follows the teacher's four-tab workflow", () => {
  const labels = ["仓位", "活跃信号", "信号日志", "仓位日志"];
  const routes = ["/", "/signals", "/signal-log", "/returns"];
  const pages = ["positions", "active-signals", "signal-log", "position-log"];

  const positions = labels.map((label) => html.indexOf(`<span>${label}</span>`));
  assert.ok(positions.every((position) => position >= 0));
  assert.deepEqual([...positions].sort((a, b) => a - b), positions);
  routes.forEach((route) => assert.match(html, new RegExp(`navigateTo\\('${route.replace("/", "\\/")}\\'\\)`)));
  pages.forEach((page) => assert.match(html, new RegExp(`id="page-${page}"`)));
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

test("each teacher tab owns the correct operational content", () => {
  const positions = html.split('id="page-positions"')[1].split('id="page-active-signals"')[0];
  const activeSignals = html.split('id="page-active-signals"')[1].split('id="page-signal-log"')[0];
  const signalLog = html.split('id="page-signal-log"')[1].split('id="page-position-log"')[0];
  const positionLog = html.split('id="page-position-log"')[1].split('id="forceModal"')[0];

  assert.match(positions, /id="activeTrades"/);
  assert.match(positions, /id="positionsTable"/);
  assert.match(activeSignals, /id="condRows"/);
  assert.match(activeSignals, /id="activeSignalList"/);
  assert.doesNotMatch(activeSignals, /id="tvSignals"/);
  assert.match(signalLog, /id="tvSignals"/);
  assert.match(signalLog, /id="webhookUrl"/);
  assert.match(positionLog, /id="pnlChart"/);
  assert.match(positionLog, /id="eventTimeline"/);
  assert.match(positionLog, /id="tradesTable"/);
  assert.match(positionLog, /id="hedgesTable"/);
  assert.match(html, /function renderActiveSignals\(items\)/);
});

test("four-tab shell is responsive and keeps danger actions explicit", () => {
  assert.match(html, /\.main-nav\{display:grid;grid-template-columns:repeat\(4,minmax\(0,1fr\)\)/);
  assert.match(html, /\.global-metrics\{display:grid;grid-template-columns:repeat\(5,minmax\(0,1fr\)\)/);
  assert.match(html, /@media\(max-width:700px\)[\s\S]*\.global-metrics\{grid-template-columns:repeat\(2,1fr\)/);
  assert.match(html, /class="btn btn-danger hide-tablet" onclick="openForceDialog\(\)"/);
  assert.match(html, /@media\(max-width:1120px\)[^}]*[\s\S]*?\.mobile-only\{display:grid\}/);
  assert.match(html, /function enhanceMobileTables\(\)/);
  assert.match(html, /@media\(max-width:700px\)[\s\S]*\.data-table tr\{display:block/);
});
