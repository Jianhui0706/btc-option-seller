import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";

const frontend = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const deployed = readFileSync(
  new URL("../../backend/static/index.html", import.meta.url), "utf8",
);

test("expiry lifecycle is visible and does not claim a close fill", () => {
  for (const html of [frontend, deployed]) {
    assert.match(html, /SETTLEMENT_PENDING:"等待到期结算"/);
    assert.match(html, /SETTLED:"已到期结算"/);
    assert.match(html, /EXPIRY_SETTLED:"到期结算完成"/);
    assert.match(html, /由 Deribit 到期结算，没有发送平仓订单/);
    assert.match(html, /选择后立即使用，并持有至结算/);
  }
});
