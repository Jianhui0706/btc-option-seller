import json
from types import SimpleNamespace

import pytest

from src.webhook import tradingview_signal_webhook


class FakeRequest:
    def __init__(self, ctx, body):
        self.app = SimpleNamespace(state=SimpleNamespace(ctx=ctx))
        self._body = body

    async def json(self):
        return self._body


class FakeQueue:
    def __init__(self):
        self.commands = []

    def open(self, payload):
        return ("open", payload)

    def normal_close(self, parent_id, reason):
        return ("close", parent_id, reason)

    def reverse(self, payload):
        return ("reverse", payload)

    def put(self, command):
        self.commands.append(command)


class FakeRepo:
    conn = None

    def __init__(self, parent=None):
        self.parents = (
            list(parent) if isinstance(parent, list)
            else ([parent] if parent is not None else [])
        )
        self.stored_payload = None
        self.status_updates = []
        self.open_parents_called = False

    async def insert_signal(self, signal_id, event, strategy, spread_type, payload):
        self.stored_payload = payload
        return True

    async def active_parents_by_trade_id(
            self, trade_id, strategy="", spread_type=""):
        self.looked_up_trade_id = trade_id
        self.looked_up_strategy = strategy
        self.looked_up_spread_type = spread_type
        return [
            parent for parent in self.parents
            if not spread_type or parent.get("spread_type") in (
                None, spread_type)
        ]

    async def active_parent_by_trade_id(self, trade_id, strategy=""):
        rows = await self.active_parents_by_trade_id(trade_id, strategy)
        return rows[0] if rows else None

    async def open_parents(self):
        self.open_parents_called = True
        return []

    async def set_signal_status(self, signal_id, status, reason=None):
        self.status_updates.append((signal_id, status, reason))


def make_ctx(repo):
    return SimpleNamespace(
        repo=repo,
        queue=FakeQueue(),
        cfg=SimpleNamespace(
            env=SimpleNamespace(
                webhook_auth_token="secret",
                trading_account_name="account_1",
            ),
            strategy=SimpleNamespace(name="BTC_OPTION_SELLER_V2"),
        ),
    )


def response_json(response):
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_teacher_style_strategy_message_becomes_one_open_command():
    repo = FakeRepo()
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "passphrase": "secret",
        "accountName": "account_1",
        "strategy": "BTC_OPTION_SELLER_V2",
        "signal_id": "PUT_CREDIT:2026-07-24T01:02:03Z",
        "trade_id": 1784707200000,
        "action": "OPEN",
        "side": "buy",
        "marketPosition": "long",
        "prevMarketPosition": "flat",
        "size": 0.4,
        "sell_delta": 0.20,
        "dte_min": 20,
        "dte_max": 35,
        "dvol_tier": 2,
        "group_split_count": 4,
        "reason": "PUT_CREDIT",
    }))

    body = response_json(response)
    assert response.status_code == 202
    assert body["status"] == "queued"
    assert body["trade_id"] == "1784707200000"
    assert repo.stored_payload["spread_type"] == "PUT_CREDIT"
    assert repo.stored_payload["quantity"] == 0.4
    assert repo.stored_payload["sell_delta"] == 0.20
    assert ctx.queue.commands == [("open", repo.stored_payload)]


@pytest.mark.asyncio
async def test_authenticated_alternative_strategy_is_accepted_and_preserved():
    repo = FakeRepo()
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "passphrase": "secret",
        "accountName": "account_1",
        "strategy": "SECOND_TV_STRATEGY",
        "signal_id": "SECOND:2026-07-24T01:02:03Z",
        "trade_id": 1784707200001,
        "action": "OPEN",
        "marketPosition": "long",
        "size": 0.2,
        "sell_delta": 0.18,
        "dte_min": 18,
        "dte_max": 30,
        "dvol_tier": 1,
        "group_split_count": 2,
        "reason": "PUT_CREDIT",
    }))

    assert response.status_code == 202
    assert repo.stored_payload["strategy"] == "SECOND_TV_STRATEGY"
    assert ctx.queue.commands == [("open", repo.stored_payload)]


