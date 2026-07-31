import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.engine import scanner
from src.engine.commands import CommandQueue


@pytest.mark.asyncio
async def test_independent_scan_cycle_runs_while_execution_is_busy(monkeypatch):
    """交易状态只用于展示，不会阻止独立扫描。"""
    calls = []

    async def fake_scan(ctx, payload):
        calls.append((ctx.current_command_kind, payload))
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(scanner, "handle_scan", fake_scan)
    ctx = SimpleNamespace(
        current_command_kind="REVERSE",
        scan_running=False,
        scan_last_started_ts=None,
        scan_last_completed_ts=None,
        scan_last_error=None,
        scan_last_error_kind=None,
        scan_last_error_ts=None,
    )

    await scanner.run_scan_cycle(ctx)

    assert calls == [("REVERSE", {})]
    assert ctx.scan_running is False
    assert ctx.scan_last_completed_ts is not None
    assert ctx.scan_last_error is None
    assert ctx.scan_last_error_kind is None


@pytest.mark.asyncio
async def test_scan_failure_is_reported_without_stopping_future_cycles(monkeypatch):
    outcomes = iter([RuntimeError("Deribit unavailable"), True])

    async def fake_scan(ctx, payload):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    class FakeEvents:
        async def emit(self, *args, **kwargs):
            return None

    monkeypatch.setattr(scanner, "handle_scan", fake_scan)
    monkeypatch.setattr(scanner, "ev", FakeEvents())
    ctx = SimpleNamespace(
        repo=SimpleNamespace(conn=object()),
        scan_running=False,
        scan_last_started_ts=None,
        scan_last_completed_ts=None,
        scan_last_error=None,
        scan_last_error_kind=None,
        scan_last_error_ts=None,
    )

    await scanner.run_scan_cycle(ctx)
    assert ctx.scan_last_error == "Deribit unavailable"
    assert ctx.scan_last_error_kind == "internal"
    assert ctx.scan_last_error_ts is not None

    await scanner.run_scan_cycle(ctx)
    assert ctx.scan_last_error is None
    assert ctx.scan_last_error_kind is None
    assert ctx.scan_last_completed_ts is not None


@pytest.mark.asyncio
async def test_reverse_signals_are_not_coalesced_or_reordered():
    queue = CommandQueue()
    first = {"signal_id": "reverse-1"}
    second = {"signal_id": "reverse-2"}

    assert queue.put(queue.reverse(first)) is True
    assert queue.put(queue.reverse(second)) is True

    cmd1 = await queue.get()
    cmd2 = await queue.get()
    assert cmd1.kind == cmd2.kind == "REVERSE"
    assert cmd1.payload["signal"] == first
    assert cmd2.payload["signal"] == second


def test_app_scheduler_no_longer_routes_scans_through_trade_worker():
    app_source = (
        Path(__file__).resolve().parents[1] / "src" / "app.py"
    ).read_text(encoding="utf-8")
    scanner_source = (
        Path(__file__).resolve().parents[1] / "src" / "engine" / "scanner.py"
    ).read_text(encoding="utf-8")

    handlers = app_source.split("ctx.handlers = {", 1)[1].split("}", 1)[0]
    scheduler = scanner_source.split(
        "async def run_scheduler", 1
    )[1].split("async def run_scan_cycle", 1)[0]
    assert '"SCAN"' not in handlers
    assert "ctx.queue.put" not in scheduler
    assert "await run_scan_cycle(ctx)" in scheduler


def test_closing_parent_is_requeued_by_each_scan_cycle():
    scanner_source = (
        Path(__file__).resolve().parents[1] / "src" / "engine" / "scanner.py"
    ).read_text(encoding="utf-8")
    closing_block = scanner_source.split(
        "if closing:", 1
    )[1].split("# 5.", 1)[0]

    assert "ctx.queue.normal_close" in closing_block
    assert 'parent.get("close_reason") or "CLOSE_RETRY"' in closing_block
