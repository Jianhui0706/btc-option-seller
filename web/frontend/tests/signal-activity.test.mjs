import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");

test("signal activity is a compact single-open accordion", () => {
  assert.match(html, /let model=null,eventLevel="ALL",refreshing=false,expandedSignalKey=null/);
  assert.match(html, /function toggleSignalActivity\(key\)/);
  assert.match(html, /expandedSignalKey===key\?null:key/);
  assert.match(html, /class="activity-stream"/);
  assert.match(html, /aria-expanded=/);
  assert.match(html, /\.activity-stream\{[^}]*max-height:520px;[^}]*overflow-y:auto/);
  assert.match(html, /@media\(max-width:700px\)[\s\S]*\.activity-stream\{max-height:390px/);
});

test("activity colors distinguish connection, open signal, exit, and rejection", () => {
  assert.match(html, /kind==="HEARTBEAT"/);
  assert.match(html, /TradingView 已连接 · 不触发开仓/);
  assert.match(html, /activity-heartbeat/);
  assert.match(html, /activity-open/);
  assert.match(html, /activity-exit/);
  assert.match(html, /activity-rejected/);
  assert.match(html, /activity-pending/);
  assert.match(html, /item\.status==="REJECTED"\?"activity-rejected"/);
  assert.match(html, /系统拒绝/);
  assert.match(html, /REJECT_DVOL_BELOW_ENTRY:"DVOL 低于开仓门槛"/);
});

test("strategy center contains all executable signals but excludes connection reports", () => {
  assert.match(html, /items\.filter\(item=>item\.kind!=="HEARTBEAT"\)\.slice\(0,8\)/);
  assert.match(html, /id="strategySignalList"/);
  assert.match(html, /真实开仓或退出信号/);
});

test("activity details remove secrets and escape dynamic values", () => {
  assert.match(html, /delete safePayload\.passphrase/);
  assert.match(html, /esc\(reasonCode\)/);
  assert.match(html, /esc\(JSON\.stringify\(safePayload,null,2\)\)/);
  assert.match(html, /toggleSignalActivity\(this\.closest\('article'\)\.dataset\.key\)/);
  assert.doesNotMatch(html, /toggleSignalActivity\('\$\{esc\(key\)\}'\)/);
});

test("signal metric combines open signals with all received webhook activity", () => {
  assert.match(html, /<div class="metric-label">开仓信号 \/ 收到信号<\/div>/);
  assert.match(
    html,
    /\$\("sTvCount"\)\.textContent=`\$\{sum\.open_signals\|\|0\} \/ \$\{sum\.total\|\|0\}`/,
  );
  assert.match(html, /收到信号包含连接回报/);
});

test("user-facing heartbeat terminology is connection report", () => {
  assert.match(html, /const eventLabel=isHeartbeat\?"连接回报"/);
  assert.match(
    html,
    /\$\{sum\.heartbeats\|\|0\} 回报 · \$\{sum\.signal_total\|\|0\} 信号/,
  );
  assert.doesNotMatch(
    html,
    /["`](?:[^"`]*)(?:链路心跳|Pine 心跳|TradingView 心跳)(?:[^"`]*)["`]/,
  );
});
