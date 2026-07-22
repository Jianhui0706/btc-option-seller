# TradingView Webhook Activity Feed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在信号中心以紧凑可下拉时间流展示每次 TradingView 心跳、开仓信号、退出信号及后端处理结果。

**Architecture:** 使用独立 `pine_heartbeats` 表持久化已验证的 `STATUS`，同时继续更新现有最新心跳状态。`GET /api/signals` 在 Python 中合并心跳与现有交易信号，前端将统一 items 渲染为可展开手风琴列表，展开状态在 5 秒刷新之间保留。

**Tech Stack:** Python 3.12, FastAPI, aiosqlite/SQLite, pytest/pytest-asyncio, 原生 HTML/CSS/JavaScript, Node test runner, Vite, PowerShell + systemd/Caddy testnet 部署。

## Global Constraints

- `STATUS` 只证明链路连通，不得调用 `queue.open()` 或修改交易开关。
- 任何已验证 `OPEN_REQUEST` 事件都用绿色表示 TradingView 开仓条件成立；后端拒绝另用红色结果徽标表示。
- 只有通过 passphrase 验证的心跳可进入正常活动流；passphrase 不得写库、返回 API 或渲染到前端。
- 心跳历史最多保留 500 条；API 默认返回 100 条，`limit` 范围为 1–500。
- 活动记录默认单行收起，同时只展开一条，自动刷新后保留展开项。
- 桌面至少同屏显示 8 条收起记录，宽度不超过 700px 时至少显示 5 条。
- 不改变 Pine 条件、开仓队列或现有风控链路。
- 保留并避开覆盖当前工作区中与本功能无关的用户改动。

---

### Task 1: Persist authenticated Pine heartbeats

**Files:**
- Modify: `web/backend/src/store/schema.sql`
- Modify: `web/backend/src/store/repo.py`
- Modify: `web/backend/tests/test_db_schema.py`
- Create: `web/backend/tests/test_heartbeat_history.py`

**Interfaces:**
- Produces: `Repo.record_pine_heartbeat(payload: dict, retention: int = 500) -> None`
- Produces: `Repo.recent_pine_heartbeats(limit: int) -> list[dict]`
- Persists: `pine_heartbeats(id, received_at, bar_ts, strategy, payload_json)` and `system_state['pine_status']` in one commit.

- [ ] **Step 1: Write failing schema and repository tests**

Add this schema assertion to `test_db_schema.py`:

```python
heartbeat_rows = await (await conn.execute(
    "PRAGMA table_info(pine_heartbeats)")).fetchall()
heartbeat_cols = {r[1] for r in heartbeat_rows}
assert {"id", "received_at", "bar_ts", "strategy", "payload_json"} <= heartbeat_cols
```

