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
