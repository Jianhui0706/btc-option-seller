import base64
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from fastapi import Response

from src.api import (delete_strategy_records, hedge_ledger, performance, signal_feed,
                     status, trades_history, tradingview_setup)
from src.app import app
from src.store.db import Database
from src.store.repo import Repo
from src.webhook import tradingview_webhook


def test_dashboard_exposes_signal_and_returns_pages_and_apis():
    def walk(items):
        for route in items:
            if hasattr(route, "path"):
                yield route
            nested = getattr(route, "routes", None)
            if nested is None:
                nested = getattr(getattr(route, "original_router", None),
                                 "routes", None)
            if nested:
                yield from walk(nested)

    routes = {(route.path, method) for route in walk(app.routes)
              for method in getattr(route, "methods", set())}

    assert ("/api/signals", "GET") in routes
    assert ("/api/performance", "GET") in routes
    assert ("/api/hedge_ledger", "GET") in routes
    assert ("/api/tradingview_setup", "GET") in routes
    assert ("/api/strategies", "DELETE") in routes
    assert ("/control/manual_hedge", "POST") in routes
    assert ("/control/manual_hedge_release", "POST") in routes
    assert ("/control/residual/retry", "POST") in routes
    assert ("/control/residual/quarantine", "POST") in routes
    assert ("/webhook/signal", "POST") in routes
    assert ("/webhook/tradingview", "POST") in routes
    assert ("/signals", "GET") in routes
    assert ("/tradingview", "GET") in routes
    assert ("/signal-log", "GET") in routes
    assert ("/returns", "GET") in routes
    assert ("/control/test_signal", "POST") not in routes


