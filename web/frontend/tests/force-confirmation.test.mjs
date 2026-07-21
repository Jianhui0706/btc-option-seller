import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const html = await readFile(new URL("../index.html", import.meta.url), "utf8");

test("force-close modal does not require typed confirmation", () => {
  assert.doesNotMatch(html, /id="forceConfirm"/);
  assert.doesNotMatch(html, /输入 YES/);
  assert.doesNotMatch(html, /class="confirm-input/);
  assert.doesNotMatch(html, /forceConfirm.*value/);
});

test("force-close warning and click actions remain", () => {
  assert.match(html, /执行全账户强制平仓/);
  assert.match(html, /onclick="closeForceDialog\(\)"[^>]*>取消</);
  assert.match(html, /onclick="confirmForceClose\(\)"[^>]*>确认强平</);
  assert.match(html, /api\("\/control\/force_close\?reason=ui_manual"/);
});
