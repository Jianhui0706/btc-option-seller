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

    def put(self, command):
        self.commands.append(command)


class FakeRepo:
    conn = None

    def __init__(self, parent=None):
        self.parent = parent
        self.stored_payload = None
        self.status_updates = []
        self.open_parents_called = False

    async def insert_signal(self, signal_id, event, strategy, spread_type, payload):
        self.stored_payload = payload
        return True

    async def active_parent_by_trade_id(self, trade_id, strategy=""):
        self.looked_up_trade_id = trade_id
        self.looked_up_strategy = strategy
        return self.parent

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
    assert repo.open_parents_called is False
    assert ctx.queue.commands == [("close", 41, "TV_DVOL_EXIT")]


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