@pytest.mark.asyncio
async def test_tv_id_is_the_preferred_alert_trade_identity():
    repo = FakeRepo()
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "passphrase": "secret",
        "accountName": "account_1",
        "strategy": "SECOND_TV_STRATEGY",
        "signal_id": "SECOND:2026-07-24T01:02:03Z",
        "tv_id": 987654321,
        "trade_id": 1784707200001,
        "action": "OPEN",
        "marketPosition": "long",
        "size": 0.2,
        "sell_delta": 0.18,
        "dte_min": 18,
        "dte_max": 30,
        "reason": "PUT_CREDIT",
    }))

    assert response.status_code == 202
    assert repo.stored_payload["tv_id"] == "987654321"
    assert repo.stored_payload["trade_id"] == "987654321"


@pytest.mark.asyncio
async def test_close_signal_targets_trade_id_instead_of_closing_everything(monkeypatch):
    repo = FakeRepo(parent={"id": 41, "trade_id": "1784707200000"})
    ctx = make_ctx(repo)

    async def ignore_emit(*args, **kwargs):
        return None

    monkeypatch.setattr("src.webhook.ev.emit", ignore_emit)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "passphrase": "secret",
        "accountName": "account_1",
        "strategy": "BTC_OPTION_SELLER_V2",
        "signal_id": "EXIT:2026-07-24T03:00:00Z",
        "trade_id": 1784707200000,
        "action": "CLOSE",
        "marketPosition": "flat",
        "prevMarketPosition": "long",
        "reason": "TV_DVOL_EXIT",
    }))

    assert response.status_code == 202
    assert repo.looked_up_trade_id == "1784707200000"
    assert repo.looked_up_strategy == "BTC_OPTION_SELLER_V2"
    assert repo.looked_up_spread_type == "PUT_CREDIT"
    assert repo.open_parents_called is False
    assert ctx.queue.commands == [("close", 41, "TV_DVOL_EXIT")]


@pytest.mark.asyncio
async def test_close_put_closes_all_same_source_put_batches(monkeypatch):
    repo = FakeRepo(parent=[
        {"id": 41, "trade_id": "source-a", "spread_type": "PUT_CREDIT"},
        {"id": 42, "trade_id": "source-a", "spread_type": "PUT_CREDIT"},
        {"id": 43, "trade_id": "source-a", "spread_type": "CALL_CREDIT"},
    ])
    ctx = make_ctx(repo)

    async def ignore_emit(*args, **kwargs):
        return None

    monkeypatch.setattr("src.webhook.ev.emit", ignore_emit)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "schema_version": "2",
        "passphrase": "secret",
        "accountName": "account_1",
        "source_id": "source-a",
        "source_name": "MACD",
        "signal_code": "ClosePut",
        "explicit_action": "CLOSE_LONG",
        "previous_position": "long",
        "market_position": "flat",
        "bar_time": 1784707200000,
    }))

    body = response_json(response)
    assert response.status_code == 202
    assert body["parent_id"] is None
    assert body["parent_ids"] == [41, 42]
    assert ctx.queue.commands == [
        ("close", 41, "TRADINGVIEW_CLOSE"),
        ("close", 42, "TRADINGVIEW_CLOSE"),
    ]


@pytest.mark.asyncio
async def test_alert_for_another_account_is_rejected_before_queueing():
    repo = FakeRepo()
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "passphrase": "secret",
        "accountName": "account_2",
        "strategy": "BTC_OPTION_SELLER_V2",
        "signal_id": "wrong-account",
        "action": "OPEN",
        "marketPosition": "long",
        "size": 0.1,
    }))

    assert response.status_code == 400
    assert response_json(response)["reason"] == "bad_account"
    assert ctx.queue.commands == []


