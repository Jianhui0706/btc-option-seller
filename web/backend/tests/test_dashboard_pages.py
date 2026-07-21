import base64
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from src.api import performance, signal_feed
from src.app import app
from src.store.db import Database
from src.store.repo import Repo
from src.webhook import tradingview_webhook


def test_dashboard_exposes_signal_and_returns_pages_and_apis():
    routes = {(route.path, method) for route in app.routes
              for method in getattr(route, "methods", set())}

    assert ("/api/signals", "GET") in routes
    assert ("/api/performance", "GET") in routes
    assert ("/signals", "GET") in routes
    assert ("/returns", "GET") in routes
    assert ("/control/test_signal", "POST") not in routes


def test_dashboard_pages_disable_heuristic_caching():
    """页面无内容哈希文件名，少了 no-cache 浏览器会在部署后继续显示旧版。"""
    from src.app import DASHBOARD_HEADERS, _dashboard_page

    assert DASHBOARD_HEADERS["Cache-Control"] == "no-cache"
    assert _dashboard_page().headers["cache-control"] == "no-cache"


def test_signals_page_has_entry_condition_panel():
    source = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "开仓条件实时看板" in source
    assert 'id="condRows"' in source
    # 心跳状态只在表头交代一次，不逐行重复
    assert 'id="condPineState"' in source
    assert source.count("尚未收到心跳") <= 1


def test_frontend_contains_three_distinct_dashboard_views():
    source = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'id="page-overview"' in source
    assert 'id="page-signals"' in source
    assert 'id="page-returns"' in source
    assert "TradingView 信号" in source
    assert "TradingView 实时监听" in source
    assert "测试工具" not in source
    assert "testSignal(" not in source
    assert "tradeSwitch" not in source
    assert "收益分析" in source
    assert "Array.from({length:30}" in source
    assert '<div class="topbar-primary">' in source
    assert '<div class="topbar-command">' in source
    assert '<small>Overview</small>' in source
    assert '<small>Signals</small>' in source
    assert '<small>Performance</small>' in source
    assert 'class="btn icon-btn mobile-only primary-mobile-menu"' in source


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
async def test_signal_feed_exposes_only_real_tradingview_signals(tmp_path):
    db = Database(tmp_path / "signals.db")
    conn = await db.open()
    try:
        repo = Repo(conn)
        await repo.insert_signal(
            "TV-1", "OPEN_REQUEST", "BTC_OPTION_SELLER_V2", "PUT_CREDIT",
            {"source": "tradingview", "btc_price": 66000, "dvol_tier": 2},
        )
        await repo.set_signal_status("TV-1", "ACCEPTED")
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
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            ctx=SimpleNamespace(
                repo=repo,
                cfg=SimpleNamespace(env=SimpleNamespace(webhook_auth_token="configured")),
            )
        )))

        result = await signal_feed(request, limit=20)

        assert result["configured"] is True
        assert result["summary"] == {
            "total": 1, "accepted": 1, "rejected": 0, "pending": 0,
        }
        assert [item["signal_id"] for item in result["items"]] == ["TV-1"]
        assert result["items"][0]["source"] == "tradingview"
        assert result["items"][0]["payload"]["btc_price"] == 66000
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_performance_summarizes_realized_unrealized_and_trade_results(tmp_path):
    db = Database(tmp_path / "performance.db")
    conn = await db.open()
    now = int(time.time() * 1000)
    eight_days_ago = now - 8 * 86400 * 1000
    try:
        await conn.executemany(
            "INSERT INTO parent_trades(id, signal_id, spread_type, state, closed_at, created_at) "
            "VALUES(?,?,?,?,?,?)",
            [
                (1, None, "PUT_CREDIT", "CLOSED", now, now - 1000),
                (2, None, "CALL_CREDIT", "CLOSED", eight_days_ago, eight_days_ago - 1000),
                (3, None, "PUT_CREDIT", "OPEN", None, now),
            ],
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
            ctx=SimpleNamespace(repo=SimpleNamespace(conn=conn))
        )))

        result = await performance(request)

        summary = result["summary"]
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

    assert result["status"] == "queued"
    assert repo.stored_payload["source"] == "tradingview"