Create `test_heartbeat_history.py` with tests that call `record_pine_heartbeat()` 503 times, assert exactly 500 rows remain, assert the oldest retained `bar_ts` is `3`, assert `system_state.pine_status` equals the newest payload, and assert `recent_pine_heartbeats(2)` returns bar timestamps `[502, 501]` without a `passphrase` key.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
cd web/backend
python -m pytest -q tests/test_db_schema.py tests/test_heartbeat_history.py
```

Expected: failure because `pine_heartbeats`, `record_pine_heartbeat`, and `recent_pine_heartbeats` do not exist.

- [ ] **Step 3: Add the table and repository methods**

Append this table and index to `schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS pine_heartbeats (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at  INTEGER NOT NULL,
  bar_ts       INTEGER,
  strategy     TEXT,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pine_heartbeats_received
  ON pine_heartbeats(received_at DESC);
```

Add methods to `Repo` that remove `passphrase`, supply `received_at=now_ms()` when absent, insert the safe JSON payload, upsert `system_state.pine_status`, prune with:

```sql
DELETE FROM pine_heartbeats
WHERE id NOT IN (
  SELECT id FROM pine_heartbeats ORDER BY received_at DESC, id DESC LIMIT ?
)
```

and call `commit()` exactly once after all three statements. `recent_pine_heartbeats()` must clamp the caller-provided limit to 1–500 and decode invalid JSON as `{}` without failing the list.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the same pytest command. Expected: all focused tests pass.

- [ ] **Step 5: Commit the heartbeat persistence slice**

```powershell
git add web/backend/src/store/schema.sql web/backend/src/store/repo.py web/backend/tests/test_db_schema.py web/backend/tests/test_heartbeat_history.py
git commit -m "feat: persist authenticated Pine heartbeats"
```

### Task 2: Route STATUS through the atomic history writer

**Files:**
- Modify: `web/backend/src/webhook.py`
- Modify: `web/backend/tests/test_dashboard_pages.py`

**Interfaces:**
- Consumes: `Repo.record_pine_heartbeat(payload: dict, retention: int = 500)`
- Preserves: `_handle_status(ctx, body, expected) -> {'status': 'ok', 'bar_ts': ...}`

- [ ] **Step 1: Write failing webhook behavior tests**

Add two async tests using a fake repo with `record_pine_heartbeat()` and a fake queue that records calls:

```python
result = await tradingview_webhook(valid_status_request)
assert result == {"status": "ok", "bar_ts": 123}
assert repo.heartbeat["event"] == "STATUS"
assert "passphrase" not in repo.heartbeat
assert queue.commands == []
```

For a mismatched passphrase, assert the response is `{"status": "rejected", "reason": "auth"}`, `repo.heartbeat is None`, and the queue stays empty.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
cd web/backend
python -m pytest -q tests/test_dashboard_pages.py -k "status"
```

Expected: failure because `_handle_status()` still calls `set_state()` instead of `record_pine_heartbeat()`.

- [ ] **Step 3: Replace the STATUS persistence call**

Keep authentication and Pydantic parsing unchanged. Build the safe payload with `model_dump(exclude={"passphrase"})`, add `received_at`, then call:

```python
await ctx.repo.record_pine_heartbeat(payload)
```

Do not insert a signal, emit an OPEN command, or catch database errors as success.

- [ ] **Step 4: Run focused and webhook regression tests**

```powershell
cd web/backend
python -m pytest -q tests/test_dashboard_pages.py tests/test_webhook_payload.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit the STATUS routing slice**

```powershell
git add web/backend/src/webhook.py web/backend/tests/test_dashboard_pages.py
git commit -m "feat: record Pine heartbeat history"
```

### Task 3: Return a unified chronological activity API

**Files:**
- Modify: `web/backend/src/api.py`
- Modify: `web/backend/tests/test_dashboard_pages.py`

**Interfaces:**
- Consumes: `Repo.recent_pine_heartbeats(limit: int) -> list[dict]`
- Produces: `/api/signals` items with stable `key`, `kind`, `event`, `received_at`, `status`, `reject_reason`, and `payload` fields.
- Produces summary keys: `total`, `heartbeats`, `signal_total`, `open_signals`, `exit_signals`, `accepted`, `rejected`, `pending`.

- [ ] **Step 1: Extend the existing API test to define the response contract**

Insert two heartbeat rows around one accepted and one rejected TradingView signal, call `signal_feed(limit=20)`, and assert:

```python
assert [item["kind"] for item in result["items"]] == [
    "HEARTBEAT", "OPEN_SIGNAL", "HEARTBEAT", "EXIT_SIGNAL"
]
assert result["items"][1]["key"] == "signal:TV-OPEN-1"
assert result["items"][1]["status"] == "REJECTED"
assert result["items"][1]["reject_reason"] == "REJECT_DVOL_BELOW_ENTRY"
assert result["summary"] == {
    "total": 4,
    "heartbeats": 2,
    "signal_total": 2,
    "open_signals": 1,
    "exit_signals": 1,
    "accepted": 1,
    "rejected": 1,
    "pending": 0,
}
```

Also assert heartbeat items contain no `passphrase` and a corrupt heartbeat `payload_json` becomes `{}` rather than a 500 response.

- [ ] **Step 2: Run the API test and verify RED**

```powershell
cd web/backend
python -m pytest -q tests/test_dashboard_pages.py::test_signal_feed_exposes_only_real_tradingview_signals
```

Expected: failure because the response contains only signals and has no `kind`/heartbeat summary.

- [ ] **Step 3: Implement the merge without changing trading semantics**

Map signals as:

```python
item["key"] = f"signal:{item['signal_id']}"
item["kind"] = "OPEN_SIGNAL" if item["event"] == "OPEN_REQUEST" else "EXIT_SIGNAL"
```

Map heartbeat rows as:

```python
{
    "key": f"heartbeat:{row['id']}",
    "kind": "HEARTBEAT",
    "event": "STATUS",
    "received_at": row["received_at"],
    "status": "CONNECTED",
    "reject_reason": None,
    "payload": row["payload"],
}
```

Fetch up to `safe_limit` rows from each source, combine, sort by `(received_at, key)` descending, then slice to `safe_limit`. Compute summary from the returned slice while excluding heartbeats from accepted/rejected/pending counts. Set `last_received_at` from the first returned item.

- [ ] **Step 4: Run API and full backend tests**

```powershell
cd web/backend
python -m pytest -q tests/test_dashboard_pages.py
python -m pytest -q
```

Expected: both commands pass with zero failures.

- [ ] **Step 5: Commit the API slice**

```powershell
git add web/backend/src/api.py web/backend/tests/test_dashboard_pages.py
git commit -m "feat: merge heartbeat and trading signal activity"
```

### Task 4: Render the compact accordion activity stream

**Files:**
- Modify: `web/frontend/index.html`
- Create: `web/frontend/tests/signal-activity.test.mjs`

**Interfaces:**
- Consumes: `/api/signals` unified `items` and expanded summary.
- Produces: `toggleSignalActivity(key: string)`, `signalActivityCard(item: object)`, `renderSignals(data: object)`.
- Maintains: module-level `expandedSignalKey` across 5-second refresh renders.

- [ ] **Step 1: Write failing frontend contract tests**

Create a Node test that reads `index.html` and asserts the source contains:

```javascript
assert.match(html, /let expandedSignalKey=null/);
assert.match(html, /function toggleSignalActivity\(key\)/);
assert.match(html, /kind==="HEARTBEAT"/);
assert.match(html, /TradingView 已连接 · 不触发开仓/);
assert.match(html, /class="activity-stream"/);
assert.match(html, /REJECT_DVOL_BELOW_ENTRY:"DVOL 低于开仓门槛"/);
assert.match(html, /aria-expanded=/);
```

Assert CSS includes `max-height`, `overflow-y:auto`, heartbeat/open/exit color classes, and a `@media(max-width:700px)` activity-stream rule.

- [ ] **Step 2: Run the Node test and verify RED**

```powershell
cd web/frontend
node --test tests/signal-activity.test.mjs
```

Expected: assertions fail because the accordion activity UI does not exist.

- [ ] **Step 3: Implement compact row state and safe detail rendering**

Initialize state with:

```javascript
let model=null,eventLevel="ALL",refreshing=false,expandedSignalKey=null;
```

Implement `toggleSignalActivity(key)` so clicking the same key sets `null`, clicking another key replaces it, and then calls `renderSignals(model.signals)`. Build each item as an `<article class="activity-item ..." data-key="...">` with a `<button class="activity-summary" aria-expanded="true|false">`; the detail element is emitted only when its key equals `expandedSignalKey`.

Use event color independently from status:

- `HEARTBEAT` → `activity-heartbeat` blue, badge `链路正常`;
- `OPEN_SIGNAL` → `activity-open` green even when `status=REJECTED`;
- `EXIT_SIGNAL` → `activity-exit` purple;
- rejected status → separate red `系统拒绝` badge.

Add a rejection map containing the exact existing codes from `monitor/events.py`, including:

```javascript
const REJECT_REASON={
  REJECT_TRADING_DISABLED:"交易开关已关闭或强平锁定",
  REJECT_EXISTING_OPTION_SHORT:"账户已有期权卖方仓位",
  REJECT_DVOL_BELOW_ENTRY:"DVOL 低于开仓门槛",
  REJECT_NO_VALID_LEGS:"没有符合要求的期权腿",
  REJECT_INSUFFICIENT_RISK_BUDGET:"账户风险预算不足",
  REJECT_BAD_PAYLOAD:"信号参数不合法",
  REJECT_DUPLICATE_SIGNAL:"重复信号"
};
```

All payload values and rejection codes must pass through `esc()` before HTML insertion. Do not show passphrase even if malformed legacy payload data contains it; destructure or clone payload and delete `passphrase` before formatting details.

- [ ] **Step 4: Implement compact scrolling CSS and responsive behavior**

Use `.activity-stream{max-height:520px;overflow-y:auto}` with collapsed summary rows no taller than approximately 58px, colored left borders, a rotating chevron for expanded rows, and inline details. Under `@media(max-width:700px)`, use `max-height:390px`, hide nonessential signal IDs from the summary, and keep at least five collapsed rows visible.

Update metric labels so heartbeat count and real signal count are visibly separate. Empty copy becomes `尚未收到 TradingView 心跳或信号`.

- [ ] **Step 5: Run frontend tests and production build**

```powershell
cd web/frontend
node --test tests/*.test.mjs
npm.cmd run build
```

Expected: all Node tests pass and Vite writes `web/backend/static/index.html` successfully.

- [ ] **Step 6: Commit source and generated frontend together**

```powershell
git add web/frontend/index.html web/frontend/tests/signal-activity.test.mjs web/backend/static/index.html
git commit -m "feat: show compact TradingView activity stream"
```

### Task 5: Full regression, security audit, and cloud deployment

**Files:**
- Verify: `web/backend/tests/`
- Verify: `web/frontend/tests/`
- Verify: `deploy/build-cloud-package.ps1`
- Verify: `deploy/cloud/install.sh`

**Interfaces:**
- Consumes the completed heartbeat table, webhook path, activity API, and frontend build.
- Produces a verified deployment on testnet host `43.160.202.151`.

- [ ] **Step 1: Run the complete local verification suite**

```powershell
cd web/backend
python -m pytest -q
cd ../frontend
node --test tests/*.test.mjs
npm.cmd run build
cd ../..
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/start.tests.ps1
```

Expected: zero pytest/Node failures, successful Vite build, and successful launcher integration exit code.

- [ ] **Step 2: Verify the passphrase and trade-authority boundaries**

Run repository searches and confirm:

```powershell
rg -n "passphrase" web/backend/src web/frontend/index.html
rg -n "queue\.open|record_pine_heartbeat" web/backend/src
```

Expected: API/frontend contain no rendered passphrase; `record_pine_heartbeat` is called only from authenticated STATUS handling; `queue.open` remains only on the existing validated `OPEN_REQUEST` branch.

- [ ] **Step 3: Build and upload the locked testnet package**

```powershell
.\deploy\build-cloud-package.ps1
scp -i C:\Users\jianh\.ssh\jup_tencent_ed25519 -o IdentitiesOnly=yes `
  dist\btc-option-seller-cloud-43-160-202-151.tar.gz `
  ubuntu@43.160.202.151:/tmp/btc-option-seller-activity-20260722.tar.gz
```

Expected: package build and upload succeed; build script refuses any environment other than testnet.

- [ ] **Step 4: Deploy with the existing database-preserving installer**

Over SSH, extract only to `/tmp/btc-option-seller-activity-20260722`, run:

```bash
sudo bash /tmp/btc-option-seller-activity-20260722/btc-option-seller-cloud/deploy/cloud/install.sh
```

then remove exactly the uploaded archive and extraction directory. Expected: installer reports database carry-over, valid Caddy configuration, active service, and deployment completion.

- [ ] **Step 5: Verify cloud behavior and data preservation**

Check:

```bash
systemctl is-active btc-option-seller.service
curl -fsS http://127.0.0.1:8310/status
curl -fsS 'http://127.0.0.1:8310/api/signals?limit=10'
curl -fsS http://127.0.0.1:8310/api/entry_conditions
```

Expected: service is `active`, environment is `testnet`, prior `trading_enabled` and active-trade state are preserved, activity items contain heartbeat rows without passphrase, and public HTTPS `/status` returns 200 or the configured access-control response with valid TLS.

- [ ] **Step 6: Commit any verification-only documentation adjustment**

If no documentation adjustment is necessary, make no empty commit. Otherwise stage only the explicitly reviewed documentation file and commit with `docs: document TradingView activity feed`.
