import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";

const html=readFileSync(new URL("../index.html",import.meta.url),"utf8");

test("strategy inspector loads history independently from the global recent feed",()=>{
  assert.match(html,/strategyActivityCache=new Map\(\)/);
  assert.match(html,/function strategyActivities\(data,name\)/);
  assert.match(html,/function loadStrategyActivities\(name,force=false\)/);
  assert.match(
    html,
    /api\(`\/api\/signals\?limit=500&strategy=\$\{encodeURIComponent\(name\)\}`\)/,
  );
  assert.match(html,/void loadStrategyActivities\(name\)/);
  assert.match(html,/正在按当前策略单独加载，不受全站 100 条限制/);
});
