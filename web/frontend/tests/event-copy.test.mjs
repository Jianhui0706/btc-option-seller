import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";

const html=readFileSync(new URL("../index.html",import.meta.url),"utf8");

test("risk threshold warnings are distinct from scan failures",()=>{
  assert.match(html,/EVENT\.RISK_ALERT="风险阈值提醒"/);
  assert.match(html,/p\.command==="RISK_ALERT"\?"风险阈值提醒"/);
  assert.match(html,/自动处置未开启，本条仅作风险提醒/);
});

test("hedge checkpoints render as a compact Chinese summary",()=>{
  assert.match(html,/EVENT\.HEDGE_CHECKPOINT="对冲门槛检查"/);
  assert.match(html,/function hedgeCheckpointText\(p\)/);
  assert.match(html,/尚未触发，距离激活价/);
  assert.match(html,/本批 .* 次 \/ 累计 .* 次/);
  assert.match(html,/function compactTimelineEvents\(events\)/);
  assert.match(html,/seen\.has\(key\)/);
});
