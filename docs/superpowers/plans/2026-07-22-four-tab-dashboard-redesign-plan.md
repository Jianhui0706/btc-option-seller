# Four-Tab Dark Dashboard Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the existing dark dashboard into the teacher-requested four-tab workflow—仓位、活跃信号、信号日志、仓位日志—without changing trading behavior or backend data semantics.

**Architecture:** Keep the single-file HTML/CSS/JavaScript frontend and existing API calls. Introduce a shared dashboard header and five global metrics, split existing DOM sections across four route-backed page views, and preserve the current render functions by retaining their element IDs. Add one FastAPI route for `/signal-log` and use CSS-only responsive table/card behavior.

**Tech Stack:** HTML5, CSS custom properties/grid, vanilla JavaScript, Node.js built-in test runner, Vite, FastAPI, pytest.

## Global Constraints

- Four navigation labels and order are exactly: 仓位、活跃信号、信号日志、仓位日志.
- Retain the existing dark visual system; borrow the reference page's spacing, KPI cards, horizontal tabs, and single-content-panel hierarchy.
- Do not change API payloads, Pine, signal decisions, risk controls, order execution, or database semantics.
- Preserve IDs consumed by existing JavaScript render functions unless the same task updates every reference and test.
- Connection reports remain blue, accepted OPEN signals green, accepted EXIT signals cyan, rejected signals red, and pending signals amber.
- Strong-close and trading-toggle controls remain globally accessible and keep their existing confirmation behavior.
- Preserve unrelated dirty-worktree changes.

---

### Task 1: Four routes and navigation contract

**Files:**
- Create: `web/frontend/tests/four-tab-dashboard.test.mjs`
- Modify: `web/backend/tests/test_dashboard_pages.py`
- Modify: `web/backend/src/app.py`
- Modify: `web/frontend/index.html`

**Interfaces:**
- Produces routes `/`, `/signals`, `/signal-log`, `/returns` and page keys `positions`, `active-signals`, `signal-log`, `position-log`.

- [ ] **Step 1: Add failing frontend navigation tests**

Assert four `.nav-btn` elements exist in the exact Chinese order, point to the four routes, and four page containers exist with IDs `page-positions`, `page-active-signals`, `page-signal-log`, `page-position-log`.

- [ ] **Step 2: Add failing backend route test**

Extend the route assertion to require `("/signal-log", "GET")`.

- [ ] **Step 3: Run focused tests and verify RED**

```powershell
node --test tests/four-tab-dashboard.test.mjs
python -m pytest tests/test_dashboard_pages.py -q
```

Expected: failures for the missing fourth tab, page IDs, and route.

- [ ] **Step 4: Implement route and navigation skeleton**

Add the FastAPI dashboard route and change the nav to:

```html
<button class="nav-btn" data-page="positions" onclick="navigateTo('/')">仓位</button>
<button class="nav-btn" data-page="active-signals" onclick="navigateTo('/signals')">活跃信号</button>
<button class="nav-btn" data-page="signal-log" onclick="navigateTo('/signal-log')">信号日志</button>
<button class="nav-btn" data-page="position-log" onclick="navigateTo('/returns')">仓位日志</button>
```

Update `currentPage()` and `renderRoute()` to map paths to the four page keys and titles.

- [ ] **Step 5: Re-run focused tests and verify GREEN**

---

### Task 2: Shared header and five global KPIs

**Files:**
- Modify: `web/frontend/tests/four-tab-dashboard.test.mjs`
- Modify: `web/frontend/index.html`

**Interfaces:**
- Consumes existing status, account, trades, signal, and performance payloads already loaded by `refresh()`.
- Produces global IDs `gEquity`, `gAvailable`, `gPositions`, `gSignals`, `gTodayPnl`.

- [ ] **Step 1: Add failing shared-shell tests**

Assert a single `dashboard-hero`, one `global-metrics` section, exactly five global metric cards, and the five IDs above outside all page views.

- [ ] **Step 2: Run the frontend test and verify RED**

- [ ] **Step 3: Implement the shared shell**

Move the repeated page identity into one dark hero card above the navigation. Add the five global KPI cards and retain the global refresh, trading toggle, and force-close buttons.

- [ ] **Step 4: Populate global KPIs**

Add `renderGlobalMetrics(status, account, trades, signals, performance)` and call it from `refresh()`. Use account equity/available funds, current active parent count, the existing open/total signal summary, and today's realized PnL.