@pytest.mark.asyncio
async def test_hedge_ledger_reports_requested_vs_filled_and_attribution(tmp_path):
    db = Database(tmp_path / "hedge-ledger.db")
    conn = await db.open()
    now = int(time.time() * 1000)
    try:
        repo = Repo(conn)
        await repo.insert_signal(
            "HEDGE-LEDGER-1", "OPEN_REQUEST", "MACD策略2", "CALL_CREDIT", {})
        await conn.execute(
            "INSERT INTO parent_trades(id, signal_id, spread_type, state, created_at) "
            "VALUES(1, 'HEDGE-LEDGER-1', 'CALL_CREDIT', 'OPEN', ?)", (now,))
        await conn.executemany(
            "INSERT INTO hedge_actions(parent_id, ts, action, net_delta_before, "
            "net_delta_after, order_usd, requested_usd, managed_usd_after, "
            "cycle_id, attempt_no, max_attempts, residual_delta, fill_price, fee) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (1, now - 2000, "REBALANCE", -2.0, -1.4, 40000, 130000,
                 40000, "cycle-a", 1, 3, -1.4, 65000, 0.0003),
                (1, now - 1000, "REBALANCE", -1.4, -0.01, 90000, 90000,
                 130000, "cycle-a", 2, 3, -0.01, 65100, 0.0007),
            ],
        )
        await repo.record_execution_attempts(1, [{
            "phase": "hedge",
            "operation": "manual_perp",
            "instrument": "BTC-PERPETUAL",
            "direction": "buy",
            "liquidity_role": "MAKER",
            "attempt_no": 2,
            "round": 1,
            "requested_qty": 20000,
            "filled_qty": 15000,
            "limit_price": 65050,
            "avg_fill_price": 65050,
            "status": "PARTIAL",
            "group_index": 3,
            "group_count": 5,
            "hedge_cycle_id": "manual-cycle",
            "cycle_attempt_no": 1,
            "cycle_max_attempts": 1,
            "placed_at": now - 1500,
            "finished_at": now - 1400,
            "duration_ms": 100,
            "exchange_order_id": "test-order-1",
            "fill_qty_is_incremental": True,
        }])
        await conn.execute(
            "UPDATE hedge_actions SET details_json=? WHERE attempt_no=2",
            (json.dumps({
                "manual": False, "liquidity_role": "TAKER",
                "exchange_order_id": "auto-order-2",
                "exchange_trade_ids": ["auto-trade-2"],
            }),))
        await conn.commit()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            ctx=SimpleNamespace(repo=repo))))

        result = await hedge_ledger(request, days=14, limit=20)

        assert result["summary"]["executions"] == 2
        assert result["summary"]["requested_usd"] == pytest.approx(220000)
        assert result["summary"]["filled_usd"] == pytest.approx(130000)
        assert result["summary"]["partial_fills"] == 1
        assert result["summary"]["fees_btc"] == pytest.approx(0.001)
        assert result["actions"][0]["result"] == "FILLED"
        assert result["actions"][1]["result"] == "PARTIAL"
        assert result["actions"][1]["parent_id"] == 1
        assert result["actions"][1]["strategy"] == "MACD策略2"
        assert result["actions"][1]["fill_ratio"] == pytest.approx(40000 / 130000)
        assert result["actions"][0]["cycle_id"] == "cycle-a"
        assert result["actions"][0]["attempt_no"] == 2
        assert result["actions"][0]["max_attempts"] == 3
        assert result["actions"][0]["residual_delta"] == pytest.approx(-0.01)
        detailed = next(row for row in result["execution_records"]
                        if row["record_type"] == "ORDER_ATTEMPT")
        assert detailed["group_index"] == 3
        assert detailed["group_attempt_no"] == 2
        assert detailed["cycle_attempt_no"] == 1
        assert detailed["requested_usd"] == pytest.approx(20000)
        assert detailed["filled_usd"] == pytest.approx(15000)
        assert detailed["exchange_order_id"] == "test-order-1"
        automatic = [row for row in result["execution_records"]
                     if row["record_type"] == "AUTO_IOC"]
        assert len(automatic) == 2
        assert all(row["liquidity_role"] == "TAKER" for row in automatic)
        linked_auto = next(row for row in automatic
                           if row["cycle_attempt_no"] == 2)
        assert linked_auto["exchange_order_id"] == "auto-order-2"
        assert linked_auto["exchange_trade_ids"] == ["auto-trade-2"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_hedge_ledger_converts_legacy_cumulative_maker_fill_to_incremental(
        tmp_path):
    db = Database(tmp_path / "legacy-hedge-attempts.db")
    conn = await db.open()
    now = int(time.time() * 1000)
    try:
        repo = Repo(conn)
        await repo.insert_signal(
            "LEGACY-HEDGE", "OPEN_REQUEST", "测试策略", "PUT_CREDIT", {})
        await conn.execute(
            "INSERT INTO parent_trades(id, signal_id, spread_type, state, created_at) "
            "VALUES(1, 'LEGACY-HEDGE', 'PUT_CREDIT', 'OPEN', ?)", (now,))
        await conn.execute(
            "INSERT INTO hedge_actions(parent_id, ts, action, order_usd, "
            "requested_usd, details_json) VALUES(?,?,?,?,?,?)",
            (1, now, "REBALANCE", -30, -30,
             json.dumps({"manual": True, "groups": []})))
        await conn.executemany(
            "INSERT INTO execution_attempts(parent_id, phase, operation, "
            "instrument, direction, liquidity_role, attempt_no, round_no, "
            "requested_qty, filled_qty, status, details_json) "
            "VALUES(1,'hedge','manual_perp','BTC-PERPETUAL','sell','MAKER',"
            "?,?,?,?,?,?)",
            [
                (1, 1, 30, 20, "PARTIAL", json.dumps({"group_index": 5})),
                (2, 1, 10, 30, "FILLED", json.dumps({"group_index": 5})),
            ],
        )
        await conn.commit()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            ctx=SimpleNamespace(repo=repo))))

        result = await hedge_ledger(request, days=14, limit=20)
        rows = [row for row in result["execution_records"]
                if row["record_type"] == "ORDER_ATTEMPT"]

        assert [row["filled_usd"] for row in rows] == [-10.0, -20.0]
        assert all(row["group_index"] == 5 for row in rows)
        assert all(str(row["cycle_id"]).startswith("legacy-action-")
                   for row in rows)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_status_exposes_explainable_hedge_diagnostic():
    parent = {
        "id": 7,
        "signal_id": "LIVE-HEDGE-7",
        "spread_type": "PUT_CREDIT",
        "state": "OPEN",
        "open_index_price": 65000.0,
        "bep_price": 62000.0,
    }

    class FakeRepo:
        async def active_parents(self):
            return [parent]

        async def groups_of(self, parent_id):
            return []

        async def get_hedge(self, parent_id):
            return {"state": "HEDGE_INACTIVE", "futures_usd": 0.0}

        async def get_signal_payload(self, signal_id):
            return {}

        async def get_state(self, key):
            return "1000" if key == "last_scan_ts" else None

    class FakeCtx:
        repo = FakeRepo()
        hedge_frozen = False
        hedge_snapshot = {
            "bar_close": 65000.0,
            "bar_ts": 900,
            "observed_at": 1000,
            "option_delta": 0.01,
            "futures_delta": 0.0,
            "net_delta": 0.01,
            "equity_btc": 1.0,
            "index_price": 65000.0,
        }
        hedge_snapshots = {7: {
            **hedge_snapshot,
            "option_delta": 0.003,
            "net_delta": 0.003,
        }}
        scan_last_completed_ts = 1000
        scan_last_error = None
        scan_last_error_ts = None
        scan_last_error_kind = None
        scan_running = False
        scan_last_started_ts = 1000
        current_command_kind = None
        current_command_started_ts = None
        cfg = SimpleNamespace(
            env=SimpleNamespace(is_testnet=True),
            hedge=SimpleNamespace(
                delta_account_risk_pct=0.02,
                delta_trigger_pct=0.10,
                bep_activation_progress=0.50,
                call_release_pct=0.98,
                put_release_pct=1.02,
                perp_contract_usd=10.0,
            ),
            debug=SimpleNamespace(
                bep_override=None,
                trigger_override=None,
            ),
        )

        async def trading_enabled(self):
            return True

        async def force_close_active(self):
            return False

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(ctx=FakeCtx())))

    result = await status(request)

    diagnostic = result["active_trades"][0]["hedge_diagnostic"]
    assert diagnostic["reason_code"] == "WAITING_FOR_HEDGE_TRIGGER"
    assert diagnostic["bar_close"] == 65000.0
    assert diagnostic["bep"] == 62000.0
    assert diagnostic["activation_price"] == 63500.0
    assert diagnostic["net_delta"] == pytest.approx(0.003)


