import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");

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
