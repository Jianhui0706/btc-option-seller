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
  assert.match(html, /系统拒绝/);
  assert.match(html, /REJECT_DVOL_BELOW_ENTRY:"DVOL 低于开仓门槛"/);
});

test("activity details remove secrets and escape dynamic values", () => {
  assert.match(html, /delete safePayload\.passphrase/);
  assert.match(html, /esc\(reasonCode\)/);
  assert.match(html, /esc\(JSON\.stringify\(safePayload,null,2\)\)/);
  assert.match(html, /toggleSignalActivity\(this\.closest\('article'\)\.dataset\.key\)/);
  assert.doesNotMatch(html, /toggleSignalActivity\('\$\{esc\(key\)\}'\)/);
});