@pytest.mark.asyncio
async def test_tradingview_setup_exposes_configured_secret_for_experiment():
    ctx = SimpleNamespace(cfg=SimpleNamespace(
        env=SimpleNamespace(
            trading_account_name="account_1",
            webhook_auth_token="super-secret",
        ),
        strategy=SimpleNamespace(name="BTC_OPTION_SELLER_V2"),
    ))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=ctx)))

    response = Response()
    result = await tradingview_setup(request, response)

    assert result["webhook_path"] == "/webhook/signal"
    assert result["schema_version"] == "2"
    assert result["message_mode"] == "generic_position_transition"
    assert result["account_name"] == "account_1"
    assert result["passphrase_configured"] is True
    assert result["passphrase"] == "super-secret"
    assert response.headers["cache-control"] == "no-store"


def test_dashboard_pages_disable_heuristic_caching():
    """页面无内容哈希文件名，少了 no-cache 浏览器会在部署后继续显示旧版。"""
    from src.app import DASHBOARD_HEADERS, _dashboard_page

    assert DASHBOARD_HEADERS["Cache-Control"] == "no-cache"
    assert _dashboard_page().headers["cache-control"] == "no-cache"


def test_signals_page_is_multi_strategy_activity_center():
    source = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text(
        encoding="utf-8"
    )

    page = source.split('id="page-active-signals"', 1)[1].split(
        'id="page-signal-log"', 1)[0]
    assert "策略信号中心" in page
    assert 'id="strategyRoster"' in page
    assert 'id="strategyInspector"' in page
    assert "删除旧策略记录" in source
    assert 'id="journeySummary"' not in page
    assert "DVOL 档位" not in page