@pytest.mark.asyncio
async def test_macd_long_to_short_opens_call_without_closing_put():
    repo = FakeRepo()
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "passphrase": "secret",
        "accountName": "account_1",
        "strategy": "MACD Strategy",
        "tv_id": 43904791,
        "id": "MacdSE",
        "bar_time": 1785201840000,
        "marketPosition": "short",
        "prevMarketPosition": "long",
        "size": 2,
    }))

    body = response_json(response)
    assert response.status_code == 202
    assert body["action"] == "OPEN"
    assert body["normalized_action"] == "OPEN_SHORT"
    assert repo.stored_payload["spread_type"] == "CALL_CREDIT"
    assert repo.stored_payload["quantity"] is None
    assert repo.stored_payload["execution_profile"] == "TESTNET_DEFAULT"
    assert ctx.queue.commands == [("open", repo.stored_payload)]


@pytest.mark.asyncio
async def test_macd_short_to_long_opens_put_without_closing_call():
    repo = FakeRepo()
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "passphrase": "secret",
        "accountName": "account_1",
        "strategy": "MACD Strategy",
        "tv_id": 43904791,
        "id": "MacdLE",
        "bar_time": 1785201900000,
        "marketPosition": "long",
        "prevMarketPosition": "short",
        "size": 2,
    }))

    assert response.status_code == 202
    assert response_json(response)["normalized_action"] == "OPEN_LONG"
    assert repo.stored_payload["spread_type"] == "PUT_CREDIT"
    assert repo.stored_payload["quantity"] is None
    assert ctx.queue.commands == [("open", repo.stored_payload)]


@pytest.mark.asyncio
async def test_macd_order_label_and_negative_strategy_size_are_ignored():
    repo = FakeRepo()
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "passphrase": "secret",
        "accountName": "account_1",
        "strategy": "MACD Strategy",
        "action": "MacdSE",
        "tv_id": 43904791,
        "id": "MacdSE",
        "bar_time": 1785201960000,
        "marketPosition": "short",
        "prevMarketPosition": "long",
        "size": -2,
    }))

    assert response.status_code == 202
    assert response_json(response)["normalized_action"] == "OPEN_SHORT"
    assert repo.stored_payload["quantity"] is None
    assert ctx.queue.commands == [("open", repo.stored_payload)]


@pytest.mark.asyncio
async def test_v2_other_strategy_open_uses_backend_profile_and_source_identity():
    repo = FakeRepo()
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "schema_version": "2",
        "passphrase": "secret",
        "accountName": "account_1",
        "source_id": "other-alert-901",
        "source_name": "Supertrend Strategy",
        "signal_code": "Long Entry",
        "explicit_action": "",
        "previous_position": "flat",
        "market_position": "long",
        "symbol": "BTCUSDT",
        "timeframe": "5",
        "bar_time": 1785202020000,
        "event_time": 1785202020123,
        "size": 999,
        "sell_delta": 0.60,
    }))

    body = response_json(response)
    assert response.status_code == 202
    assert body["action"] == "OPEN"
    assert body["normalized_action"] == "OPEN_LONG"
    assert body["trade_id"] == "other-alert-901"
    assert repo.stored_payload["strategy"] == "Supertrend Strategy"
    assert repo.stored_payload["source_id"] == "other-alert-901"
    assert repo.stored_payload["signal_code"] == "Long Entry"
    assert repo.stored_payload["spread_type"] == "PUT_CREDIT"
    assert repo.stored_payload["quantity"] is None
    assert repo.stored_payload["sell_delta"] is None
    assert repo.stored_payload["execution_profile"] == "TESTNET_DEFAULT"
    assert ctx.queue.commands == [("open", repo.stored_payload)]


@pytest.mark.asyncio
async def test_v2_explicit_short_open_does_not_require_position_placeholders():
    repo = FakeRepo()
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "schema_version": "2",
        "passphrase": "secret",
        "source_id": "custom-alert-902",
        "source_name": "Custom Breakout",
        "signal_code": "SHORT",
        "explicit_action": "OPEN_SHORT",
        "bar_time": 1785202080000,
    }))

    assert response.status_code == 202
    assert response_json(response)["normalized_action"] == "OPEN_SHORT"
    assert repo.stored_payload["spread_type"] == "CALL_CREDIT"
    assert repo.stored_payload["execution_profile"] == "TESTNET_DEFAULT"
    assert ctx.queue.commands == [("open", repo.stored_payload)]


