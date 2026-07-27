import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";

const html=readFileSync(new URL("../index.html",import.meta.url),"utf8");

test("TradingView setup is a first-class page with a complete copy workflow",()=>{
  assert.match(html,/data-page="tradingview" onclick="navigateTo\('\/tradingview'\)"/);
  assert.match(html,/id="page-tradingview"/);
  assert.match(html,/TradingView 接入中心/);
  assert.match(html,/四步完成接入/);
  assert.match(html,/复制 Webhook URL/);
  assert.match(html,/复制完整 Alert JSON/);
  assert.match(html,/onclick="copyTvWebhook\(\)"/);
  assert.match(html,/onclick="copyTvMessage\(\)"/);
  assert.match(html,/id="tvStrategyName"/);
  assert.match(html,/id="tvAlertId"/);
  assert.match(html,/onclick="regenerateTvId\(\)"/);
  assert.match(html,/必须和 Pine 参数里的 strategy 完全一致/);
  assert.match(html,/"accountName": \$\{JSON\.stringify\(account\)\}/);
  assert.match(html,/"strategy": \$\{JSON\.stringify\(strategy\)\}/);
  assert.doesNotMatch(html,/"signal_id": \$\{JSON\.stringify/);
  assert.match(html,/"order_id": "\{\{strategy\.order\.id\}\}"/);
  assert.match(html,/"bar_time": "\{\{time\}\}"/);
  assert.match(html,/"side": "\{\{strategy\.order\.action\}\}"/);
  assert.match(html,/"price": "\{\{close\}\}"/);
  assert.match(html,/"size": "\{\{strategy\.order\.contracts\}\}"/);
  assert.match(html,/"marketPosition": "\{\{strategy\.market_position\}\}"/);
  assert.match(html,/"prevMarketPosition": "\{\{strategy\.prev_market_position\}\}"/);
  assert.match(html,/"tv_id": \$\{alertId\}/);
  assert.match(html,/function generateTvId\(\)/);
  assert.doesNotMatch(html,/"trade_id": \{\{plot\("trade_id"\)\}\}/);
  assert.match(html,/"sell_delta": "\{\{plot_2\}\}"/);
  assert.match(html,/"dte_min": "\{\{plot_3\}\}"/);
  assert.match(html,/"put_hedge_release_pct": "\{\{plot_10\}\}"/);
  assert.doesNotMatch(html,/\{\{plot\("/);
  assert.match(html,/订单成交和 alert\(\) 函数调用/);
});

test("experimental setup displays the server webhook secret without browser persistence",()=>{
  assert.match(html,/id="tvPassphrase" type="text" readonly/);
  assert.match(html,/\$\("tvPassphrase"\)\.value=tvSetup\.passphrase\|\|""/);
  assert.match(html,/function copyTvSecret\(\)/);
  assert.doesNotMatch(html,/id="tvSetupModal"/);
  assert.doesNotMatch(html,/localStorage\.(setItem|getItem)\([^)]*passphrase/i);
  assert.doesNotMatch(html,/sessionStorage\.(setItem|getItem)\([^)]*passphrase/i);
});

test("generated TradingView message is valid JSON before placeholder replacement",()=>{
  const start=html.indexOf("function buildTvAlertTemplate(){");
  const end=html.indexOf("function generateTvId()",start);
  const source=html.slice(start,end);
  const values={
    tvAccountName:"test_1",
    tvPassphrase:"secret",
    tvStrategyName:"BTC_OPTION_SELLER_V2",
  };
  const build=new Function("$","tvSetup","ensureTvAlertId",
    `${source};return buildTvAlertTemplate;`)(
      id=>({value:values[id]||""}),
      null,
      ()=>876241925,
    );
  const message=build();
  const parsed=JSON.parse(message);

  assert.equal(parsed.strategy,"BTC_OPTION_SELLER_V2");
  assert.equal(parsed.tv_id,876241925);
  assert.equal(parsed.sell_delta,"{{plot_2}}");
  assert.equal(parsed.put_hedge_release_pct,"{{plot_10}}");
  assert.equal(parsed.signal_id,undefined);
});
