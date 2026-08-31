import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";

const html=readFileSync(new URL("../index.html",import.meta.url),"utf8");

test("production static dashboard stays synchronized with frontend source",()=>{
  const productionHtml=readFileSync(new URL("../../backend/static/index.html",import.meta.url),"utf8");
  const normalize=text=>text.replace(/\r\n/g,"\n");
  assert.equal(normalize(productionHtml),normalize(html),"run npm build before packaging or deployment");
});

test("strategy signal page is multi-strategy rather than one Pine condition monitor",()=>{
  const page=html.split('id="page-active-signals"')[1].split('id="page-signal-log"')[0];
  assert.match(page,/策略信号中心/);
  assert.match(page,/id="strategyRoster"/);
  assert.match(page,/id="strategyInspector"/);
  assert.match(page,/已发现策略/);
  assert.match(page,/已有交易信号/);
  assert.match(page,/策略来源与交易执行分开管理/);
  assert.doesNotMatch(page,/id="journeySummary"/);
  assert.doesNotMatch(page,/id="journeyStep1"/);
  assert.doesNotMatch(page,/DVOL 档位/);
  assert.doesNotMatch(page,/HV 分位/);
  assert.doesNotMatch(page,/condBlockers|condition-card|condEvidence/);
});

test("strategy center auto-discovers and groups webhook strategies",()=>{
  assert.match(html,/function strategyDirectoryCard\(item,activities,serverTs\)/);
  assert.match(html,/function renderStrategyCenter\(data\)/);
  assert.match(html,/const strategies=data\.strategies\|\|\[\]/);
  assert.match(html,/strategies\.map\(item=>strategyDirectoryCard\(item,items,data\.server_ts\)\)/);
  assert.match(html,/item\.active_trades/);
  assert.match(html,/strategy\.open_signals/);
  assert.match(html,/strategy\.exit_signals/);
  assert.match(html,/strategy\.accepted/);
  assert.match(html,/strategy\.rejected/);
  assert.match(html,/id="strategyModal"/);
  assert.match(html,/function openStrategyModal\(name\)/);
  assert.match(html,/function renderStrategyModal\(data\)/);
  assert.match(html,/function strategyModalActivityRow\(item,serverTs\)/);
  assert.match(html,/class="strategy-source-select"/);
  assert.match(html,/function renderStrategyInspector\(data\)/);
  assert.match(html,/class="strategy-modal-feed"/);
  assert.match(html,/overflow-y:scroll/);
  assert.match(html,/全部活动/);
  assert.match(html,/连接回报/);
  assert.match(html,/交易信号/);
  assert.match(html,/当前显示 \$\{filtered\.length\} \/ \$\{all\.length\} 条/);
  assert.match(html,/function toggleStrategyActivity\(key\)/);
  assert.match(html,/function strategyTransportDetail\(item\)/);
  assert.match(html,/查看传输内容/);
  assert.match(html,/TradingView 传输字段/);
  assert.match(html,/脱敏后的原始 JSON/);
  assert.match(html,/Webhook 口令不会在这里显示/);
  assert.match(html,/aria-expanded="\$\{expanded\}"/);
});

test("dashboard polling no longer fetches the single-strategy condition snapshot",()=>{
  const refresh=html.slice(html.indexOf("async function refresh("),html.indexOf("function showTab("));
  assert.doesNotMatch(refresh,/api\/entry_conditions/);
  assert.doesNotMatch(refresh,/renderConditions/);
  assert.match(refresh,/api\/signals\?limit=100/);
});

test("multi-strategy layout remains responsive",()=>{
  assert.match(html,/\.strategy-center-layout\{display:grid;grid-template-columns:/);
  assert.match(html,/@media\(max-width:1120px\)[\s\S]*\.strategy-center-layout\{grid-template-columns:1fr\}/);
  assert.match(html,/@media\(max-width:700px\)[\s\S]*\.strategy-roster\{grid-template-columns:1fr/);
});

test("signal and position history expose all three correlation identities",()=>{
  assert.match(html,/function tradeIdentity\(value,includeTrade=true\)/);
  assert.match(html,/<b>strategy<\/b>/);
  assert.match(html,/<b>tv_id<\/b>/);
  assert.match(html,/<b>signal_id<\/b>/);
  assert.match(html,/策略 \/ Alert \/ 信号/);
  assert.match(html,/safePayload\.tv_id\?\?safePayload\.trade_id/);
});
