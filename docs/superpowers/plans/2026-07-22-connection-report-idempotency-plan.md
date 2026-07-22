# Connection Report Idempotency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure one visible and counted TradingView connection report per strategy and 2-hour bar, even when the alert layer triggers or delivers the same STATUS payload more than once.

**Architecture:** Pine uses an intra-runtime `lastStatusT` guard keyed by the requested 2-hour `t2` value. SQLite provides the authoritative idempotency boundary through a partial unique index on `(strategy, bar_ts)`; the repository reports whether an insert was fresh, and the webhook always acknowledges duplicates with HTTP 200.

**Tech Stack:** Pine Script v6, Python 3, FastAPI, aiosqlite/SQLite, pytest, PowerShell deployment packaging, systemd/Caddy.

## Global Constraints

- Preserve `alert.freq_all` for OPEN, EXIT and STATUS so multiple different messages on one 3-minute close are not suppressed.
- Do not change any opening, exit, threshold or order-execution condition.
- Connection report identity is `(strategy, bar_ts)` only when both values are present and strategy is non-empty.
- Duplicate STATUS requests must return HTTP 200 and must not affect history, counters or latest state.
- Existing duplicate history must retain the earliest stored row in each identity group.
- Preserve all unrelated dirty-worktree changes.
- Cloud deployment remains locked to Deribit testnet.

---

### Task 1: Pine source-level connection report guard

**Files:**
- Modify: `web/backend/tests/test_pine_alert_delivery.py`
- Modify: `pine/btc_credit_spread_v2.pine`

**Interfaces:**
- Consumes: Pine higher-timeframe timestamp `t2` and existing `newBar2h`, `heartbeat_on`, `barstate.isconfirmed` conditions.
- Produces: `varip int lastStatusT` and a guarded STATUS `alert()` invocation.

- [ ] **Step 1: Extend the Pine contract test**

Assert the source includes:

```python
assert "varip int lastStatusT = na" in source
assert "statusCanFire = na(lastStatusT) or t2 != lastStatusT" in source
assert "if barstate.isconfirmed and newBar2h and heartbeat_on and statusCanFire" in source
assert "lastStatusT := t2" in source
```

Keep the existing assertions that all OPEN, EXIT and STATUS calls use `alert.freq_all`.

- [ ] **Step 2: Run the focused test and verify RED**

Run from `web/backend`:

```powershell
python -m pytest tests/test_pine_alert_delivery.py -q
```

Expected: FAIL because `lastStatusT` and `statusCanFire` do not yet exist.

- [ ] **Step 3: Implement the minimal Pine guard**

Near `lastSigT`, add:

```pine
varip int lastStatusT = na
statusCanFire = na(lastStatusT) or t2 != lastStatusT
```

Change only the STATUS block to:

```pine
if barstate.isconfirmed and newBar2h and heartbeat_on and statusCanFire
    alert(f_status_json(), alert.freq_all)
    lastStatusT := t2
```

- [ ] **Step 4: Re-run the focused test and verify GREEN**

Expected: the Pine delivery contract test passes.

---

### Task 2: SQLite authoritative idempotency and migration

**Files:**
- Modify: `web/backend/tests/test_heartbeat_history.py`
- Modify: `web/backend/tests/test_db_schema.py`
- Modify: `web/backend/src/store/schema.sql`
- Modify: `web/backend/src/store/repo.py`

**Interfaces:**
- Consumes: `Repo.record_pine_heartbeat(payload: dict, retention: int = 500)`.
- Produces: the same method returning `bool`, where `True` means inserted and `False` means duplicate.

- [ ] **Step 1: Add failing repository idempotency tests**

Add tests that call `record_pine_heartbeat()` twice with identical non-empty `strategy` and `bar_ts`, then assert:

```python
assert first is True
assert duplicate is False
assert count == 1
```

Also assert different `bar_ts`, different strategy, and missing identity fields insert independently.

- [ ] **Step 2: Add a failing legacy migration test**

Create an old `pine_heartbeats` table containing two rows with the same `(strategy, bar_ts)`, open it through `Database.open()`, and assert only the earliest row remains and the unique index exists.

- [ ] **Step 3: Run focused storage tests and verify RED**

```powershell
python -m pytest tests/test_heartbeat_history.py tests/test_db_schema.py -q
```

Expected: FAIL because duplicates are currently inserted and no unique index exists.

