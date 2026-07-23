import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import vm from "node:vm";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");

test("production static dashboard stays synchronized with frontend source", () => {
  const productionHtml=readFileSync(new URL("../../backend/static/index.html",import.meta.url),"utf8");
  const normalize=text=>text.replace(/\r\n/g,"\n");
  assert.equal(normalize(productionHtml),normalize(html),"run npm build before packaging or deployment");
});

test("step 1 keeps the blocker-first condition hierarchy inside the journey", () => {
  const summary = html.indexOf('id="journeySummary"');
  const step1 = html.indexOf('id="journeyStep1"');
  assert.ok(summary >= 0 && step1 > summary);
  // 条件卡片仍复用原有渲染，只是被收进第 1 步
  assert.match(html, /function renderConditionCard\(row,resolved\)/);
  assert.match(html, /class="condition-card blocker"/);
  // 第 1 步优先展示阻断项，再展示等待形态、交后端、待定
  const step1Body = html.indexOf("function renderStep1(");
  const blockers = html.indexOf('sect("condBlockers"', step1Body);
  const passed = html.indexOf("已通过 ${passed.length} 项", step1Body);
  assert.ok(blockers >= 0 && passed > blockers, "blockers render before the passed collapse");
});

test("waiting bollinger shape is amber, not an error", () => {
  assert.match(html, /等待市场触发形态/);
  assert.match(html, /等待 PUT\/CALL 破轨 \+ 反向收线/);
  // 未触发形态转成 waiting，而不是 false 阻断
  assert.match(html, /shapeCard\.resolved\.ok===false\)shapeCard\.resolved=\{\.\.\.shapeCard\.resolved,ok:"waiting"/);
});

test("passed conditions stay collapsed but expandable", () => {
  assert.match(html, /const conditionSections=\{passed:false,shape:false,evidence:false\}/);
  assert.match(html, /function toggleConditionSection\(name\)/);
  assert.match(html, /aria-expanded="\$\{conditionSections\.passed\}"/);
  assert.match(html, /已通过 \$\{passed\.length\} 项/);
});

test("raw calculation data is secondary and expandable", () => {
  assert.match(html, /查看原始计算数据/);
  assert.match(html, /conditionSections\.evidence/);
});

test("condition cards collapse to one column on compact screens", () => {
  assert.match(html, /@media\(max-width:900px\)[^{]*\{[^}]*\.condition-card-grid\{grid-template-columns:1fr\}/s);
  assert.match(html, /@media\(max-width:700px\)[^{]*\{[^}]*\.condition-toggle-note\{display:none\}/s);
});

function loadStageHelpers() {
  const start = html.indexOf("function buildTradingViewStage(");
  const end = html.indexOf("function renderConditions(", start);
  assert.ok(start >= 0 && end > start, "stage helpers should exist before renderConditions");
  const context = {};
  vm.runInNewContext(`${html.slice(start, end)};this.buildTradingViewStage=buildTradingViewStage;this.buildBackendStage=buildBackendStage`, context);
  return context;
}

test("delegated Pine DVOL does not block TradingView signal generation", () => {
  const {buildTradingViewStage} = loadStageHelpers();
  const waiting = buildTradingViewStage({pine:{
    dvol_tier:-1,tier_ok:true,cfg_ok:true,hv_ok:true,cci_ok:true,
    put_shape:false,call_shape:false,dvol_samples:89,
  }});
  assert.equal(waiting.title, "尚未发送开仓信号——等待 PUT/CALL 布林形态");
  assert.equal(waiting.delegated, 1);
  assert.equal(waiting.blocked, 0);
  assert.equal(waiting.waiting, 1);

  const ready = buildTradingViewStage({pine:{
    dvol_tier:-1,tier_ok:true,cfg_ok:true,hv_ok:true,cci_ok:true,
    put_shape:true,call_shape:false,dvol_samples:89,
  }});
  assert.equal(ready.state, "ready_to_send");
  assert.equal(ready.blocked, 0);
});

test("backend stage distinguishes preflight from a real signal result", () => {
  const {buildBackendStage} = loadStageHelpers();
  const conditions = {
    pine:{bar_ts:1784707200000},
    backend:{checks:{dvol_tier:{ok:false,percentile:0.073,tier:0,samples:1460,window_bars:1460},config:{levels:[0.4,0.6,0.85]}}},
  };
  const preflight = buildBackendStage(conditions,{items:[]});
  assert.equal(preflight.state,"waiting");
  assert.equal(preflight.preflight,"reject");
  assert.equal(preflight.actual,false);

  const rejected = buildBackendStage(conditions,{items:[{
    kind:"OPEN_SIGNAL",status:"REJECTED",reject_reason:"REJECT_DVOL_BELOW_ENTRY",
    spread_type:"PUT_CREDIT",received_at:1784714582979,payload:{bar_ts:1784707200000},
  }]});
  assert.equal(rejected.state,"rejected");
  assert.equal(rejected.actual,true);
  assert.equal(rejected.reason,"REJECT_DVOL_BELOW_ENTRY");
});

test("stale Pine data cannot be presented as the current TradingView decision", () => {
  const {buildTradingViewStage} = loadStageHelpers();
  const stage = buildTradingViewStage({server_ts:2_000_000_000,pine:{
    received_at:2_000_000_000-151*60*1000,
    dvol_tier:-1,cfg_ok:true,hv_ok:true,cci_ok:true,put_shape:true,call_shape:false,
  }});
  assert.equal(stage.state,"unknown");
});

test("a nearby but unrelated open signal is not attached to the current Pine bar", () => {
  const {buildBackendStage} = loadStageHelpers();
  const barTs=1784707200000;
  const stage=buildBackendStage({
    pine:{bar_ts:barTs},
    backend:{checks:{dvol_tier:{ok:false}}},
  },{items:[{
    kind:"OPEN_SIGNAL",signal_id:"BTC_OPTION_SELLER_V2_1784700000000",
    status:"REJECTED",received_at:barTs+2*60*60*1000,payload:{},
  }]});
  assert.equal(stage.actual,false);
  assert.equal(stage.state,"waiting");
});

test("active signals renders a four-step journey instead of two stages", () => {
  for (const id of ["journeySummary","journeyStep1","journeyStep2","journeyStep3","journeyStep4"])
    assert.match(html,new RegExp(`id="${id}"`));
  assert.doesNotMatch(html,/id="tvSignalStage"/);
  assert.doesNotMatch(html,/id="backendExecutionStage"/);
  assert.doesNotMatch(html,/id="signalTransit"/);
});

test("delegated DVOL and backend preflight keep unambiguous, non-green labels", () => {
  assert.match(html,/condition-card delegated/);
  assert.match(html,/交后端判断/);
  assert.match(html,/样本不足不阻止 TradingView 发信/);
  assert.match(html,/提前预警，不是实际拒绝/);
  assert.match(html,/后端 · Deribit DVOL 全历史/);
});

test("condition rendering consumes signals and parent trades", () => {
  assert.match(html,/function renderConditions\(data,signals,trades\)/);
  assert.match(html,/renderConditions\(conditions,signals,trades\)/);
});
