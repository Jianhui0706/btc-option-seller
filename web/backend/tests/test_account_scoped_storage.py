from types import SimpleNamespace

from src import config


def _env(*, client_id: str, testnet: bool = True):
    return SimpleNamespace(
        client_id=client_id,
        is_testnet=testnet,
    )


def test_account_scope_is_stable_and_does_not_expose_client_id():
    env = _env(client_id="sensitive-client-id")

    first = config.account_scope_key(env)
    second = config.account_scope_key(env)

    assert first == second
    assert first.startswith("testnet-")
    assert "sensitive-client-id" not in first


def test_different_accounts_and_environments_use_different_ledgers(monkeypatch,
                                                                  tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    testnet_a = config.db_path(_env(client_id="client-a"))
    testnet_b = config.db_path(_env(client_id="client-b"))
    mainnet_a = config.db_path(_env(client_id="client-a", testnet=False))

    assert len({testnet_a, testnet_b, mainnet_a}) == 3
    assert testnet_a.parent == tmp_path
    assert testnet_a.name.startswith("seller_v2_testnet-")
    assert mainnet_a.name.startswith("seller_v2_mainnet-")


def test_legacy_no_argument_path_remains_available(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    assert config.db_path() == tmp_path / "seller_v2.db"