def test_frontend_contains_primary_tradingview_and_teacher_dashboard_views():
    source = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'id="page-positions"' in source
    assert 'id="page-tradingview"' in source
    assert 'id="page-active-signals"' in source
    assert 'id="page-signal-log"' in source
    assert 'id="page-position-log"' not in source
    positions = source.split('id="page-positions"', 1)[1].split(
        'id="page-tradingview"', 1)[0]
    assert 'id="positionHistory"' in positions
    assert 'id="pnlCurveCanvas"' in positions
    assert "累计已实现收益曲线" in positions
    assert 'class="position-primary-stack"' in positions
    assert 'class="panel active-strategy-panel"' in positions
    assert 'class="panel actual-position-panel"' in positions
    assert 'id="portfolioCurrentTab"' in positions
    assert 'id="portfolioHistoryTab"' in positions
    assert "策略来源与交易执行分开管理" in source
    assert "TradingView 实时监听" in source
    assert "测试工具" not in source
    assert "testSignal(" not in source
    assert "tradeSwitch" not in source
    assert "仓位历史与收益" in source
    assert "renderPnlCurve" in source
    assert '<div class="topbar-primary">' in source
    assert '<div class="topbar-command">' in source
    assert '<small>Positions</small>' in source
    assert '<small>Primary Setup</small>' in source
    assert '<small>Strategies</small>' in source
    assert '<small>Signal Log</small>' in source
    assert '<small>Position Log</small>' not in source
    assert 'class="btn icon-btn mobile-only primary-mobile-menu"' in source
    assert "<th>成交方式</th>" in source
    assert "Maker 挂单" in source
    assert "Taker 吃单" in source
    assert "function forceCloseResultText(p)" in source
    assert "强平失败：" in source
    assert "强平完成，Deribit 实际仓位已归零" in source


def test_project_uses_the_chinese_brand_name_everywhere():
    root = Path(__file__).resolve().parents[3]
    branded_files = [
        root / "README.md",
        root / "start.ps1",
        root / "web" / "frontend" / "index.html",
        root / "web" / "frontend" / "package.json",
        root / "web" / "frontend" / "package-lock.json",
        root / "web" / "backend" / "server.py",
        root / "web" / "backend" / "static" / "index.html",
    ]

    for path in branded_files:
        source = path.read_text(encoding="utf-8-sig")
        assert "VolGuard" not in source, path
        assert "volguard" not in source, path

    frontend = (root / "web" / "frontend" / "index.html").read_text(encoding="utf-8")
    assert '<div class="mark">策</div>' in frontend
    assert '<div class="brand-name">BTC 期权卖方系统</div>' in frontend
    assert "DERIBIT 策略执行系统" in frontend


def test_windows_launcher_remains_ascii_safe_and_decodes_the_chinese_brand():
    root = Path(__file__).resolve().parents[3]
    launcher = (root / "start.ps1").read_bytes()

    assert all(byte < 128 for byte in launcher)
    assert base64.b64decode("QlRDIOacn+adg+WNluaWueezu+e7nw==").decode("utf-8") == (
        "BTC 期权卖方系统"
    )


