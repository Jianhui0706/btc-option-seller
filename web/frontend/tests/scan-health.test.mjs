import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";

const html=readFileSync(new URL("../index.html",import.meta.url),"utf8");

test("dashboard separates scan attempts, completed scans, and quote degradation",()=>{
  assert.match(html,/scan\.last_started_ts/);
  assert.match(html,/scan\.last_completed_ts/);
  assert.match(html,/scan\.last_healthy_ts/);
  assert.match(html,/scan\.state==="degraded"/);
  assert.match(html,/scan\.quote_issues/);
  assert.match(html,/完成 .*盘口降级/);
  assert.doesNotMatch(html,/const scanPct=age==null/);
});

test("dashboard distinguishes execution, scan delay, and Deribit scan errors",()=>{
  assert.match(html,/status\.scan\|\|\{\}/);
  assert.match(html,/status\.execution\|\|\{\}/);
  assert.match(html,/scan\.last_error_kind==="deribit"/);
  assert.match(html,/Deribit 扫描失败/);
  assert.match(html,/扫描任务异常/);
  assert.match(html,/独立扫描延迟/);
  assert.match(html,/正在按信号执行，风险扫描独立运行/);
  assert.doesNotMatch(html,/stale\?"扫描状态异常"/);
  assert.doesNotMatch(html,/stale\?"请检查后端与 Deribit 连接"/);
});

test("dashboard explains transient risk recovery instead of a permanent pause",()=>{
  assert.match(html,/status\.risk_auto_resume_state/);
  assert.match(html,/瞬时风控已暂停新开仓；完整健康扫描后会自动恢复/);
  assert.match(html,/status\.trading_pause_reason/);
  assert.match(html,/item\.reject_reason/);
  assert.match(html,/REVERSE_OPEN_SKIPPED:"反向信号平仓后，新开仓因交易暂停被跳过"/);
  assert.match(html,/REJECT_TRADING_DISABLED:"交易开关已关闭或强平锁定"/);
  assert.match(html,/TRADING_AUTO_RESUMED/);
});
