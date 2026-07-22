# Active Signals Blocker-First Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current flat entry-condition table with a blocker-first active-signals panel that immediately explains why an opening signal cannot fire while preserving all existing strategy decisions.

**Architecture:** Keep the existing `/api/entry_conditions` contract and `condRow()` authority logic unchanged. Reorganize its five resolved rows into blocker, pending, passed, and final-shape presentation groups inside the existing single-page frontend; store accordion state only in frontend memory and rerender from `model.conditions`.

**Tech Stack:** Static HTML/CSS/JavaScript, Node.js built-in test runner, Vite production build, FastAPI static mirror, browser visual verification.

## Global Constraints

- Do not modify Pine Script conditions, backend condition calculations, Webhook authentication, signal execution, or opening rules.
- Preserve the current TradingView-versus-backend authority selection, including backend-authoritative deferred DVOL.
- Treat `false` as blocked, `true` as passed, and `null` as indeterminate; never classify `null` as blocked.
- Blockers remain expanded; passed conditions default collapsed but remain user-expandable.
- Bollinger shape is a separate final-trigger section.
- Preserve the sticky four-tab navigation and mobile safety menu.
- Do not stage or modify unrelated existing working-tree changes.

---

### Task 1: Blocker-first information hierarchy

**Files:**
- Modify: `web/frontend/index.html:42-103`
- Modify: `web/frontend/index.html:177-187`
- Modify: `web/frontend/index.html:296-398`
- Test: `web/frontend/tests/entry-condition-panel.test.mjs`
- Generated: `web/backend/static/index.html`

**Interfaces:**
- Consumes: existing `COND_ROWS`, `condRow(r, data, pineFresh, deferred)`, and `renderConditions(data)` inputs.
- Produces: `renderConditionCard(row, resolved)` returning blocker/passed card HTML and stable containers `#condBlockers`, `#condPending`, `#condPassed`, and `#condShape`.

- [ ] **Step 1: Write the failing structure test**

Create `web/frontend/tests/entry-condition-panel.test.mjs`:

```js
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
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
node --test web/frontend/tests/entry-condition-panel.test.mjs
```

Expected: FAIL because the four new containers and `renderConditionCard()` do not exist.

- [ ] **Step 3: Add the blocker-first markup and card styles**

Replace the flat condition-header/row area with:

```html
<div id="condSummary" class="cond-summary">...</div>
<div id="condWatch" class="cond-watch" style="display:none"></div>
<div id="condLive" class="cond-live" style="display:none"></div>
<section id="condBlockers" class="condition-section"></section>
<section id="condPending" class="condition-section"></section>
<section id="condPassed" class="condition-section"></section>
<section id="condShape" class="condition-section"></section>
<section id="condEvidence" class="condition-evidence"></section>
```

Add CSS classes for `.condition-section-title`, `.condition-card-grid`, `.condition-card`, `.condition-card.blocker`, `.condition-card.pending`, `.condition-card-head`, `.condition-card-value`, `.condition-card-gap`, `.condition-card-source`, and `.condition-card-details`. Blocker cards use a red left border and readable gap copy; pending cards use neutral/amber styling.

- [ ] **Step 4: Partition resolved rows without changing authority rules**

After calculating `pineFresh`, `deferred`, and `pick`, resolve rows once:

```js
const renderedRows=COND_ROWS.map(row=>({
  row,
  resolved:condRow(row,data,pineFresh,deferred),
}));
const gateCards=renderedRows.slice(0,4);
const shapeCard=renderedRows[4];
const blockedCards=gateCards.filter(x=>x.resolved.ok===false);
const pendingCards=gateCards.filter(x=>x.resolved.ok==null);
const passedCards=gateCards.filter(x=>x.resolved.ok===true);
```

Implement `renderConditionCard(row,resolved)` to show the existing resolved value, `standard`, `gap`, `srcTag`, and `extra` without recomputing a strategy result.

- [ ] **Step 5: Render conclusion, blockers, and pending conditions**

Populate `#condBlockers` only when `blockedCards.length > 0`, with heading `需要先解决` and a fixed-order card grid. Populate `#condPending` only when `pendingCards.length > 0`, with heading `暂时无法判定`. Keep the existing conclusion-state decisions, but make the summary copy list blockers and show authority plus update time as secondary metadata.

- [ ] **Step 6: Run focused and complete frontend tests**

Run:

```powershell
node --test web/frontend/tests/entry-condition-panel.test.mjs
node --test web/frontend/tests/*.test.mjs
```

Expected: the new focused tests pass and all existing frontend tests remain green.

- [ ] **Step 7: Build the static mirror and commit**

Run:

```powershell
npm.cmd run build --prefix web/frontend
git add -- web/frontend/index.html web/frontend/tests/entry-condition-panel.test.mjs web/backend/static/index.html
git commit -m "feat: prioritize active-signal blockers"
```

Expected: Vite succeeds and only the three listed files are committed.

---

### Task 2: Passed-condition accordion and final-shape section

