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

test("force-close warning explains the grouped maker-first execution", () => {
  assert.match(html, /期权最多拆成 5 批/);
  assert.match(html, /中间价 Maker → 靠近对手价 Maker → 剩余 IOC/);
  assert.match(html, /先买回卖方腿，再卖出保护买腿/);
  assert.match(html, /未成交量带入下一批/);
  assert.match(html, /BTC 永续仍使用 IOC 快速归零/);
});

test("force-close result reports the real exchange failure", () => {
  assert.match(html, /function forceCloseResultText\(p\)/);
  assert.match(html, /强平失败：/);
  assert.match(html, /item\.instrument/);
  assert.match(html, /item\.error/);
  assert.match(html, /强平完成，Deribit 实际仓位已归零/);
});
