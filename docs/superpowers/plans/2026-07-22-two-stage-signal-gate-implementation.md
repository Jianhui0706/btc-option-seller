# Two-Stage Signal Gate Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the active-signals page separately and accurately explain whether TradingView will send an opening signal and whether the backend would execute a received signal.

**Architecture:** Keep the existing APIs and derive two small frontend view models from `/api/entry_conditions` and `/api/signals`. Stage one is Pine-authoritative and treats `dvol_tier=-1` as delegated rather than passed; stage two uses the latest matching real `OPEN_REQUEST` when present and otherwise renders a clearly labelled backend preflight.

**Tech Stack:** Pine status JSON, FastAPI JSON endpoints, single-file HTML/CSS/JavaScript frontend, Node built-in test runner, pytest, Vite.

## Global Constraints

- TradingView signal generation and backend execution must be shown as separate stages.
- `dvol_tier=-1` must be blue “交后端判断”, never green or red.
- Waiting for a Bollinger trigger is amber and is not an error.
- A backend preflight must say “若现在收到” and must not look like an actual rejection.
- A real signal status and `reject_reason` override backend preflight copy.
- Preserve the dark visual system, passed-condition accordion, evidence accordion, and mobile no-overflow behavior.
- Do not modify Pine signal-generation behavior or backend trading decisions in this feature.

---

### Task 1: Build explicit stage view models

**Files:**
- Modify: `web/frontend/index.html`
- Modify: `web/frontend/tests/entry-condition-panel.test.mjs`

**Interfaces:**
- Consumes: `conditions.pine`, `conditions.backend`, `signals.items`.
- Produces: `buildTradingViewStage(conditions)` and `buildBackendStage(conditions, signals)` plain objects used only by rendering code.

- [ ] **Step 1: Write failing behavioral tests for TradingView delegation and trigger state**

Extract the pure helpers from `index.html` with the existing `vm` pattern and assert:

```js
const stage = buildTradingViewStage({
  pine: {dvol_tier:-1, tier_ok:true, cfg_ok:true, hv_ok:true, cci_ok:true,
         put_shape:false, call_shape:false, dvol_samples:89},
});
assert.equal(stage.title, "尚未发送开仓信号——等待 PUT/CALL 布林形态");
assert.equal(stage.delegated, 1);
assert.equal(stage.blocked, 0);
assert.equal(stage.waiting, 1);
```

Also assert that `put_shape=true` with the same delegated DVOL produces `ready_to_send` rather than a DVOL block.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `node --test tests/entry-condition-panel.test.mjs`

Expected: FAIL because `buildTradingViewStage` and `buildBackendStage` do not exist.

- [ ] **Step 3: Implement the pure view-model helpers**

Add helpers before `renderConditions`:

```js
function buildTradingViewStage(data){
  const p=data?.pine;
  if(!p)return {state:"unknown",title:"TradingView 暂时无法完成判断",passed:0,delegated:0,waiting:0,blocked:0};
  const delegated=p.dvol_tier==null||p.dvol_tier<0;
  const blockers=[!p.cfg_ok&&"档位配置",!delegated&&!p.tier_ok&&"DVOL 档位",!p.hv_ok&&"HV 分位",!p.cci_ok&&"CCI"].filter(Boolean);
  const shape=p.put_shape||p.call_shape;
  if(blockers.length)return {state:"blocked",title:"TradingView 当前不会发送开仓信号",blockers,delegated:+delegated,waiting:+!shape};
  if(!shape)return {state:"waiting",title:"尚未发送开仓信号——等待 PUT/CALL 布林形态",blockers:[],delegated:+delegated,waiting:1};
  return {state:"ready_to_send",title:"TradingView 已具备发送开仓信号的条件",blockers:[],delegated:+delegated,waiting:0};
}
```

Implement `buildBackendStage(data, signals)` so it selects the newest `OPEN_SIGNAL` whose payload `bar_ts` equals the current Pine `bar_ts`. When matched, return its actual status and rejection reason. Otherwise return `state:"waiting"` plus DVOL preflight fields from `backend.checks.dvol_tier`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `node --test tests/entry-condition-panel.test.mjs`

Expected: all entry-condition tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add web/frontend/index.html web/frontend/tests/entry-condition-panel.test.mjs
git commit -m "feat: model signal generation and execution stages"
```

### Task 2: Render the two-stage dashboard

**Files:**
- Modify: `web/frontend/index.html`
- Modify: `web/frontend/tests/entry-condition-panel.test.mjs`
- Modify: `web/frontend/tests/four-tab-dashboard.test.mjs`
- Generated: `web/backend/static/index.html`

**Interfaces:**
- Consumes: `buildTradingViewStage(conditions)` and `buildBackendStage(conditions, signals)` from Task 1.
- Produces: DOM containers `#tvSignalStage`, `#signalTransit`, `#backendExecutionStage`, and summary lines `#tvStageSummary`, `#backendStageSummary`.