@pytest.mark.asyncio
async def test_signal_feed_merges_heartbeats_and_real_tradingview_signals(tmp_path):
    db = Database(tmp_path / "signals.db")
    conn = await db.open()
    try:
        repo = Repo(conn)
        await repo.insert_signal(
            "TV-OPEN-1", "OPEN_REQUEST", "BTC_OPTION_SELLER_V2", "PUT_CREDIT",
            {"source": "tradingview", "btc_price": 66000, "dvol_tier": 2},
        )
        await repo.set_signal_status(
            "TV-OPEN-1", "REJECTED", "REJECT_DVOL_BELOW_ENTRY")
        await repo.insert_signal(
            "TV-EXIT-1", "EXIT_DVOL", "BTC_OPTION_SELLER_V2", "",
            {"source": "tradingview", "dvol_percentile": 0.25},
        )
        await repo.set_signal_status("TV-EXIT-1", "ACCEPTED")
        await repo.insert_signal(
            "UI-1", "EXIT_DVOL", "BTC_OPTION_SELLER_V2", "",
            {"source": "test_tool"},
        )
        await repo.set_signal_status("UI-1", "REJECTED", "trading disabled")
        await repo.insert_signal(
            "TEST-LEGACY-1", "OPEN_REQUEST", "BTC_OPTION_SELLER_V2", "PUT_CREDIT",
            {"btc_price": 65000},
        )
        await repo.set_signal_status("TEST-LEGACY-1", "ACCEPTED")
        await conn.execute(
            "UPDATE signals SET received_at=300 WHERE signal_id='TV-OPEN-1'")
        await conn.execute(
            "UPDATE signals SET received_at=100 WHERE signal_id='TV-EXIT-1'")
        await conn.commit()
        await repo.record_pine_heartbeat({
            "event": "STATUS", "strategy": "BTC_OPTION_SELLER_V2",
            "bar_ts": 200, "received_at": 200, "passphrase": "hidden",
        })
        await repo.record_pine_heartbeat({
            "event": "STATUS", "strategy": "BTC_OPTION_SELLER_V2",
            "bar_ts": 400, "received_at": 400, "passphrase": "hidden",
        })
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            ctx=SimpleNamespace(
                repo=repo,
                cfg=SimpleNamespace(env=SimpleNamespace(webhook_auth_token="configured")),
            )
        )))

        result = await signal_feed(request, limit=20)

        assert result["configured"] is True
        assert result["summary"] == {
            "total": 4,
            "heartbeats": 2,
            "signal_total": 2,
            "open_signals": 1,
            "exit_signals": 1,
            "accepted": 1,
            "rejected": 1,
            "pending": 0,
        }
        assert [item["kind"] for item in result["items"]] == [
            "HEARTBEAT", "OPEN_SIGNAL", "HEARTBEAT", "EXIT_SIGNAL",
        ]
        assert result["items"][1]["key"] == "signal:TV-OPEN-1"
        assert result["items"][1]["status"] == "REJECTED"
        assert result["items"][1]["reject_reason"] == "REJECT_DVOL_BELOW_ENTRY"
        assert result["items"][1]["payload"]["btc_price"] == 66000
        assert all("passphrase" not in item["payload"] for item in result["items"])
        assert result["server_ts"] > 0
        assert result["strategies"] == [{
            "strategy": "BTC_OPTION_SELLER_V2",
            "account_name": "",
            "signal_total": 2,
            "open_signals": 1,
            "exit_signals": 1,
            "accepted": 1,
            "rejected": 1,
            "pending": 0,
            "heartbeats": 2,
            "active_trades": 0,
            "last_signal_at": 300,
            "last_heartbeat_at": 400,
            "last_activity_at": 400,
        }]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_signal_feed_groups_multiple_strategies_independently(tmp_path):
    db = Database(tmp_path / "multi-strategy-signals.db")
    conn = await db.open()
    try:
        repo = Repo(conn)
        await repo.insert_signal(
            "ALPHA-OPEN", "OPEN_REQUEST", "ALPHA", "PUT_CREDIT",
            {"source": "tradingview", "account_name": "account_1"},
        )
        await repo.set_signal_status("ALPHA-OPEN", "ACCEPTED")
        await repo.insert_signal(
            "BETA-EXIT", "EXIT_DVOL", "BETA", "",
            {"source": "tradingview", "account_name": "account_1"},
        )
        await repo.set_signal_status("BETA-EXIT", "REJECTED", "no_matching_trade")
        await repo.record_pine_heartbeat({
            "event": "STATUS", "strategy": "BETA",
            "bar_ts": 500, "received_at": 500,
        })
        await conn.commit()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            ctx=SimpleNamespace(
                repo=repo,
                cfg=SimpleNamespace(env=SimpleNamespace(webhook_auth_token="configured")),
            )
        )))

        result = await signal_feed(request, limit=20)
        by_name = {item["strategy"]: item for item in result["strategies"]}

        assert set(by_name) == {"ALPHA", "BETA"}
        assert by_name["ALPHA"]["open_signals"] == 1
        assert by_name["ALPHA"]["accepted"] == 1
        assert by_name["ALPHA"]["account_name"] == "account_1"
        assert by_name["BETA"]["exit_signals"] == 1
        assert by_name["BETA"]["rejected"] == 1
        assert by_name["BETA"]["heartbeats"] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_signal_feed_can_load_one_strategy_outside_global_recent_limit(tmp_path):
    db = Database(tmp_path / "strategy-history-pagination.db")
    conn = await db.open()
    try:
        rows = [
            (
                f"OLD-{index}", "OPEN_REQUEST", "OLD_STRATEGY", "PUT_CREDIT",
                '{"source":"tradingview"}', index, "ACCEPTED",
            )
            for index in range(1, 4)
        ]
        rows.extend(
            (
                f"NEW-{index}", "OPEN_REQUEST", "NEW_STRATEGY", "CALL_CREDIT",
                '{"source":"tradingview"}', 1000 + index, "REJECTED",
            )
            for index in range(120)
        )
        await conn.executemany(
            "INSERT INTO signals(signal_id, event, strategy, spread_type, "
            "payload_json, received_at, status) VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        await conn.commit()
        repo = Repo(conn)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            ctx=SimpleNamespace(
                repo=repo,
                cfg=SimpleNamespace(
                    env=SimpleNamespace(webhook_auth_token="configured")),
            )
        )))

        global_result = await signal_feed(request, limit=100)
        old_result = await signal_feed(
            request, limit=500, strategy="OLD_STRATEGY")

        assert len(global_result["items"]) == 100
        assert all(
            item["strategy"] == "NEW_STRATEGY"
            for item in global_result["items"]
        )
        assert [item["signal_id"] for item in old_result["items"]] == [
            "OLD-3", "OLD-2", "OLD-1",
        ]
        assert next(
            item for item in old_result["strategies"]
            if item["strategy"] == "OLD_STRATEGY"
        )["signal_total"] == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_signal_feed_tolerates_corrupt_heartbeat_json(tmp_path):
    db = Database(tmp_path / "corrupt-heartbeat.db")
    conn = await db.open()
    try:
        await conn.execute(
            "INSERT INTO pine_heartbeats(received_at, bar_ts, strategy, payload_json) "
            "VALUES(1, 1, 'BTC_OPTION_SELLER_V2', '{bad json')")
        await conn.commit()
        repo = Repo(conn)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            ctx=SimpleNamespace(
                repo=repo,
                cfg=SimpleNamespace(env=SimpleNamespace(webhook_auth_token="configured")),
            )
        )))

        result = await signal_feed(request, limit=20)

        assert result["items"][0]["kind"] == "HEARTBEAT"
        assert result["items"][0]["payload"] == {}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_performance_summarizes_realized_unrealized_and_trade_results(tmp_path):
    db = Database(tmp_path / "performance.db")
    conn = await db.open()
    now = int(time.time() * 1000)
    eight_days_ago = now - 8 * 86400 * 1000
    try:
        repo = Repo(conn)
        await repo.insert_signal(
            "TV-OPEN-1", "OPEN_REQUEST", "PARAMETER_SET_A", "PUT_CREDIT",
            {"source": "tradingview", "tv_id": "1784851200123"},
        )
        await conn.executemany(
            "INSERT INTO parent_trades(id, signal_id, spread_type, state, closed_at, created_at) "
            "VALUES(?,?,?,?,?,?)",
            [
                (1, "TV-OPEN-1", "PUT_CREDIT", "CLOSED", now, now - 1000),
                (2, None, "CALL_CREDIT", "CLOSED", eight_days_ago, eight_days_ago - 1000),
                (3, None, "PUT_CREDIT", "OPEN", None, now),
            ],
        )
        await conn.execute(
            "UPDATE parent_trades SET trade_id=? WHERE id=1",
            ("1784851200123",),
        )
        await conn.executemany(
            "INSERT INTO trade_groups(parent_id, group_index, qty, state, "
            "realized_pnl_btc, unrealized_pnl_btc, closed_at) VALUES(?,?,?,?,?,?,?)",
            [
                (1, 1, 1, "CLOSED", 0.02, 0, now),
                (2, 1, 1, "CLOSED", -0.01, 0, eight_days_ago),
                (3, 1, 1, "OPEN", 0, 0.005, None),
            ],
        )
        await conn.execute(
            "INSERT INTO hedge_actions(parent_id, ts, action, fee) VALUES(?,?,?,?)",
            (1, now, "CLOSE", 0.001),
        )
        await conn.commit()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            ctx=SimpleNamespace(
                repo=repo,
                db=db,
                cfg=SimpleNamespace(env=SimpleNamespace(
                    trading_account_name="account_2",
                    client_id="account-2-client",
                    is_testnet=True,
                )),
            )
        )))

        result = await performance(request)
        history = await trades_history(request)

        summary = result["summary"]
        assert result["account"]["name"] == "account_2"
        assert result["account"]["environment"] == "TESTNET"
        assert result["account"]["scope"].startswith("testnet-")
        assert summary["realized_pnl_btc"] == pytest.approx(0.01)
        assert summary["unrealized_pnl_btc"] == pytest.approx(0.005)
        assert summary["hedge_fees_btc"] == pytest.approx(0.001)
        assert summary["tracked_net_pnl_btc"] == pytest.approx(0.014)
        assert summary["today_realized_btc"] == pytest.approx(0.02)
        assert summary["last_7d_realized_btc"] == pytest.approx(0.02)
        assert summary["last_30d_realized_btc"] == pytest.approx(0.01)
        assert summary["wins"] == 1
        assert summary["losses"] == 1
        assert summary["win_rate"] == pytest.approx(0.5)
        assert len(result["trades"]) == 3
        assert [point["trade_id"] for point in result["curve"]] == [2, 1]
        assert result["curve"][0]["realized_pnl_btc"] == pytest.approx(-0.01)
        assert result["curve"][0]["cumulative_realized_pnl_btc"] == pytest.approx(-0.01)
        assert result["curve"][1]["cumulative_realized_pnl_btc"] == pytest.approx(0.01)
        attributed = next(t for t in result["trades"] if t["id"] == 1)
        assert attributed["strategy"] == "PARAMETER_SET_A"
        assert attributed["trade_id"] == "1784851200123"
        assert attributed["tv_id"] == "1784851200123"
        history_trade = next(t for t in history if t["id"] == 1)
        assert history_trade["strategy"] == "PARAMETER_SET_A"
        assert history_trade["tv_id"] == "1784851200123"
        assert history_trade["signal_id"] == "TV-OPEN-1"
        assert "open_liquidity_role" in history_trade
        assert "close_liquidity_role" in history_trade
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_performance_curve_is_not_limited_by_recent_trade_history(tmp_path):
    db = Database(tmp_path / "performance-curve-history.db")
    conn = await db.open()
    now = int(time.time() * 1000)
    try:
        repo = Repo(conn)
        await conn.execute(
            "INSERT INTO parent_trades(id, spread_type, state, closed_at, created_at) "
            "VALUES(1, 'PUT_CREDIT', 'CLOSED', ?, ?)",
            (now - 1000, now - 2000),
        )
        await conn.execute(
            "INSERT INTO trade_groups(parent_id, group_index, qty, state, "
            "realized_pnl_btc, closed_at) VALUES(1, 1, 1, 'CLOSED', 0.02, ?)",
            (now - 1000,),
        )
        await conn.executemany(
            "INSERT INTO parent_trades(id, spread_type, state, created_at) "
            "VALUES(?, 'CALL_CREDIT', 'OPEN_FAILED', ?)",
            [(trade_id, now + trade_id) for trade_id in range(2, 202)],
        )
        await conn.commit()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            ctx=SimpleNamespace(repo=repo)
        )))

        result = await performance(request)

        assert [trade["id"] for trade in result["trades"]] == [1]
        assert result["summary"]["closed_trades"] == 1
        assert result["summary"]["wins"] == 1
        assert [point["trade_id"] for point in result["curve"]] == [1]
        assert result["curve"][0]["cumulative_realized_pnl_btc"] == pytest.approx(0.02)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delete_strategy_removes_terminal_history_and_keeps_shared_data(tmp_path):
    db = Database(tmp_path / "delete-strategy.db")
    conn = await db.open()
    try:
        await conn.executemany(
            "INSERT INTO signals(signal_id, event, strategy, spread_type, "
            "payload_json, received_at) VALUES(?,?,?,?,?,?)",
            [
                ("OLD-1", "OPEN_REQUEST", "OLD_STRATEGY", "PUT_CREDIT", "{}", 1),
                ("KEEP-1", "OPEN_REQUEST", "KEEP_STRATEGY", "CALL_CREDIT", "{}", 2),
            ],
        )
        await conn.executemany(
            "INSERT INTO parent_trades(id, signal_id, spread_type, state, created_at) "
            "VALUES(?,?,?,?,?)",
            [
                (1, "OLD-1", "PUT_CREDIT", "CLOSED", 1),
                (2, "KEEP-1", "CALL_CREDIT", "CLOSED", 2),
            ],
        )
        await conn.execute(
            "INSERT INTO trade_groups(id, parent_id, group_index, qty) "
            "VALUES(1,1,1,1)"
        )
        await conn.execute(
            "INSERT INTO legs(parent_id, role, instrument) "
            "VALUES(1,'SELL_LEG','BTC-OLD-P')"
        )
        await conn.execute(
            "INSERT INTO fills(parent_id, instrument, direction, qty, phase, ts) "
            "VALUES(1,'BTC-OLD-P','sell',1,'open',1)"
        )
        await conn.execute("INSERT INTO hedge_state(parent_id) VALUES(1)")
        await conn.execute(
            "INSERT INTO hedge_actions(parent_id, ts, action) VALUES(1,1,'CLOSE')"
        )
        await conn.execute(
            "INSERT INTO events(ts, code, parent_id, group_id, payload_json) "
            "VALUES(1,'NORMAL_CLOSE',1,1,'{}')"
        )
        await conn.execute(
            "INSERT INTO pine_heartbeats(received_at, strategy, payload_json) "
            "VALUES(1,'OLD_STRATEGY','{}')"
        )
        await conn.commit()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            ctx=SimpleNamespace(
                db=SimpleNamespace(path=db.path),
                repo=Repo(conn),
            )
        )))

        result = await delete_strategy_records(
            request, strategy="OLD_STRATEGY")

        assert result["signals"] == 1
        assert result["trades"] == 1
        assert result["groups"] == 1
        assert result["heartbeats"] == 1
        for table in (
                "signals", "parent_trades", "trade_groups", "legs", "fills",
                "hedge_state", "hedge_actions", "events", "pine_heartbeats"):
            remaining = await (await conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE "
                + ("strategy='OLD_STRATEGY'" if table in ("signals", "pine_heartbeats")
                   else "parent_id=1" if table not in ("parent_trades",)
                   else "id=1")
            )).fetchone()
            assert remaining[0] == 0
        keep = await (await conn.execute(
            "SELECT COUNT(*) FROM signals WHERE strategy='KEEP_STRATEGY'"
        )).fetchone()
        assert keep[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delete_strategy_rejects_active_trade(tmp_path):
    db = Database(tmp_path / "active-strategy.db")
    conn = await db.open()
    try:
        await conn.execute(
            "INSERT INTO signals(signal_id, event, strategy, spread_type, "
            "payload_json, received_at) "
            "VALUES('LIVE-1','OPEN_REQUEST','LIVE_STRATEGY','PUT_CREDIT','{}',1)"
        )
        await conn.execute(
            "INSERT INTO parent_trades(signal_id, spread_type, state, created_at) "
            "VALUES('LIVE-1','PUT_CREDIT','OPEN',1)"
        )
        await conn.commit()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            ctx=SimpleNamespace(
                db=SimpleNamespace(path=db.path),
                repo=Repo(conn),
            )
        )))

        with pytest.raises(Exception) as exc:
            await delete_strategy_records(request, strategy="LIVE_STRATEGY")

        assert getattr(exc.value, "status_code", None) == 409
        remaining = await (await conn.execute(
            "SELECT COUNT(*) FROM signals WHERE strategy='LIVE_STRATEGY'"
        )).fetchone()
        assert remaining[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_tradingview_webhook_marks_the_stored_signal_source():
    class FakeRepo:
        stored_payload = None

        async def insert_signal(self, signal_id, event, strategy, spread_type, payload):
            self.stored_payload = payload
            return True

    class FakeQueue:
        def open(self, payload):
            return payload

        def put(self, command):
            self.command = command

    repo = FakeRepo()
    ctx = SimpleNamespace(
        repo=repo,
        queue=FakeQueue(),
        cfg=SimpleNamespace(
            env=SimpleNamespace(webhook_auth_token="secret"),
            strategy=SimpleNamespace(name="BTC_OPTION_SELLER_V2"),
        ),
    )

    class FakeRequest:
        app = SimpleNamespace(state=SimpleNamespace(ctx=ctx))

        async def json(self):
            return {
                "passphrase": "secret",
                "event": "OPEN_REQUEST",
                "strategy": "BTC_OPTION_SELLER_V2",
                "signal_id": "TV-SOURCE-1",
                "spread_type": "PUT_CREDIT",
                "dvol_tier": 2,
                "dvol_percentile": 0.67,
            }

    result = await tradingview_webhook(FakeRequest())

    assert result.status_code == 202
    assert json.loads(result.body)["status"] == "queued"
    assert repo.stored_payload["source"] == "tradingview"
