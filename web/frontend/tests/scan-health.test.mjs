import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";

const html=readFileSync(new URL("../index.html",import.meta.url),"utf8");

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
  assert.match(html,/REVERSE_OPEN_SKIPPED/);
  assert.match(html,/TRADING_AUTO_RESUMED/);
});
