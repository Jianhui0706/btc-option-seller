import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import vm from "node:vm";

const html=readFileSync(new URL("../index.html",import.meta.url),"utf8");

function loadJourneyHelpers(){
  const start=html.indexOf("function buildTradingViewStage(");
  const end=html.indexOf("function renderConditions(",start);
  assert.ok(start>=0&&end>start,"journey helpers must precede renderConditions");
  const context={};
  vm.runInNewContext(
    `${html.slice(start,end)};
     this.buildSignalJourney=buildSignalJourney;
     this.matchOpenSignal=matchOpenSignal;`,
    context,
  );
  return context;
}

function conditions(overrides={}){
  return {
    server_ts:1_784_710_000_000,
    pine:{
      received_at:1_784_709_900_000,
      bar_ts:1_784_707_200_000,
      cfg_ok:true,dvol_tier:-1,tier_ok:true,hv_ok:true,cci_ok:true,
      put_shape:false,call_shape:false,dvol_samples:90,hv_percentile:0.236,
    },
    backend:{checks:{
      config:{levels:[0.4,0.6,0.85]},
      dvol_tier:{ok:false,percentile:0.073,tier:0,samples:1460,window_bars:1460},
    }},
    watchdog:{armed:false,alarm:false,signal_received:false},
    ...overrides,
  };
}

test("Pine blockers keep the journey on market conditions despite backend preflight",()=>{
  const {buildSignalJourney}=loadJourneyHelpers();
  const data=conditions();
  data.pine.hv_ok=false;
  const journey=buildSignalJourney(data,{items:[]},[]);
  assert.equal(journey.currentStep,1);
  assert.equal(journey.headline,"现在不能开仓");
  assert.match(journey.reason,/HV/);
  assert.equal(journey.safety.mode,"preflight");
  assert.equal(journey.safety.tone,"info");
});

test("an eligible Pine bar with no request moves to delivery and can time out",()=>{
  const {buildSignalJourney}=loadJourneyHelpers();
  const data=conditions();
  data.pine.put_shape=true;
  data.watchdog={armed:true,alarm:true,signal_received:false,side:"PUT_CREDIT",bar_ts:data.pine.bar_ts,bar_close:data.pine.bar_ts+7_200_000};
  const journey=buildSignalJourney(data,{items:[]},[]);
  assert.equal(journey.currentStep,2);
  assert.equal(journey.headline,"等待开仓申请");
  assert.equal(journey.delivery.state,"timeout");
  assert.match(journey.reason,/尚未收到/);
});

test("only a real open signal from the same 2h bar passes delivery",()=>{
  const {buildSignalJourney}=loadJourneyHelpers();
  const data=conditions();
  data.pine.put_shape=true;
  const unrelated={kind:"OPEN_SIGNAL",signal_id:"old",status:"REJECTED",payload:{bar_ts:data.pine.bar_ts-7_200_000}};
  const matching={
    kind:"OPEN_SIGNAL",event:"OPEN_REQUEST",signal_id:"open-1",
    status:"REJECTED",reject_reason:"REJECT_DVOL_BELOW_ENTRY",
    spread_type:"PUT_CREDIT",received_at:data.server_ts-1_000,
    payload:{bar_ts:data.pine.bar_ts},
  };
  assert.equal(buildSignalJourney(data,{items:[unrelated]},[]).currentStep,2);
  const journey=buildSignalJourney(data,{items:[unrelated,matching]},[]);
  assert.equal(journey.currentStep,3);
  assert.equal(journey.delivery.state,"received");
  assert.equal(journey.safety.state,"rejected");
  assert.equal(journey.safety.reason,"REJECT_DVOL_BELOW_ENTRY");
  assert.equal(journey.safety.mode,"actual");
});

