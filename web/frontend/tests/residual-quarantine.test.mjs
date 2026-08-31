import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const html = await readFile(new URL("../index.html", import.meta.url), "utf8");

test("residual long positions are visibly quarantined", () => {
  assert.match(html, /RESIDUAL_LONG_QUARANTINED:/);
  assert.match(html, /p\.quarantined/);
  assert.match(html, /o\.quarantined/);
});

test("residual positions expose an explicit operator decision", () => {
  assert.match(html, /residual_decision_required/);
  assert.match(html, /retryResidual/);
  assert.match(html, /quarantineResidual/);
  assert.match(html, /control\/residual\/retry/);
  assert.match(html, /control\/residual\/quarantine/);
});

test("quarantined residue is excluded from active strategy hedge controls", () => {
  assert.match(html, /residual_decision_required/);
  assert.match(html, /已确认隔离/);
});