@pytest.mark.asyncio
async def test_v2_close_routes_only_by_stable_source_id(monkeypatch):
    repo = FakeRepo(parent={"id": 88, "trade_id": "other-alert-901"})
    ctx = make_ctx(repo)

    async def ignore_emit(*args, **kwargs):
        return None

    monkeypatch.setattr("src.webhook.ev.emit", ignore_emit)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "schema_version": "2",
        "passphrase": "secret",
        "source_id": "other-alert-901",
        "source_name": "Renamed Display Name",
        "signal_code": "Close Long",
        "explicit_action": "CLOSE_LONG",
        "bar_time": 1785202140000,
    }))

    assert response.status_code == 202
    assert response_json(response)["normalized_action"] == "CLOSE_LONG"
    assert repo.looked_up_trade_id == "other-alert-901"
    assert repo.looked_up_strategy == ""
    assert repo.open_parents_called is False
    assert ctx.queue.commands == [("close", 88, "TRADINGVIEW_CLOSE")]


@pytest.mark.asyncio
async def test_v2_missing_source_identity_is_rejected(monkeypatch):
    repo = FakeRepo()
    ctx = make_ctx(repo)

    async def ignore_emit(*args, **kwargs):
        return None

    monkeypatch.setattr("src.webhook.ev.emit", ignore_emit)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "schema_version": "2",
        "passphrase": "secret",
        "source_name": "Incomplete Strategy",
        "signal_code": "Long",
        "previous_position": "flat",
        "market_position": "long",
    }))

    assert response.status_code == 400
    assert response_json(response)["reason"] == "missing_signal_identity"
    assert ctx.queue.commands == []


@pytest.mark.asyncio
async def test_v2_close_without_active_trade_is_successful_noop():
    repo = FakeRepo()
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "schema_version": "2",
        "passphrase": "secret",
        "source_id": "flat-source-903",
        "source_name": "Already Flat Strategy",
        "signal_code": "Close",
        "explicit_action": "CLOSE_LONG",
        "bar_time": 1785202200000,
    }))

    assert response.status_code == 200
    assert response_json(response)["status"] == "noop"
    assert response_json(response)["reason"] == "no_matching_trade"
    assert repo.status_updates[-1][1] == "ACCEPTED"
    assert ctx.queue.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("explicit_action", "expected_spread"), [
    ("OPEN_LONG", "PUT_CREDIT"),
    ("OPEN_SHORT", "CALL_CREDIT"),
])
async def test_v3_without_explicit_spread_type_uses_each_signal_position(
        explicit_action, expected_spread):
    repo = FakeRepo()
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "schema_version": "3", "passphrase": "secret",
        "source_id": "put-v3", "source_name": "PUT V3",
        "signal_code": expected_spread, "explicit_action": explicit_action,
        "bar_time": 1785202080000,
    }))
    assert response.status_code == 202
    assert repo.stored_payload["spread_type"] == expected_spread


@pytest.mark.asyncio
async def test_v3_explicit_put_is_queued_without_direction_guessing():
    repo = FakeRepo()
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "schema_version": "3", "passphrase": "secret",
        "source_id": "put-v3", "source_name": "PUT V3",
        "spread_type": "PUT_CREDIT", "signal_code": "PUT_CREDIT",
        "explicit_action": "OPEN_LONG", "bar_time": 1785202080000,
    }))
    assert response.status_code == 202
    assert repo.stored_payload["spread_type"] == "PUT_CREDIT"


@pytest.mark.asyncio
async def test_v3_fixed_legacy_spread_does_not_block_each_signal_direction():
    repo = FakeRepo()
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "schema_version": "3", "passphrase": "secret",
        "source_id": "bad-v3", "source_name": "BAD V3",
        "spread_type": "PUT_CREDIT", "signal_code": "CALL_CREDIT",
        "explicit_action": "OPEN_SHORT", "bar_time": 1785202080000,
    }))
    assert response.status_code == 202
    assert repo.stored_payload["spread_type"] == "CALL_CREDIT"