test("accepted signals advance through execution to an opened position",()=>{
  const {buildSignalJourney}=loadJourneyHelpers();
  const data=conditions();
  data.pine.put_shape=true;
  const signal={
    kind:"OPEN_SIGNAL",event:"OPEN_REQUEST",signal_id:"open-2",status:"ACCEPTED",
    spread_type:"PUT_CREDIT",received_at:data.server_ts-1_000,
    payload:{bar_ts:data.pine.bar_ts},
  };
  const checking=buildSignalJourney(data,{items:[signal]},[]);
  assert.equal(checking.currentStep,4);
  assert.equal(checking.headline,"正在执行开仓");

  const opening=buildSignalJourney(data,{items:[signal]},[
    {id:7,signal_id:"open-2",state:"OPENING",spread_type:"PUT_CREDIT"},
  ]);
  assert.equal(opening.execution.state,"opening");

  const opened=buildSignalJourney(data,{items:[signal]},[
    {id:7,signal_id:"open-2",state:"OPEN",spread_type:"PUT_CREDIT",opened_at:data.server_ts},
  ]);
  assert.equal(opened.headline,"已经开仓");

  const failed=buildSignalJourney(data,{items:[signal]},[
    {id:7,signal_id:"open-2",state:"OPEN_FAILED",close_reason:"REJECT_NO_VALID_LEGS"},
  ]);
  assert.equal(failed.currentStep,4);
  assert.equal(failed.execution.state,"failed");
  assert.match(failed.reason,/选腿|REJECT_NO_VALID_LEGS/);
});

test("active signals renders a four-step beginner journey",()=>{
  for(const id of ["journeySummary","journeyStep1","journeyStep2","journeyStep3","journeyStep4"])
    assert.match(html,new RegExp(`id="${id}"`));
  for(const label of [
    "等待市场出现开仓机会",
    "TradingView 发送开仓申请",
    "系统进行最终安全检查",
    "允许并执行开仓",
  ])assert.match(html,new RegExp(label));
  assert.doesNotMatch(html,/id="tvSignalStage"/);
  assert.doesNotMatch(html,/id="backendExecutionStage"/);
});

test("journey steps are buttons with accessible expansion state",()=>{
  assert.match(html,/class="journey-step-toggle"/);
  assert.match(html,/aria-expanded="\$\{open\}"/);
  assert.match(html,/aria-controls="journeyStepBody\$\{step\}"/);
  assert.match(html,/function toggleJourneyStep\(step\)/);
  assert.match(html,/function syncJourneyExpansion\(journey\)/);
});

test("journey expansion store survives polling but resets per instance",()=>{
  assert.match(html,/const journeyStepOverrides=\{\}/);
  assert.match(html,/let journeyInstanceKey=null/);
  assert.match(html,/let journeyAutoStep=null/);
  // 新实例时清空用户展开选择
  assert.match(html,/journey\.instanceKey!==journeyInstanceKey/);
  assert.match(html,/for\(const key of Object\.keys\(journeyStepOverrides\)\)delete journeyStepOverrides\[key\]/);
});

test("stale Pine, backend rejection, and execution failure stop at their own step",()=>{
  const {buildSignalJourney}=loadJourneyHelpers();
  const stale=conditions();
  stale.pine.received_at=stale.server_ts-151*60*1000;
  assert.equal(buildSignalJourney(stale,{items:[]},[]).currentStep,1);

  const source=html.slice(html.indexOf("function renderConditions("));
  assert.match(source,/TradingView 状态已过期/);
  assert.match(source,/提前预警，不是实际拒绝/);
  assert.match(source,/开仓执行失败/);
  assert.match(source,/系统检查数据暂不可用/);
});

test("expanded diagnostics never render authentication fields",()=>{
  assert.doesNotMatch(html,/JSON\.stringify\(.*passphrase/s);
  assert.doesNotMatch(html,/authorization.*journey-step-body/is);
  assert.match(html,/function safeSignalDetails\(/);
  assert.match(html,/signalMetaGrid\(d\.signal\)/);
});

test("journey is one vertical column without mobile overflow",()=>{
  assert.match(html,/\.journey-list\{display:grid;gap:10px\}/);
  assert.match(html,/@media\(max-width:700px\)[^{]*\{[\s\S]*?\.journey-step-toggle\{grid-template-columns:30px minmax\(0,1fr\) 18px/);
  assert.match(html,/@media\(max-width:700px\)[^{]*\{[\s\S]*?\.journey-step-status\{grid-column:2/);
  assert.doesNotMatch(html,/\.journey-list\{[^}]*grid-template-columns:repeat\(/);
});
