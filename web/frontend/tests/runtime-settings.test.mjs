import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");

test("runtime settings panel exposes review, release and side controls", () => {
  assert.match(html, /策略运行参数/);
  assert.match(html, /id="rtProgress"/);
  assert.match(html, /开仓价至 BEP 触发进度 α/);
  assert.match(html, /id="rtProgress" type="number" min="0" max="2"/);
  assert.match(html, /id="rtReviewBar"/);
  assert.match(html, /id="rtReleaseRecovery"/);
  assert.match(html, /BEP 向开仓价恢复比例 β/);
  assert.match(html, /id="rtPutOuter"/);
  assert.match(html, /id="rtCallOuter"/);
  assert.match(html, /id="rtExpiry"/);
  assert.match(html, /id="runtimeBadge"/);
  assert.match(html, /onclick="saveRuntimeSettings\(\)"/);
  assert.match(html, /onclick="clearRuntimeSettings\(\)"/);
  assert.match(html, /指定期权到期日/);
  assert.match(html, /不指定（按信号 DTE）/);
});

test("runtime side profiles are compact collapsible groups", () => {
  const sideGroups = html.match(/<details class="runtime-side-group">/g) ?? [];
  assert.equal(sideGroups.length, 2);
  assert.match(html, /class="runtime-trigger-grid"/);
  assert.match(html, /class="runtime-review-grid"/);
  assert.match(html, /class="runtime-param-card alpha"/);
  assert.match(html, /class="runtime-param-card beta"/);
  assert.match(html, /class="tv-field-grid runtime-side-grid"/);
  assert.match(html, /\.runtime-trigger-grid\{display:grid;grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/);
  assert.match(html, /\.runtime-side-grid\{display:grid;grid-template-columns:repeat\(2,minmax\(0,1fr\)\)/);
  assert.doesNotMatch(html, /<details class="runtime-side-group" open/);
});

test("runtime settings wire to /api/runtime_settings and load on boot", () => {
  assert.match(html, /async function loadRuntimeSettings\(\)/);
  assert.match(html, /async function saveRuntimeSettings\(\)/);
  assert.match(html, /async function clearRuntimeSettings\(\)/);
  assert.match(html, /\/api\/runtime_settings/);
  assert.match(html, /bep_activation_progress/);
  assert.match(html, /forced_expiry_ts/);
  assert.match(html, /review_bar_minutes/);
  assert.match(html, /release_recovery_pct/);
  assert.match(html, /激活系数 α 必须在 \[0,2\] 之间/);
  assert.match(html, /hedge_profiles/);
  // 启动时加载一次当前设置
  assert.match(html, /loadRuntimeSettings\(\);refresh\(\)/);
});

test("hedge diagnostics follow the configured review bar and history is account scoped", () => {
  assert.match(html, /d\.review_bar_minutes/);
  assert.match(html, /p\.review_bar_minutes/);
  assert.doesNotMatch(html, /<span>3分钟收盘<\/span>/);
  assert.match(html, /id="pAccountBadge"/);
  assert.match(html, /data\.account/);
});