- [ ] **Step 1: Write failing structure and copy tests**

Assert the four IDs exist and the source contains these exact labels:

```js
for (const id of ["tvStageSummary","backendStageSummary","tvSignalStage","backendExecutionStage"])
  assert.match(html,new RegExp(`id="${id}"`));
assert.match(html,/交后端判断/);
assert.match(html,/若现在收到开仓信号/);
assert.match(html,/等待 PUT\/CALL 布林形态/);
```

Assert delegated cards use `condition-card delegated`, not `condition-card passed`, and the stage counters contain passed, delegated, waiting, and blocked values.

- [ ] **Step 2: Run tests and verify RED**

Run: `node --test tests/entry-condition-panel.test.mjs tests/four-tab-dashboard.test.mjs`

Expected: FAIL for missing two-stage containers.

- [ ] **Step 3: Replace the ambiguous single conclusion with two summary lines**

Render:

```html
<div id="tvStageSummary" class="stage-summary"></div>
<div id="backendStageSummary" class="stage-summary"></div>
```

The TradingView line must never use backend DVOL to decide whether Pine will send. The backend line must say “预检” until a matching real signal exists.

- [ ] **Step 4: Render Stage 1 conditions with explicit current value, rule, source, and state**

Use Pine values for configuration, HV, CCI, and shape. For delegated DVOL render:

```html
<article class="condition-card delegated">
  <span class="condition-card-status">交后端判断</span>
  <div class="condition-card-value">Pine 样本 89 根</div>
  <div class="condition-card-standard">样本不足不阻止 TradingView 发信</div>
</article>
```

Keep true passed cards in the existing collapsed passed accordion. Keep shape visible and amber while waiting.

- [ ] **Step 5: Render transit and Stage 2 preflight/actual result**

When no matching signal exists, show “等待 TradingView 开仓信号” and a backend DVOL preflight card containing percentile, tier, samples/window, L1 threshold, gap, and `后端 · Deribit DVOL 全历史`.

When a matching signal exists, show direction, receive time, actual status, and an expandable rejection reason. Escape every signal-derived string with `esc()`.

- [ ] **Step 6: Add responsive styling**

Use a vertical stage flow on all widths. At `max-width:900px`, cards remain one column. At `max-width:700px`, hide secondary notes but retain stage titles, current values, requirements, and status labels.

- [ ] **Step 7: Run frontend tests and build**

Run:

```bash
node --test tests/*.test.mjs
npm run build
```

Expected: all frontend tests pass and Vite writes `web/backend/static/index.html`.

- [ ] **Step 8: Commit Task 2**

```bash
git add web/frontend/index.html web/frontend/tests web/backend/static/index.html
git commit -m "feat: show TradingView and backend signal stages"
```

### Task 3: Regression, visual verification, and cloud deployment

**Files:**
- Verify: `web/backend/tests/test_dashboard_pages.py`
- Verify: `web/backend/static/index.html`
- Use: `deploy/build-cloud-package.ps1`

**Interfaces:**
- Consumes: complete Task 2 frontend.
- Produces: tested cloud deployment at `https://btc-option-seller-43-160-202-151.sslip.io/signals`.

- [ ] **Step 1: Run all local suites**

Run:

```powershell
python -m pytest web/backend/tests -q
Push-Location web/frontend
node --test tests/*.test.mjs
npm run build
Pop-Location
```

Expected: 86 backend tests pass, all frontend tests pass, and build exits 0.

- [ ] **Step 2: Perform independent code review**

Review the feature commits against `docs/superpowers/specs/2026-07-22-two-stage-signal-gate-design.md`. Fix every Critical or Important issue and rerun Step 1.

- [ ] **Step 3: Verify desktop and mobile behavior in a browser**

At desktop width verify both stage titles, delegated DVOL, waiting shape, and backend preflight are simultaneously visible. At 390px verify one-column layout, no horizontal overflow, and that current values and requirements remain visible.

- [ ] **Step 4: Build and deploy the TESTNET-locked package**

Run `deploy/build-cloud-package.ps1`, upload the resulting archive, and run `sudo bash deploy/cloud/install.sh`. Preserve the cloud database carried over by the installer.

- [ ] **Step 5: Verify production endpoints and live semantics**

Verify `/`, `/signals`, `/signal-log`, `/returns`, `/status`, and `/api/entry_conditions` return HTTP 200; `/status` reports `env=testnet`; and the live page states:

- Stage 1: DVOL delegated, HV/CCI/config passed, PUT/CALL shape waiting, no `OPEN_REQUEST` received.
- Stage 2: waiting for TradingView and preflight rejection because backend DVOL is below L1.

- [ ] **Step 6: Commit any verification-only test adjustments**

Only if a stale structural assertion requires updating:

```bash
git add web/backend/tests web/frontend/tests
git commit -m "test: cover two-stage signal dashboard"
```

