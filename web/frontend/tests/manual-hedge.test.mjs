import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const html = await readFile(new URL("../index.html", import.meta.url), "utf8");

test("live hedge card exposes a guarded one-click hedge action", () => {
  assert.match(html, /id="manualHedgeButton"/);
  assert.match(html, /onclick="manualHedge\(\)"/);
  assert.match(html, />一键对冲（自动平）</);
  assert.match(html, /function manualHedge\(\)/);
  assert.match(html, /\/control\/manual_hedge/);
  assert.match(html, /5 个近似等量批次/);
  assert.match(html, /两档 Maker 各等待 10 秒/);
  assert.match(html, /开、关对冲严格互斥/);
  assert.match(html, /id="manualHedgeReleaseButton"/);
  assert.match(html, /onclick="manualHedgeRelease\(\)"/);
  assert.match(html, /\/control\/manual_hedge_release/);
});

test("one-click hedge is disabled for non-open or unsafe account state", () => {
  assert.match(html, /\["OPEN","PARTIALLY_CLOSED"\]/);
  assert.match(html, /button\.disabled=!hedgeable\|\|busy\|\|different/);
  assert.match(html, /release\.disabled=!hasManaged\|\|busy\|\|different/);
  assert.match(html, /实际永续与系统归属/);
  assert.match(html, /逐级 IOC 补齐/);
  assert.match(html, /每批开始前复算最新 Delta/);
  assert.match(html, /随后按 β 恢复线自动平仓/);
  assert.match(html, /成交后交回自动状态机/);
  assert.match(html, /随后自动监控撤销/);
  assert.match(html, /关对冲完成归零前/);
  assert.match(html, /manualHold/);
  assert.match(html, /group_index/);
  assert.match(html, /group_count/);
  assert.match(html, /不会平期权、隔离残余腿/);
});