**Files:**
- Modify: `web/frontend/index.html:42-105`
- Modify: `web/frontend/index.html:229-235`
- Modify: `web/frontend/index.html:348-398`
- Test: `web/frontend/tests/entry-condition-panel.test.mjs`
- Generated: `web/backend/static/index.html`

**Interfaces:**
- Consumes: `model.conditions`, the Task 1 `renderedRows` grouping, and `renderConditionCard()`.
- Produces: `conditionSections = {passed:false, shape:false, evidence:false}` and `toggleConditionSection(name)`.

- [ ] **Step 1: Add failing interaction tests**

Append:

```js
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
```

- [ ] **Step 2: Run focused test and verify RED**

Run `node --test web/frontend/tests/entry-condition-panel.test.mjs`.

Expected: FAIL because accordion state and final-shape presentation are absent.

- [ ] **Step 3: Implement stable accordion state**

Add beside the existing frontend state variables:

```js
const conditionSections={passed:false,shape:false,evidence:false};
function toggleConditionSection(name){
  if(!(name in conditionSections))return;
  conditionSections[name]=!conditionSections[name];
  if(model?.conditions)renderConditions(model.conditions);
}
```

The object must not be reset inside `renderConditions()`, so automatic refreshes preserve user choices during the page session.

- [ ] **Step 4: Render the passed accordion**

When `passedCards.length > 0`, render a button with `aria-expanded`, text `已通过 ${passedCards.length} 项`, and click handler `toggleConditionSection('passed')`. Render passed cards only while `conditionSections.passed` is true. Hide the entire section when the count is zero.

- [ ] **Step 5: Render the final shape state**

Render `shapeCard` separately:

- if any gate is blocked or pending, label it `等待前置门槛` and keep details collapsed;
- if all gates pass but shape is false, label it `等待破轨 + 反向收线` and expose the PUT/CALL details;
- if shape is true, show a green completed direction card;
- if shape is null, show `暂时无法判定`.

Use `conditionSections.shape` as the manual override for expanding and collapsing shape details.

- [ ] **Step 6: Move timestamps and source explanations into evidence accordion**

Keep the current `foot` content, but render it under a `数据与判断依据` button controlled by `conditionSections.evidence`. Watchdog alarms remain outside the accordion and stay immediately visible.

- [ ] **Step 7: Run tests, build, and commit**

Run:

```powershell
node --test web/frontend/tests/*.test.mjs
python -m pytest -q web/backend/tests
npm.cmd run build --prefix web/frontend
git diff --check
git add -- web/frontend/index.html web/frontend/tests/entry-condition-panel.test.mjs web/backend/static/index.html
git commit -m "feat: add condition detail accordions"
```

Expected: all frontend and backend tests pass, build succeeds, and whitespace check reports no errors.

---

### Task 3: Responsive verification, review, and cloud deployment

**Files:**
- Modify if verification finds a defect: `web/frontend/index.html`
- Modify alongside any defect fix: `web/frontend/tests/entry-condition-panel.test.mjs`
- Generated: `web/backend/static/index.html`

**Interfaces:**
- Consumes: completed blocker-first panel from Tasks 1 and 2.
- Produces: verified production package and deployed TESTNET dashboard.

- [ ] **Step 1: Add and run responsive contract tests**

Add assertions that mobile condition cards are single-column, accordion buttons have a minimum touch target, and existing sticky-navigation/mobile-menu selectors remain present. Run all frontend tests and verify PASS.

- [ ] **Step 2: Perform browser verification at three widths**

Start the local FastAPI server in a hidden window and inspect `/signals` at 1280×720, 900×800, and 390×844. Verify:

- blockers are readable without clicking;
- passed conditions open and close;
- accordion state survives a manual data refresh;
- shape section behavior matches gate state;
- no horizontal overflow exists;
- sticky navigation and the mobile safety menu remain visible after scrolling.

- [ ] **Step 3: Request read-only code review**

Review the implementation range against `docs/superpowers/specs/2026-07-22-active-signals-blocker-first-design.md`. Fix every Critical or Important issue with a failing test first, rerun verification, and commit only relevant files.

- [ ] **Step 4: Run final verification**

Run:

```powershell
node --test web/frontend/tests/*.test.mjs
python -m pytest -q web/backend/tests
npm.cmd run build --prefix web/frontend
git diff --check
```

Expected: zero test failures, successful Vite build, and no whitespace errors in the implementation files.

- [ ] **Step 5: Build and deploy the cloud package**

Run `deploy/build-cloud-package.ps1`, upload the resulting archive to the existing Tencent Cloud host with the configured SSH key, and run `deploy/cloud/install.sh`. Preserve the existing database and TESTNET lock.

- [ ] **Step 6: Verify the public deployment**

Check `/`, `/signals`, `/signal-log`, `/returns`, and `/status` through the public HTTPS domain. Require HTTP 200 for all pages, `env = testnet`, `force_close_active = false`, and deployed HTML markers for the blocker-first containers and accordion state.

- [ ] **Step 7: Report delivery**

Report the public URL, test counts, deployment status, TESTNET status, relevant commits, and any pre-existing working-tree changes that were intentionally left untouched.
