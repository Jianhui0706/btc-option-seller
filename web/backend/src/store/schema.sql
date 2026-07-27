-- BTC 期权卖方策略 V2 — SQLite schema（WAL 模式，单写者）

CREATE TABLE IF NOT EXISTS signals (
  signal_id     TEXT PRIMARY KEY,
  event         TEXT NOT NULL,               -- OPEN_REQUEST / EXIT_DVOL
  strategy      TEXT,
  spread_type   TEXT,                        -- PUT_CREDIT / CALL_CREDIT
  payload_json  TEXT NOT NULL,
  received_at   INTEGER NOT NULL,
  status        TEXT NOT NULL DEFAULT 'RECEIVED',   -- RECEIVED / ACCEPTED / REJECTED
  reject_reason TEXT
);

CREATE TABLE IF NOT EXISTS parent_trades (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_id       TEXT REFERENCES signals(signal_id),
  trade_id        TEXT,                        -- TradingView 交易实例 ID，供精确平仓关联
  spread_type     TEXT NOT NULL,
  state           TEXT NOT NULL,
  dvol_tier       INTEGER,
  sell_instrument TEXT,
  buy_instrument  TEXT,
  expiry_ts       INTEGER,
  sell_strike     REAL,
  buy_strike      REAL,
  qty_total       REAL DEFAULT 0,            -- 每腿张数（两腿等量）
  net_credit_btc  REAL,                      -- 每张净收权利金（BTC，实际成交口径）
  open_index_price REAL,                     -- 开仓时指数价（BEP 折算用）
  bep_price       REAL,                      -- 后端按实际成交计算
  opened_at       INTEGER,
  closed_at       INTEGER,
  close_reason    TEXT,
  created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_groups (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id     INTEGER NOT NULL REFERENCES parent_trades(id),
  group_index   INTEGER NOT NULL,
  qty           REAL NOT NULL,
  state         TEXT NOT NULL DEFAULT 'OPEN',
  closed_qty    REAL DEFAULT 0,
  realized_pnl_btc REAL DEFAULT 0,
  unrealized_pnl_btc REAL DEFAULT 0,
  profit_ratio   REAL,
  closed_at     INTEGER,
  close_reason  TEXT,
  UNIQUE(parent_id, group_index)
);

CREATE TABLE IF NOT EXISTS legs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id   INTEGER NOT NULL REFERENCES parent_trades(id),
  role        TEXT NOT NULL,                 -- SELL_LEG / BUY_LEG
  instrument  TEXT NOT NULL,
  qty         REAL DEFAULT 0,
  avg_price   REAL,                          -- 开仓均价（BTC/张）
  closed_qty  REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fills (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id   INTEGER,
  group_id    INTEGER,
  instrument  TEXT NOT NULL,
  direction   TEXT NOT NULL,                 -- buy / sell
  qty         REAL NOT NULL,
  price       REAL,
  fee         REAL DEFAULT 0,
  phase       TEXT NOT NULL,                 -- open / close / hedge / unwind
  ts          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS hedge_state (
  parent_id       INTEGER PRIMARY KEY REFERENCES parent_trades(id),
  state           TEXT NOT NULL DEFAULT 'HEDGE_INACTIVE',
  activated_at    INTEGER,
  release_pending_bar_ts INTEGER,            -- 触发撤销线的 3min bar 时间戳
  futures_usd     REAL DEFAULT 0,            -- 本策略归属的对冲成交净额（USD，正=多）
  futures_delta_btc REAL DEFAULT 0,          -- 本策略归属对冲的估算 Delta（BTC）
  last_rebalance_at INTEGER
);

CREATE TABLE IF NOT EXISTS hedge_actions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id     INTEGER,
  ts            INTEGER NOT NULL,
  action        TEXT NOT NULL,               -- ACTIVATE / REBALANCE / RELEASE / SKIP / CLOSE
  net_delta_before REAL,
  net_delta_after  REAL,
  order_usd     REAL,
  fill_price    REAL,
  fee           REAL,
  skipped_reason TEXT                        -- MIN_AMOUNT / BELOW_TRIGGER / NULL
);

CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  level       TEXT NOT NULL DEFAULT 'INFO',  -- INFO / WARN / ERROR
  code        TEXT NOT NULL,                 -- REJECT_* / BEP_HEDGE_ACTIVATED / ...
  parent_id   INTEGER,
  group_id    INTEGER,
  payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_code_ts ON events(code, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS system_state (
  key   TEXT PRIMARY KEY,
  value TEXT
);
-- keys: trading_enabled / force_close_active / day_start_equity /
--       day_start_date / last_scan_ts / last_dvol_bar_ts / hedge_frozen

CREATE TABLE IF NOT EXISTS pine_heartbeats (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at  INTEGER NOT NULL,
  bar_ts       INTEGER,
  strategy     TEXT,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pine_heartbeats_received
  ON pine_heartbeats(received_at DESC);

-- 连接回报以策略 + 2h bar 时间戳为幂等身份。升级旧库时先保留每组最早一条，
-- 再建立局部唯一索引；缺少身份字段的诊断记录不参与去重。
DELETE FROM pine_heartbeats
WHERE strategy IS NOT NULL
  AND strategy <> ''
  AND bar_ts IS NOT NULL
  AND id NOT IN (
    SELECT MIN(id)
    FROM pine_heartbeats
    WHERE strategy IS NOT NULL AND strategy <> '' AND bar_ts IS NOT NULL
    GROUP BY strategy, bar_ts
  );
CREATE UNIQUE INDEX IF NOT EXISTS uq_pine_heartbeats_strategy_bar
  ON pine_heartbeats(strategy, bar_ts)
  WHERE strategy IS NOT NULL AND strategy <> '' AND bar_ts IS NOT NULL;

-- 升级兼容：旧版本只在 system_state 保存最新一条真实 Pine 心跳。
-- 新历史表首次创建且为空时回填该记录；后续启动不会重复插入。
INSERT INTO pine_heartbeats(received_at, bar_ts, strategy, payload_json)
SELECT
  COALESCE(CAST(json_extract(value, '$.received_at') AS INTEGER),
           CAST(strftime('%s', 'now') AS INTEGER) * 1000),
  CAST(json_extract(value, '$.bar_ts') AS INTEGER),
  json_extract(value, '$.strategy'),
  json_remove(value, '$.passphrase')
FROM system_state
WHERE key = 'pine_status'
  AND json_valid(value)
  AND NOT EXISTS (SELECT 1 FROM pine_heartbeats);
