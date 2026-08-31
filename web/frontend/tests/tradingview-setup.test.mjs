import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";

const html=readFileSync(new URL("../index.html",import.meta.url),"utf8");

test("TradingView setup accepts both PUT and CALL through one generic Alert",()=>{
  const page=html.split('id="page-tradingview"')[1].split('id="page-active-signals"')[0];

  assert.match(page,/TradingView 接入中心/);
  assert.match(page,/id="tvStrategyName"/);
  assert.match(page,/id="tvAlertId"/);
  assert.doesNotMatch(page,/id="tvSignalSide"/);
  assert.match(page,/一个 Alert 可同时接收 PUT 与 CALL/);
  assert.match(page,/id="tvPassphrase"/);
  assert.match(page,/复制 Webhook URL/);
  assert.match(page,/复制完整 Alert JSON/);
  assert.match(page,/策略 → 订单成交/);
  assert.doesNotMatch(page,/id="tvAccountName"/);
  assert.doesNotMatch(page,/订单成交和 alert\(\) 函数调用/);
});

test("generated TradingView message leaves direction to each order signal",()=>{
  const start=html.indexOf("function buildTvAlertTemplate(){");
  const end=html.indexOf("function generateTvId()",start);
  const source=html.slice(start,end);
  const values={
    tvPassphrase:"super-secret",
    tvStrategyName:"BTC期权卖方策略 V2",
  };
  const build=new Function("$","tvSetup","ensureTvAlertId",
    `${source};return buildTvAlertTemplate;`)(
      id=>({value:values[id]||""}),
      null,
      ()=>"source-btc-v2",
    );

  assert.deepEqual(JSON.parse(build()),{
    schema_version:"2",
    passphrase:"super-secret",
    source_id:"source-btc-v2",
    source_name:"BTC期权卖方策略 V2",
    signal_code:"{{strategy.order.id}}",
    explicit_action:"{{strategy.order.alert_message}}",
    previous_position:"{{strategy.prev_market_position}}",
    market_position:"{{strategy.market_position}}",
    symbol:"{{ticker}}",
    timeframe:"{{interval}}",
    bar_time:"{{time}}",
    event_time:"{{timenow}}",
  });
});

test("source identity is generated automatically and the secret is not persisted",()=>{
  assert.match(html,/function generateTvId\(\)/);
  assert.match(html,/globalThis\.crypto\?\.randomUUID/);
  assert.match(html,/\$\("tvPassphrase"\)\.value=tvSetup\.passphrase\|\|""/);
  assert.doesNotMatch(html,/localStorage\.(setItem|getItem)\([^)]*passphrase/i);
  assert.doesNotMatch(html,/sessionStorage\.(setItem|getItem)\([^)]*passphrase/i);
});
