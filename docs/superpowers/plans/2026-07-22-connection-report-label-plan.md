# Connection Report Label Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将信号中心的“心跳”用户文案改为“连接回报”，并在现有指标卡显示“开仓信号数 / Webhook 接收总数”。

**Architecture:** 只修改前端显示层，继续消费 `/api/signals` 已有的 `summary.open_signals`、`summary.total`、`summary.heartbeats`。后端字段、数据库结构、Webhook 鉴权和交易链路保持不变；Vite 构建后同步生成后端静态页面。

**Tech Stack:** 原生 HTML/CSS/JavaScript、Node test runner、Vite、Python/pytest、PowerShell、systemd/Caddy testnet 部署。

## Global Constraints

- 指标主值必须使用 `open_signals / total`，右侧 `total` 包含连接回报、开仓信号和退出信号。
- 指标说明必须明确“收到信号包含连接回报”。
- 保持现有六张指标卡布局，不新增卡片。
- 所有面向用户的“心跳”文案改为“连接回报”；内部 `HEARTBEAT`、`heartbeats` 和数据库名称不改。
- 不改变 Pine 条件、Webhook 鉴权、开仓队列、退出队列或风控逻辑。
- 保留当前工作区中与本任务无关的未提交改动。

---

### Task 1: Update the signal-center metric and terminology

**Files:**
- Modify: `web/frontend/tests/signal-activity.test.mjs`
- Modify: `web/frontend/index.html`
- Generated: `web/backend/static/index.html`

**Interfaces:**
- Consumes: `/api/signals.summary.open_signals`, `.total`, `.heartbeats`, `.signal_total`
- Produces: `#sTvCount` text in the exact form `${open_signals} / ${total}`
- Preserves: `kind === "HEARTBEAT"` as an internal discriminator only

- [ ] **Step 1: Write the failing frontend contract test**

Add assertions requiring the merged metric and new terminology:

```javascript
assert.match(html, /<div class="metric-label">开仓信号 \/ 收到信号<\/div>/);
assert.match(html, /\$\("sTvCount"\)\.textContent=`\$\{sum\.open_signals\|\|0\} \/ \$\{sum\.total\|\|0\}`/);
assert.match(html, /收到信号包含连接回报/);
assert.match(html, /const eventLabel=isHeartbeat\?"连接回报"/);
assert.match(html, /\$\{sum\.heartbeats\|\|0\} 回报 · \$\{sum\.signal_total\|\|0\} 信号/);
```

Add a user-copy scan which removes JavaScript comments and asserts that these old phrases do not remain in visible strings:

```javascript
assert.doesNotMatch(html, />[^<]*(?:链路心跳|Pine 心跳|TradingView 心跳)[^<]*</);
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
cd web/frontend
node --test tests/signal-activity.test.mjs
```

Expected: FAIL because the metric still displays only `signal_total` and visible copy still contains “心跳”.

- [ ] **Step 3: Implement the minimal display changes**

In the existing six-card signal metrics section, replace the third card with:

```html
<div class="metric">
  <div class="metric-label">开仓信号 / 收到信号</div>
  <div id="sTvCount" class="metric-value">0 / 0</div>
  <div class="metric-note">收到信号包含连接回报</div>
</div>
```

In the final `renderSignals(data)` implementation set:

```javascript
$("sTvCount").textContent=`${sum.open_signals||0} / ${sum.total||0}`;
```

Replace user-visible terms as follows:

- `链路心跳` → `连接回报`
- `Pine 心跳` → `Pine 连接回报`
- `TradingView 心跳` → `TradingView 连接回报`
- `等待首个心跳或信号` → `等待首个连接回报或信号`
- `尚未收到 TradingView 心跳或信号` → `尚未收到 TradingView 连接回报或信号`
- summary badge `心跳` → `回报`

Do not rename `HEARTBEAT`, `heartbeats`, `HEARTBEAT_STALE_MS`, repository methods, API keys, or database identifiers.

- [ ] **Step 4: Run focused and full frontend verification**

```powershell
cd web/frontend
node --test tests/signal-activity.test.mjs
node --test tests/*.test.mjs
npm.cmd run build
node -e "const fs=require('fs');const h=fs.readFileSync('index.html','utf8');new Function(h.match(/<script>([\\s\\S]*?)<\\/script>/)[1]);console.log('inline script syntax OK')"
```

Expected: all Node tests pass, Vite succeeds, and inline JavaScript compiles.

- [ ] **Step 5: Commit the UI slice**

Stage only the reviewed frontend source, generated static file, and activity test:

```powershell
git add web/frontend/index.html web/frontend/tests/signal-activity.test.mjs web/backend/static/index.html
git diff --cached --check
git commit -m "feat: rename heartbeat display to connection report"
```

### Task 2: Regression verification and cloud deployment

**Files:**
- Verify: `web/backend/tests/`
- Verify: `web/frontend/tests/`
- Verify: `tests/start.tests.ps1`
- Deploy: `dist/btc-option-seller-cloud-43-160-202-151.tar.gz`

**Interfaces:**
- Preserves: cloud environment `testnet`, current strategy database, `trading_enabled`, and active-trade state
- Produces: public `/signals` page containing the new label and ratio formula

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

Expected: zero failures and successful production build.

- [ ] **Step 2: Build and upload the locked testnet package**

```powershell
.\deploy\build-cloud-package.ps1
scp -i C:\Users\jianh\.ssh\jup_tencent_ed25519 -o IdentitiesOnly=yes `
  dist\btc-option-seller-cloud-43-160-202-151.tar.gz `
  ubuntu@43.160.202.151:/tmp/btc-option-seller-connection-report-20260722.tar.gz
```

Expected: package and upload succeed; deployment configuration remains testnet-only.

- [ ] **Step 3: Deploy with the database-preserving installer**

Extract into the exact directory `/tmp/btc-option-seller-connection-report-20260722`, run its `deploy/cloud/install.sh`, and wait for `btc-option-seller.service` to become active. Remove only that exact temporary directory and archive after verification.

- [ ] **Step 4: Verify cloud state and public copy**

Verify:

```text
systemctl is-active btc-option-seller.service == active
/status.env == testnet
/status.trading_enabled is preserved
/api/signals summary still contains heartbeats/open_signals/total
HTTPS /signals == 200
HTML contains “开仓信号 / 收到信号” and “收到信号包含连接回报”
HTML user-facing copy contains no “链路心跳” or “Pine 心跳”
```

- [ ] **Step 5: Report exact live ratio**

Read the deployed `/api/signals` summary and report the rendered ratio as `open_signals / total`, explicitly stating that the denominator includes connection reports.
