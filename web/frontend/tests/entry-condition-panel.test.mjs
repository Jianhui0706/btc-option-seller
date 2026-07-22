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

test("entry conditions use a blocker-first hierarchy", () => {
  ["condBlockers", "condPending", "condPassed", "condShape"].forEach((id) =>
    assert.match(html, new RegExp(`id="${id}"`)),
  );
  const summary = html.indexOf('id="condSummary"');
  const blockers = html.indexOf('id="condBlockers"');
  const passed = html.indexOf('id="condPassed"');
  const shape = html.indexOf('id="condShape"');
  assert.ok(summary >= 0 && blockers > summary && passed > blockers && shape > passed);
  assert.match(html, /function renderConditionCard\(row,resolved\)/);
  assert.match(html, /class="condition-card blocker"/);
});

test("indeterminate conditions are separated from blockers", () => {
  assert.match(html, /const blockedCards=gateCards\.filter\(x=>x\.resolved\.ok===false\)/);
  assert.match(html, /const pendingCards=gateCards\.filter\(x=>x\.resolved\.ok==null\)/);
  assert.match(html, /暂时无法判定/);
});

test("passed conditions are collapsed but expandable", () => {
  assert.match(html, /const conditionSections=\{passed:false,shape:false,evidence:false\}/);
  assert.match(html, /function toggleConditionSection\(name\)/);
  assert.match(html, /aria-expanded="\$\{conditionSections\.passed\}"/);
  assert.match(html, /已通过 \$\{passedCards\.length\} 项/);
});

test("bollinger shape is rendered as the final trigger", () => {
  assert.match(html, /等待前置门槛/);
  assert.match(html, /等待破轨 \+ 反向收线/);
  assert.match(html, /PUT\/CALL 触发详情/);
  assert.match(html, /shapeCard\.resolved\.ok===true/);
});

test("evidence is visually secondary and expandable", () => {
  assert.match(html, /数据与判断依据/);
  assert.match(html, /conditionSections\.evidence/);
});

test("condition cards collapse to one column on compact screens", () => {
  assert.match(html, /@media\(max-width:900px\)[^{]*\{[^}]*\.condition-card-grid\{grid-template-columns:1fr\}/s);
  assert.match(html, /@media\(max-width:700px\)[^{]*\{[^}]*\.condition-toggle-note\{display:none\}/s);
});

function loadConditionSummary() {
  const source = html.match(/function conditionSummary\([\s\S]*?\n\}/)?.[0];
  assert.ok(source, "conditionSummary helper should exist");
  const context = {};
  vm.runInNewContext(`${source};this.conditionSummary=conditionSummary`, context);
  return context.conditionSummary;
}

test("known blockers outrank indeterminate gates in the headline", () => {
  const result = loadConditionSummary()([false, null, true], ["DVOL"], false, "Pine", true);
  assert.equal(result.title, "\u5f53\u524d\u4e0d\u4f1a\u53d1\u51fa\u5f00\u4ed3\u4fe1\u53f7");
  assert.equal(result.cls, "blocked");
});

test("unknown final shape remains indeterminate after all gates pass", () => {
  const result = loadConditionSummary()([true, true, true, true], [], null, "Pine", true);
  assert.equal(result.title, "\u6682\u65f6\u65e0\u6cd5\u5224\u5b9a");
  assert.equal(result.cls, "broken");
});

function loadStageHelpers() {
  const start = html.indexOf("function buildTradingViewStage(");
  const end = html.indexOf("function renderConditions(", start);
  assert.ok(start >= 0 && end > start, "two-stage helpers should exist before renderConditions");
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
  assert.equal(waiting.title, "\u5c1a\u672a\u53d1\u9001\u5f00\u4ed3\u4fe1\u53f7\u2014\u2014\u7b49\u5f85 PUT/CALL \u5e03\u6797\u5f62\u6001");
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

test("dashboard renders separate TradingView and backend stages", () => {
  for (const id of ["tvStageSummary","backendStageSummary","tvSignalStage","signalTransit","backendExecutionStage"])
    assert.match(html,new RegExp(`id="${id}"`));
  assert.match(html,/① TradingView 是否发送开仓信号/);
  assert.match(html,/② 后端收到后是否执行/);
  assert.match(html,/等待 TradingView 开仓信号/);
});

test("delegated DVOL and backend preflight use unambiguous labels", () => {
  assert.match(html,/condition-card delegated/);
  assert.match(html,/交后端判断/);
  assert.match(html,/样本不足不阻止 TradingView 发信/);
  assert.match(html,/若现在收到开仓信号/);
  assert.match(html,/后端 · Deribit DVOL 全历史/);
  assert.match(html,/已通过.*交后端.*等待触发.*阻断/s);
});

test("condition rendering consumes signal results for stage two", () => {
  assert.match(html,/function renderConditions\(data,signals\)/);
  assert.match(html,/renderConditions\(conditions,signals\)/);
});
