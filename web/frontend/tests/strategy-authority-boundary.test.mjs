import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const source = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
const production = fs.readFileSync(
  new URL("../../backend/static/index.html", import.meta.url),
  "utf8",
);

test("backend indicator values are visibly diagnostic-only", () => {
  assert.match(source, /TradingView 是开仓策略的唯一判断来源/);
  assert.match(source, /后端自算数据只用于监控诊断/);
  assert.match(source, /只读诊断 ·/);
  assert.match(source, /不会再次批准或否决 TradingView 的 DVOL、HV、CCI 与布林条件/);
});

test("production dashboard keeps the authority label", () => {
  assert.equal(production.replaceAll("\r\n", "\n"), source.replaceAll("\r\n", "\n"));
});
