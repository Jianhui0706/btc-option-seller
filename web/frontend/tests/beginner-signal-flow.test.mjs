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
