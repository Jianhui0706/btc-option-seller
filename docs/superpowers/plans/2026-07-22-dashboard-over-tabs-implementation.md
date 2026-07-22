# 策略总览移至四栏导航上方 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将共享策略总览和五项核心指标移动到四栏导航上方，并让四栏导航在滚动后独立吸顶。

**Architecture:** 保持当前单页四路由和数据渲染函数不变，只重新排列 `web/frontend/index.html` 的共享壳层 DOM。新增一个独立的 `page-switcher` 吸顶容器承载四栏导航和安全操作；Vite 构建继续把源页面镜像到后端静态页面。

**Tech Stack:** HTML5、CSS3、原生 JavaScript、Node.js `node:test`、Vite、FastAPI、pytest

## Global Constraints

- 最终顺序必须是：系统状态栏 → 策略仪表板 → 五项核心指标 → 四栏切换 → 当前栏目内容。
- 总览卡和五项指标正常滚动；四栏导航独立吸顶。
- 交易开关、全账户强平与移动操作菜单必须始终可达。
- 不修改指标口径、TradingView 信号、策略执行、风控、数据库或 Pine 脚本。
- 390px、900px 和桌面宽度不得产生横向溢出。

---

### Task 1: 固定共享总览与四栏导航的 DOM 顺序

**Files:**
- Modify: `web/frontend/tests/four-tab-dashboard.test.mjs`
- Modify: `web/frontend/index.html`

**Interfaces:**
- Consumes: `.dashboard-hero`、`.global-metrics`、`.reference-tabs` 现有唯一元素。
- Produces: `.dashboard-hero` 和 `.global-metrics` 的源码位置均早于 `.reference-tabs`。

- [ ] **Step 1: 写入失败的顺序测试**

```js
test("shared overview appears before the four-tab switcher", () => {
  const hero = html.indexOf('class="dashboard-hero"');
  const metrics = html.indexOf('class="global-metrics"');
  const tabs = html.indexOf('class="main-nav reference-tabs"');
  assert.ok(hero >= 0 && metrics > hero && tabs > metrics);
  assert.match(html, /class="page-switcher"/);
});
```

- [ ] **Step 2: 运行测试并确认先失败**

Run: `node --test web/frontend/tests/four-tab-dashboard.test.mjs`

Expected: FAIL，因为当前 `.reference-tabs` 位于总览和指标之前，且没有 `.page-switcher`。

- [ ] **Step 3: 最小化调整 HTML 结构**

将现有 `.dashboard-hero` 和 `.global-metrics` 从 `</header>` 之后移动到 `.topbar-primary` 所在系统栏之后、四栏导航之前；把现有 `.topbar-command` 包入：

```html
<div class="page-switcher">
  <div class="topbar-command">
    <nav class="main-nav reference-tabs">...</nav>
    <div class="spacer"></div>
    <div class="actions">...</div>
  </div>
</div>
```

不得复制任何带 `id` 的节点；只移动现有节点。

- [ ] **Step 4: 运行测试并确认通过**

Run: `node --test web/frontend/tests/four-tab-dashboard.test.mjs`

Expected: 所有测试 PASS。

- [ ] **Step 5: 提交 DOM 顺序变更**

```powershell
git add -- web/frontend/index.html web/frontend/tests/four-tab-dashboard.test.mjs
git commit -m "refactor: place dashboard overview above tabs"
```

### Task 2: 让四栏导航独立吸顶并保持安全入口可达

**Files:**
- Modify: `web/frontend/tests/four-tab-dashboard.test.mjs`
- Modify: `web/frontend/index.html`

**Interfaces:**
- Consumes: Task 1 产生的 `.page-switcher`。
- Produces: `.page-switcher` 的 `position:sticky;top:0;z-index:20` CSS 契约。

- [ ] **Step 1: 写入失败的吸顶和安全入口测试**

```js
test("tab switcher sticks independently below the scrolling overview", () => {
  assert.match(html, /\.topbar\{[^}]*position:static/);
  assert.match(html, /\.page-switcher\{[^}]*position:sticky;top:0;z-index:20/);
  assert.match(html, /@media\(max-width:1120px\)[\s\S]*\.mobile-only\{display:grid\}/);
  assert.match(html, /onclick="openForceDialog\(\)"/);
});
```

- [ ] **Step 2: 运行测试并确认先失败**

Run: `node --test web/frontend/tests/four-tab-dashboard.test.mjs`

Expected: FAIL，因为 `.topbar` 当前吸顶且 `.page-switcher` 尚无样式。

- [ ] **Step 3: 实现独立吸顶样式**

将系统品牌栏改为普通文档流，并给导航容器添加独立背景、模糊和边框：

```css
.topbar{position:static;background:transparent;border-bottom:1px solid rgba(255,255,255,.07)}
.page-switcher{position:sticky;top:0;z-index:20;margin-bottom:18px;background:rgba(7,10,15,.92);backdrop-filter:blur(22px);border-top:1px solid rgba(255,255,255,.045);border-bottom:1px solid rgba(255,255,255,.07)}
.page-switcher .topbar-command{border-top:0}
```

保留 `max-width:1120px` 下 `.mobile-only{display:grid}` 和桌面安全按钮的现有行为。

- [ ] **Step 4: 运行前端全套测试**

Run: `node --test web/frontend/tests/*.test.mjs`

Expected: 现有 12 项加新增测试全部 PASS。

- [ ] **Step 5: 构建后端静态镜像并提交**

```powershell
npm.cmd run build --prefix web/frontend
git add -- web/frontend/index.html web/frontend/tests/four-tab-dashboard.test.mjs web/backend/static/index.html
git commit -m "style: make four-tab navigation sticky"
```

### Task 3: 回归、视觉验收与云端更新

**Files:**
- Verify: `web/frontend/index.html`
- Verify: `web/backend/static/index.html`
- Verify: `web/backend/tests`
- Build artifact: `dist/btc-option-seller-cloud-43-160-202-151.tar.gz`

**Interfaces:**
- Consumes: Task 2 的最终静态页面。
- Produces: 通过验证的测试网云端页面。

- [ ] **Step 1: 运行完整自动化验证**

```powershell
node --test web/frontend/tests/*.test.mjs
python -m pytest -q web/backend/tests
npm.cmd run build --prefix web/frontend
git diff --check
```

Expected: 前端与后端零失败，Vite 构建 exit 0，`git diff --check` 无错误。

- [ ] **Step 2: 浏览器验证三种宽度**

使用浏览器控制技能检查桌面默认宽度、900×800 和 390×844：

- 总览与五项指标出现在四栏导航之前。
- 滚动后 `.page-switcher` 保持在视口顶部。
- 900px 和 390px 显示移动操作菜单。
- `document.documentElement.scrollWidth === document.documentElement.clientWidth`。
- 四个路由 `/`、`/signals`、`/signal-log`、`/returns` 均显示正确栏目。

- [ ] **Step 3: 构建并部署测试网包**

```powershell
.\deploy\build-cloud-package.ps1
```

上传 `dist/btc-option-seller-cloud-43-160-202-151.tar.gz`，执行现有 `deploy/cloud/install.sh`；安装脚本必须继续显示已继承旧版策略数据库。

- [ ] **Step 4: 验证公网状态**

对以下路由执行 HTTPS GET 并期望 200：

```text
/
/signals
/signal-log
/returns
/status
```

`/status` 必须保持 `env=testnet`，并且不得由部署过程改变 `trading_enabled` 或 `force_close_active`。