- [ ] **Step 4: Add migration and partial unique index**

After the heartbeat table is created, delete only duplicate rows with valid identities, retaining `MIN(id)`, then create:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_pine_heartbeats_strategy_bar
  ON pine_heartbeats(strategy, bar_ts)
  WHERE strategy IS NOT NULL AND strategy <> '' AND bar_ts IS NOT NULL;
```

- [ ] **Step 5: Make repository insertion report freshness**

Use `INSERT OR IGNORE`, compute `fresh = cur.rowcount > 0`, and update `system_state` plus retention only when `fresh` is true. Commit and return `fresh`.

- [ ] **Step 6: Re-run focused storage tests and verify GREEN**

Expected: all heartbeat-history and schema tests pass.

---

### Task 3: Webhook duplicate acknowledgement

**Files:**
- Modify: `web/backend/tests/test_entry_conditions.py`
- Modify: `web/backend/src/webhook.py`

**Interfaces:**
- Consumes: `Repo.record_pine_heartbeat(...) -> bool`.
- Produces: STATUS response `{"status": "ok", "bar_ts": int | None, "duplicate": bool}`.

- [ ] **Step 1: Update the fake repository and add failing webhook tests**

Make `_FakeRepo.record_pine_heartbeat()` return configurable freshness. Assert a fresh response contains `duplicate: false`, while a duplicate response contains `duplicate: true`; both responses retain `status: "ok"`.

- [ ] **Step 2: Run the focused webhook tests and verify RED**

```powershell
python -m pytest tests/test_entry_conditions.py -q
```

Expected: FAIL because `_handle_status()` does not expose duplicate status.

- [ ] **Step 3: Implement the acknowledgement**

Capture repository freshness and return:

```python
fresh = await ctx.repo.record_pine_heartbeat(payload)
return {"status": "ok", "bar_ts": status.bar_ts, "duplicate": not fresh}
```

- [ ] **Step 4: Re-run the focused webhook tests and verify GREEN**

Expected: all entry-condition and STATUS webhook tests pass.

---

### Task 4: Regression verification and intentional commit

**Files:**
- Verify all modified source and tests.
- Do not stage unrelated dirty-worktree files.

- [ ] **Step 1: Run the complete backend suite**

```powershell
python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Run the frontend suite and build**

```powershell
node --test tests/*.test.mjs
npm.cmd run build
```

Expected: all frontend tests pass and Vite build succeeds.

- [ ] **Step 3: Inspect diffs and commit only scoped files**

Use `git diff --check`, inspect the Pine/schema/repository/webhook/test diffs, then stage only the files listed in Tasks 1–3 and commit with:

```text
fix: deduplicate TradingView connection reports
```

---

### Task 5: Testnet cloud deployment and live verification

**Files:**
- Generated: `web/backend/static/index.html`
- Generated package: `dist/btc-option-seller-cloud-43-160-202-151.tar.gz`

**Interfaces:**
- Consumes: existing DB-preserving `deploy/cloud/install.sh` workflow.
- Produces: updated testnet service at `https://btc-option-seller-43-160-202-151.sslip.io`.

- [ ] **Step 1: Build the deployment package**

```powershell
.\deploy\build-cloud-package.ps1
```

Expected: package build succeeds and confirms testnet-only configuration.

- [ ] **Step 2: Upload and install with the existing DB-preserving installer**

Upload the archive to one exact `/tmp` path, extract to one exact directory, run `deploy/cloud/install.sh`, and preserve `/opt/btc-option-seller/web/backend/data` through the installer.

- [ ] **Step 3: Verify service and migrated data**

Check `systemctl is-active btc-option-seller`, HTTPS `/status`, and query SQLite to confirm no duplicate valid `(strategy, bar_ts)` groups remain and the unique index exists.

- [ ] **Step 4: Verify public signal API**

Fetch `/api/signals?limit=100` and assert each connection-report `(strategy, bar_ts)` identity occurs at most once. Confirm environment remains `testnet` and trading state is unchanged.

- [ ] **Step 5: Remove only the exact temporary archive and extraction directory**

Verify the resolved `/tmp` targets before removal, then remove those exact deployment artifacts.

- [ ] **Step 6: Give the user TradingView update instructions**

Provide the updated Pine file and direct the user to replace/save the script, remove or pause the existing BTC alert, and create one new “Any alert() function call” alert on the 3-minute BTCUSDT chart with the existing webhook URL.