- [ ] **Step 5: Re-run the frontend test and verify GREEN**

---

### Task 3: Migrate existing content into four page views

**Files:**
- Modify: `web/frontend/tests/four-tab-dashboard.test.mjs`
- Modify: `web/frontend/tests/signal-activity.test.mjs`
- Modify: `web/frontend/index.html`

**Interfaces:**
- Keeps all existing render-target IDs including `activeTrades`, `positionsTable`, `condRows`, `tvSignals`, `pnlChart`, `pnlTradesTable`, `eventTimeline`, `tradesTable`, and `hedgesTable`.

- [ ] **Step 1: Add failing content-ownership tests**

Assert each required ID occurs once and is contained by its designated page:

- 仓位: `activeTrades`, risk state, Deribit positions.
- 活跃信号: condition board and active/pending signal summary.
- 信号日志: webhook endpoint and `tvSignals` accordion.
- 仓位日志: performance KPIs/chart, trade table, event/trade/hedge execution records.

- [ ] **Step 2: Run tests and verify RED**

- [ ] **Step 3: Reorder DOM without changing data semantics**

Move existing panels into the four page containers. Keep the condition board in active signals; place the activity stream and webhook panel in signal log; place the performance chart and all historic execution tabs in position log.

- [ ] **Step 4: Add active-signal summary filtering**

Render a compact summary of currently pending or most recent real OPEN signals in the active-signals page, while leaving connection reports exclusively in signal log.

- [ ] **Step 5: Preserve signal color and accordion contracts**

Update signal tests only where container ownership changes; retain secret filtering, escaped reject reasons, single-open accordion behavior, and the open/received count formula.

- [ ] **Step 6: Re-run focused frontend tests and verify GREEN**

---

### Task 4: Dark reference-inspired styling and responsive behavior

**Files:**
- Modify: `web/frontend/tests/four-tab-dashboard.test.mjs`
- Modify: `web/frontend/index.html`

**Interfaces:**
- Produces shared classes `dashboard-hero`, `global-metrics`, `reference-tabs`, `page-stage`, and responsive rules at 1100px and 700px.

- [ ] **Step 1: Add failing style-contract tests**

Assert the four-column desktop nav, five-column KPI layout, centered maximum-width content, main stage panel, tablet wrapping, two-column mobile KPI layout, and compact four-tab mobile navigation.

- [ ] **Step 2: Run test and verify RED**

- [ ] **Step 3: Implement desktop and tablet styling**

Use the existing dark tokens, 12–16px radii, clear panel borders, stronger card hierarchy, consistent 14–20px gaps, and reference-style table headers/badges. Avoid purple gradients.

- [ ] **Step 4: Implement mobile styling**

Use two-column KPIs, four equal compact tabs, one-column page content, card-style record rows where practical, and keep dangerous actions accessible through the existing mobile action modal.

- [ ] **Step 5: Re-run frontend tests and verify GREEN**

---

### Task 5: Full verification, visual QA, commit, and cloud deployment

**Files:**
- Generated: `web/backend/static/index.html`
- Generated: `dist/btc-option-seller-cloud-43-160-202-151.tar.gz`

- [ ] **Step 1: Run all frontend tests and build**

```powershell
node --test tests/*.test.mjs
npm.cmd run build
```

- [ ] **Step 2: Run the complete backend suite**

```powershell
python -m pytest -q
```

- [ ] **Step 3: Start the local backend or static preview and perform visual QA**

Capture desktop and mobile screenshots for all four routes. Verify no overlap, clipped labels, inaccessible controls, horizontal page overflow, or lost content.

- [ ] **Step 4: Inspect and commit only scoped files**

Run `git diff --check`, inspect route/frontend/test diffs, and commit with:

```text
feat: redesign dashboard around four trading tabs
```

- [ ] **Step 5: Build and deploy through the existing testnet-only package workflow**

Use `deploy/build-cloud-package.ps1`, upload through the `ubuntu` SSH account, run the DB-preserving installer with sudo, and avoid changing trading state.

- [ ] **Step 6: Verify cloud routes and live state**

Require HTTPS 200 for `/`, `/signals`, `/signal-log`, and `/returns`; verify TESTNET, the pre-deployment `trading_enabled` value, four nav labels, and all page IDs in the live HTML.

- [ ] **Step 7: Remove only the exact verified deployment artifacts from `/tmp`**

