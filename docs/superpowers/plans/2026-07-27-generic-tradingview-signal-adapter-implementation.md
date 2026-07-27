# TradingView 通用信号适配与 MACD 1 分钟测试 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 TradingView 只发送来源、信号码和仓位变化，由后端统一转换为开仓、平仓、反转动作，并在 Deribit Testnet 用内置 MACD Strategy 1 分钟信号跑通完整链路。

**Architecture:** 新增独立的信号协议适配器与后端默认执行配置，Webhook 只负责鉴权、标准化、去重和路由。开仓、平仓、反转继续通过单 worker 执行；反转命令必须同步确认旧仓已关闭后才能调用现有开仓编排。信号中心按 `source_id` 汇总、按 `source_name` 展示，不再合并心跳或推断在线状态。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic v2、aiosqlite、pytest/pytest-asyncio、原生 HTML/CSS/JavaScript、Node.js test runner、Vite、TradingView Pine Script v6。

## Global Constraints

- MACD 高频链路只允许 Deribit Testnet；Mainnet 不得使用未注册的通用默认执行配置。
- TradingView 决定交易意图；后端不得重新计算 MACD、DVOL、HV、CCI 或布林带条件。
- 通用信号不得携带 Delta、DTE、真实下单数量、分组或对冲参数。
- `long → short` 与 `short → long` 必须先确认平仓成功，再开目标方向仓位。
- 平仓失败、超时或旧仓仍存在时，反转不得继续开仓。
- 不发送、不依赖、不展示空闲 `STATUS` 心跳。
- 旧版复杂 TradingView 消息和旧 `/webhook/tradingview` URL 保持兼容。
- 同一信号身份只执行一次；重复事件必须返回 `duplicate`。
- 同一时间仍只允许一笔期权卖方父交易。

---

## File Structure

- `web/backend/src/signals/protocol.py`：把新版或旧版 TradingView 字段转换为统一信号意图。
- `web/backend/src/signals/profile.py`：把统一开仓方向绑定到 Testnet 后端执行参数。
- `web/backend/src/webhook.py`：鉴权、去重、持仓定位与命令路由，不承载策略计算。
- `web/backend/src/engine/reverser.py`：严格执行“平仓成功后再开仓”。
- `web/backend/src/engine/closer.py`：暴露可返回成功状态的单父交易平仓函数。
- `web/backend/src/engine/commands.py`：增加 `REVERSE` 命令。
- `web/backend/src/config.py`、`web/backend/config/base.yaml`：声明通用 Testnet 执行配置。
- `web/backend/src/store/schema.sql`、`db.py`、`repo.py`：保存并查询父交易的 `source_id`。
- `web/backend/src/api.py`：只返回关键交易信号，并按来源身份汇总。
- `web/frontend/index.html`：极简接入页与按来源名称展示的策略信号页。
- `web/backend/static/index.html`：由 Vite 构建生成，不手工编辑。
- `pine/*.pine`：停止生成 `STATUS` 心跳。
- `docs/MACD一分测试指南.md`：Testnet 手工验收步骤。

---

### Task 1: 通用 TradingView 信号协议

**Files:**
- Create: `web/backend/src/signals/__init__.py`
- Create: `web/backend/src/signals/protocol.py`
- Create: `web/backend/tests/test_signal_protocol.py`

**Interfaces:**
- Consumes: TradingView 原始 `dict[str, Any]`。
- Produces: `SignalAction`、`NormalizedIntent`、`normalize_signal(body, now_ms)`。
- `NormalizedIntent` 字段固定为 `source_id`、`source_name`、`signal_code`、`action`、`target_side`、`previous_position`、`market_position`、`symbol`、`timeframe`、`bar_time`、`event_time`、`signal_id`、`legacy`。

- [ ] **Step 1: 写仓位转换和幂等身份的失败测试**

```python
import pytest

from src.signals.protocol import SignalAction, normalize_signal


def test_macd_entry_becomes_open_long():
    result = normalize_signal({
        "schema_version": "2",
        "source_id": "src-macd-1m",
        "source_name": "MACD Strategy 1m Test",
        "signal_code": "MacdLE",
        "previous_position": "flat",
        "market_position": "long",
        "symbol": "BTCUSDT",
        "timeframe": "1",
        "bar_time": "1785141000000",
        "event_time": "1785141001000",
    }, now_ms=lambda: 1785141002000)

    assert result.action is SignalAction.OPEN_LONG
    assert result.target_side == "LONG"
    assert result.signal_id == (
        "src-macd-1m:MacdLE:OPEN_LONG:1785141000000"
    )


def test_macd_reversal_becomes_reverse_short():
    result = normalize_signal({
        "schema_version": "2",
        "source_id": "src-macd-1m",
        "source_name": "MACD Strategy 1m Test",
        "signal_code": "MacdSE",
        "previous_position": "long",
        "market_position": "short",
        "bar_time": "1785141060000",
    }, now_ms=lambda: 1785141061000)

    assert result.action is SignalAction.REVERSE_SHORT
    assert result.target_side == "SHORT"


def test_unchanged_position_is_not_a_trade_action():
    with pytest.raises(SignalProtocolError, match="no_position_change"):
        normalize_signal({
            "schema_version": "2",
            "source_id": "src-macd-1m",
            "source_name": "MACD Strategy 1m Test",
            "signal_code": "MacdLE",
            "previous_position": "long",
            "market_position": "long",
        }, now_ms=lambda: 1)
```

- [ ] **Step 2: 运行协议测试并确认按预期失败**

Run:

```powershell
cd web/backend
python -m pytest tests/test_signal_protocol.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'src.signals'`。

- [ ] **Step 3: 实现最小协议模块**

```python
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Mapping


class SignalProtocolError(ValueError):
    pass


class SignalAction(StrEnum):
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"
    REVERSE_LONG = "REVERSE_LONG"
    REVERSE_SHORT = "REVERSE_SHORT"


@dataclass(frozen=True)
class NormalizedIntent:
    source_id: str
    source_name: str
    signal_code: str
    action: SignalAction
    target_side: str | None
    previous_position: str
    market_position: str
    symbol: str
    timeframe: str
    bar_time: str
    event_time: str
    signal_id: str
    legacy: bool


POSITION_ACTIONS = {
    ("flat", "long"): SignalAction.OPEN_LONG,
    ("flat", "short"): SignalAction.OPEN_SHORT,
    ("long", "flat"): SignalAction.CLOSE_LONG,
    ("short", "flat"): SignalAction.CLOSE_SHORT,
    ("long", "short"): SignalAction.REVERSE_SHORT,
    ("short", "long"): SignalAction.REVERSE_LONG,
}


def normalize_signal(
    body: Mapping[str, Any],
    now_ms: Callable[[], int],
) -> NormalizedIntent:
    previous = str(
        body.get("previous_position", body.get("prevMarketPosition", ""))
    ).strip().lower()
    current = str(
        body.get("market_position", body.get("marketPosition", ""))
    ).strip().lower()
    action = POSITION_ACTIONS.get((previous, current))
    if action is None:
        if previous == current and previous in {"flat", "long", "short"}:
            raise SignalProtocolError("no_position_change")
        raise SignalProtocolError("unknown_action")

    source_id = str(
        body.get("source_id") or body.get("tv_id") or ""
    ).strip()
    source_name = str(
        body.get("source_name") or body.get("strategy") or ""
    ).strip()
    signal_code = str(
        body.get("signal_code") or body.get("order_id") or body.get("id") or ""
    ).strip()
    if not source_id or not source_name or not signal_code:
        raise SignalProtocolError("missing_signal_identity")

    bar_time = str(body.get("bar_time") or now_ms())
    event_time = str(body.get("event_time") or body.get("timestamp") or now_ms())
    target = (
        "LONG" if action in {SignalAction.OPEN_LONG, SignalAction.REVERSE_LONG}
        else "SHORT" if action in {
            SignalAction.OPEN_SHORT, SignalAction.REVERSE_SHORT
        } else None
    )
    signal_id = f"{source_id}:{signal_code}:{action.value}:{bar_time}"
    return NormalizedIntent(
        source_id=source_id,
        source_name=source_name,
        signal_code=signal_code,
        action=action,
        target_side=target,
        previous_position=previous,
        market_position=current,
        symbol=str(body.get("symbol") or ""),
        timeframe=str(body.get("timeframe") or body.get("period") or ""),
        bar_time=bar_time,
        event_time=event_time,
        signal_id=signal_id,
        legacy=str(body.get("schema_version") or "1") != "2",
    )
```

Extend `normalize_signal` in the same step with explicit aliases:

```python
EXPLICIT_ACTIONS = {
    "OPEN_LONG": SignalAction.OPEN_LONG,
    "OPEN_SHORT": SignalAction.OPEN_SHORT,
    "CLOSE_LONG": SignalAction.CLOSE_LONG,
    "CLOSE_SHORT": SignalAction.CLOSE_SHORT,
    "REVERSE_LONG": SignalAction.REVERSE_LONG,
    "REVERSE_SHORT": SignalAction.REVERSE_SHORT,
}
```

Use `explicit_action` first when it matches `EXPLICIT_ACTIONS`。Also support direction-neutral custom Pine actions deterministically:

```python
if explicit == "OPEN":
    action = (
        SignalAction.OPEN_LONG if current == "long"
        else SignalAction.OPEN_SHORT if current == "short"
        else None
    )
elif explicit in {"CLOSE", "EXIT"}:
    action = (
        SignalAction.CLOSE_LONG if previous == "long"
        else SignalAction.CLOSE_SHORT if previous == "short"
        else None
    )
elif explicit == "REVERSE":
    action = (
        SignalAction.REVERSE_LONG if current == "long"
        else SignalAction.REVERSE_SHORT if current == "short"
        else None
    )
```

An empty built-in MACD placeholder falls back to `POSITION_ACTIONS`。

- [ ] **Step 4: 增加旧字段兼容测试并跑绿**

```python
def test_legacy_camel_case_positions_and_order_id_still_normalize():
    result = normalize_signal({
        "strategy": "LEGACY",
        "tv_id": 77,
        "order_id": "PUT_CREDIT",
        "prevMarketPosition": "flat",
        "marketPosition": "long",
        "bar_time": 123,
    }, now_ms=lambda: 456)

    assert result.source_id == "77"
    assert result.source_name == "LEGACY"
    assert result.signal_code == "PUT_CREDIT"
    assert result.action is SignalAction.OPEN_LONG
    assert result.legacy is True
```

Run:

```powershell
cd web/backend
python -m pytest tests/test_signal_protocol.py -v
```

Expected: all protocol tests pass。

- [ ] **Step 5: 提交协议模块**

```powershell
git add web/backend/src/signals web/backend/tests/test_signal_protocol.py
git commit -m "feat: normalize generic TradingView intents"
```

---

### Task 2: Testnet 默认执行配置

**Files:**
- Create: `web/backend/src/signals/profile.py`
- Create: `web/backend/tests/test_signal_profile.py`
- Modify: `web/backend/src/config.py`
- Modify: `web/backend/config/base.yaml`

**Interfaces:**
- Consumes: `NormalizedIntent`、`SignalExecutionCfg`、`is_testnet: bool`。
- Produces: `apply_default_profile(intent, cfg, is_testnet) -> dict[str, Any]`。
- Raises: `SignalProfileError("test_source_requires_testnet")`。

- [ ] **Step 1: 写默认选腿参数与 Mainnet 禁止的失败测试**

```python
from unittest.mock import AsyncMock
from types import SimpleNamespace
import pytest

from src.signals.profile import SignalProfileError, apply_default_profile
from src.signals.protocol import SignalAction, NormalizedIntent


def intent(action: SignalAction, target: str) -> NormalizedIntent:
    return NormalizedIntent(
        source_id="src-macd-1m",
        source_name="MACD Strategy 1m Test",
        signal_code="MacdLE",
        action=action,
        target_side=target,
        previous_position="flat",
        market_position=target.lower(),
        symbol="BTCUSDT",
        timeframe="1",
        bar_time="1",
        event_time="2",
        signal_id=f"sig-{action}",
        legacy=False,
    )


def test_testnet_default_profile_fills_backend_execution_parameters():
    cfg = SimpleNamespace(
        testnet_only=True,
        long_spread_type="PUT_CREDIT",
        short_spread_type="CALL_CREDIT",
        sell_delta=0.20,
        dte_min=20,
        dte_max=35,
    )
    result = apply_default_profile(
        intent(SignalAction.OPEN_LONG, "LONG"), cfg, is_testnet=True)

    assert result["spread_type"] == "PUT_CREDIT"
    assert result["sell_delta"] == 0.20
    assert result["dte_min"] == 20
    assert result["dte_max"] == 35
    assert "quantity" not in result


def test_generic_default_profile_is_rejected_on_mainnet():
    cfg = SimpleNamespace(testnet_only=True)
    with pytest.raises(
        SignalProfileError, match="test_source_requires_testnet"
    ):
        apply_default_profile(
            intent(SignalAction.OPEN_LONG, "LONG"), cfg, is_testnet=False)
```

- [ ] **Step 2: 运行配置测试并确认失败**

Run:

```powershell
cd web/backend
python -m pytest tests/test_signal_profile.py -v
```

Expected: import fails because `src.signals.profile` does not exist。

- [ ] **Step 3: 添加 Pydantic 配置类型和 YAML 默认值**

Add to `config.py`:

```python
class SignalExecutionCfg(BaseModel):
    testnet_only: bool = True
    long_spread_type: str = "PUT_CREDIT"
    short_spread_type: str = "CALL_CREDIT"
    sell_delta: float = 0.20
    dte_min: float = 20.0
    dte_max: float = 35.0


class AppConfig(BaseModel):
    signal_execution: SignalExecutionCfg = SignalExecutionCfg()
```

Add to `config/base.yaml`:

```yaml
signal_execution:
  testnet_only: true
  long_spread_type: PUT_CREDIT
  short_spread_type: CALL_CREDIT
  sell_delta: 0.20
  dte_min: 20
  dte_max: 35
```

- [ ] **Step 4: 实现 `apply_default_profile`**

```python
from typing import Any

from .protocol import NormalizedIntent


class SignalProfileError(ValueError):
    pass


def apply_default_profile(
    intent: NormalizedIntent,
    cfg: Any,
    is_testnet: bool,
) -> dict[str, Any]:
    if cfg.testnet_only and not is_testnet:
        raise SignalProfileError("test_source_requires_testnet")
    if intent.target_side not in {"LONG", "SHORT"}:
        return {}
    spread_type = (
        cfg.long_spread_type
        if intent.target_side == "LONG"
        else cfg.short_spread_type
    )
    return {
        "spread_type": spread_type,
        "sell_delta": float(cfg.sell_delta),
        "dte_min": float(cfg.dte_min),
        "dte_max": float(cfg.dte_max),
        "qty_type": "risk_budget",
    }
```

- [ ] **Step 5: 运行配置与既有配置测试**

Run:

```powershell
cd web/backend
python -m pytest tests/test_signal_profile.py tests/test_cloud_deployment.py -v
```

Expected: all selected tests pass。

- [ ] **Step 6: 提交执行配置**

```powershell
git add web/backend/src/signals/profile.py web/backend/src/config.py web/backend/config/base.yaml web/backend/tests/test_signal_profile.py
git commit -m "feat: add testnet signal execution profile"
```

---

### Task 3: 持久化信号来源身份

**Files:**
- Modify: `web/backend/src/store/schema.sql`
- Modify: `web/backend/src/store/db.py`
- Modify: `web/backend/src/store/repo.py`
- Modify: `web/backend/tests/test_db_schema.py`

**Interfaces:**
- Changes: `Repo.create_parent(..., source_id: str | None = None)`。
- Produces: `Repo.active_parent_by_source_id(source_id: str) -> dict | None`。

- [ ] **Step 1: 写增量迁移与来源查询失败测试**

```python
@pytest.mark.asyncio
async def test_parent_trade_source_identity_is_migrated_and_indexed(tmp_path):
    db = Database(tmp_path / "source-id.db")
    conn = await db.open()
    try:
        parent_rows = await (await conn.execute(
            "PRAGMA table_info(parent_trades)")).fetchall()
        indexes = await (await conn.execute(
            "PRAGMA index_list(parent_trades)")).fetchall()

        assert "source_id" in {row[1] for row in parent_rows}
        assert "idx_parent_trades_source_id" in {row[1] for row in indexes}
    finally:
        await db.close()
```

Add a repository behavior test:

```python
@pytest.mark.asyncio
async def test_active_parent_is_located_by_source_id(tmp_path):
    db = Database(tmp_path / "source-query.db")
    conn = await db.open()
    try:
        repo = Repo(conn)
        await repo.insert_signal(
            "sig-1", "OPEN_REQUEST", "MACD Strategy 1m Test",
            "PUT_CREDIT", {"source": "tradingview"})
        parent_id = await repo.create_parent(
            "sig-1", "PUT_CREDIT", None, source_id="src-macd-1m")

        found = await repo.active_parent_by_source_id("src-macd-1m")

        assert found["id"] == parent_id
    finally:
        await db.close()
```

- [ ] **Step 2: 运行数据库测试并确认失败**

Run:

```powershell
cd web/backend
python -m pytest tests/test_db_schema.py -v
```

Expected: `source_id` column/index assertions fail。

- [ ] **Step 3: 添加列、索引和增量迁移**

In `schema.sql`, add to `parent_trades`:

```sql
source_id TEXT,
```

In `db.py`, add:

```python
"ALTER TABLE parent_trades ADD COLUMN source_id TEXT",
```

and:

```python
await conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_parent_trades_source_id "
    "ON parent_trades(source_id)")
```

- [ ] **Step 4: 更新仓库写入与查询**

Change `Repo.create_parent` to insert `source_id`:

```python
async def create_parent(
    self,
    signal_id: str,
    spread_type: str,
    dvol_tier: Optional[int],
    trade_id: Optional[str] = None,
    source_id: Optional[str] = None,
) -> int:
```

Add:

```python
async def active_parent_by_source_id(
    self, source_id: str
) -> Optional[dict]:
    q = ",".join("?" * len(ACTIVE_TRADE_STATES))
    cur = await self.conn.execute(
        f"SELECT * FROM parent_trades "
        f"WHERE source_id=? AND state IN ({q}) "
        "ORDER BY id DESC LIMIT 1",
        (source_id, *ACTIVE_TRADE_STATES),
    )
    row = await cur.fetchone()
    return dict(row) if row else None
```

- [ ] **Step 5: 运行数据库测试**

Run:

```powershell
cd web/backend
python -m pytest tests/test_db_schema.py tests/test_state_machine.py -v
```

Expected: all selected tests pass。

- [ ] **Step 6: 提交来源持久化**

```powershell
git add web/backend/src/store web/backend/tests/test_db_schema.py
git commit -m "feat: persist TradingView source identity"
```

---

### Task 4: Webhook 路由通用开仓、平仓与反转

**Files:**
- Modify: `web/backend/src/webhook.py`
- Modify: `web/backend/tests/test_tradingview_strategy_webhook.py`
- Modify: `web/backend/tests/test_webhook_payload.py`

**Interfaces:**
- Consumes: `normalize_signal`、`apply_default_profile`。
- Produces queue calls: `queue.open(signal)`、`queue.normal_close(parent_id, reason)`、`queue.reverse(parent_id, signal, reason)`。
- HTTP results retain `queued`/`duplicate`; unmatched close returns `noop` without touching another source。

- [ ] **Step 1: 写 MACD 开仓、无仓平仓与反转路由失败测试**

```python
@pytest.mark.asyncio
async def test_macd_v2_entry_uses_backend_profile_and_queues_open():
    repo = FakeRepo()
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "schema_version": "2",
        "passphrase": "secret",
        "source_id": "src-macd-1m",
        "source_name": "MACD Strategy 1m Test",
        "signal_code": "MacdLE",
        "previous_position": "flat",
        "market_position": "long",
        "symbol": "BTCUSDT",
        "timeframe": "1",
        "bar_time": 1785141000000,
    }))

    assert response.status_code == 202
    assert repo.stored_payload["action"] == "OPEN_LONG"
    assert repo.stored_payload["spread_type"] == "PUT_CREDIT"
    assert repo.stored_payload["sell_delta"] == 0.20
    assert "quantity" not in repo.stored_payload
    assert ctx.queue.commands == [("open", repo.stored_payload)]


@pytest.mark.asyncio
async def test_unmatched_close_is_idempotent_noop():
    repo = FakeRepo(parent=None)
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "schema_version": "2",
        "passphrase": "secret",
        "source_id": "src-macd-1m",
        "source_name": "MACD Strategy 1m Test",
        "signal_code": "MacdLX",
        "previous_position": "long",
        "market_position": "flat",
        "bar_time": 1785141060000,
    }))

    assert response.status_code == 200
    assert response_json(response)["status"] == "noop"
    assert ctx.queue.commands == []


@pytest.mark.asyncio
async def test_macd_reversal_queues_one_reverse_command():
    repo = FakeRepo(parent={"id": 41, "source_id": "src-macd-1m"})
    ctx = make_ctx(repo)
    response = await tradingview_signal_webhook(FakeRequest(ctx, {
        "schema_version": "2",
        "passphrase": "secret",
        "source_id": "src-macd-1m",
        "source_name": "MACD Strategy 1m Test",
        "signal_code": "MacdSE",
        "previous_position": "long",
        "market_position": "short",
        "bar_time": 1785141120000,
    }))

    assert response.status_code == 202
    assert ctx.queue.commands == [
        ("reverse", 41, repo.stored_payload, "REVERSE_SHORT")
    ]
```

- [ ] **Step 2: 运行 Webhook 测试并确认失败**

Run:

```powershell
cd web/backend
python -m pytest tests/test_tradingview_strategy_webhook.py -v
```

Expected: new generic payload tests fail because current model requires old execution fields/action mapping。

- [ ] **Step 3: 为测试替身补齐新接口**

Add to `FakeQueue`:

```python
def reverse(self, parent_id, signal, reason):
    return ("reverse", parent_id, signal, reason)
```

Add to `FakeRepo`:

```python
async def active_parent_by_source_id(self, source_id):
    self.looked_up_source_id = source_id
    return self.parent
```

Add `signal_execution` and `env.is_testnet=True` to `make_ctx`。

- [ ] **Step 4: 在 Webhook 中增加 v2 分支**

After authentication and before legacy normalization:

```python
is_generic = (
    str(body.get("schema_version") or "") == "2"
    or bool(body.get("source_id"))
)
```

For generic messages:

1. Call `normalize_signal(body, now_ms=lambda: int(time.time() * 1000))`。
2. Convert the dataclass to a stored payload with `source="tradingview"`。
3. For open/reverse targets call `apply_default_profile`。
4. Insert with events `OPEN_REQUEST`、`CLOSE_REQUEST`、`REVERSE_REQUEST`。
5. Route close/reverse by `active_parent_by_source_id`。
6. Keep the existing legacy path unchanged。

Use this exact action classification:

```python
OPEN_ACTIONS = {SignalAction.OPEN_LONG, SignalAction.OPEN_SHORT}
CLOSE_ACTIONS = {SignalAction.CLOSE_LONG, SignalAction.CLOSE_SHORT}
REVERSE_ACTIONS = {
    SignalAction.REVERSE_LONG, SignalAction.REVERSE_SHORT,
}
```

- [ ] **Step 5: 更新开仓持久化调用**

In `orchestrator.handle_open`, pass the source:

```python
parent_id = await repo.create_parent(
    signal_id,
    spread_type,
    signal.get("dvol_tier"),
    trade_id=signal.get("trade_id"),
    source_id=signal.get("source_id"),
)
```

- [ ] **Step 6: 运行新旧 Webhook 回归**

Run:

```powershell
cd web/backend
python -m pytest tests/test_signal_protocol.py tests/test_signal_profile.py tests/test_webhook_payload.py tests/test_tradingview_strategy_webhook.py -v
```

Expected: generic MACD and legacy payload tests all pass。

- [ ] **Step 7: 提交 Webhook 路由**

```powershell
git add web/backend/src/webhook.py web/backend/src/engine/orchestrator.py web/backend/tests/test_tradingview_strategy_webhook.py web/backend/tests/test_webhook_payload.py
git commit -m "feat: route generic TradingView actions"
```

---

### Task 5: 安全反转执行命令

**Files:**
- Create: `web/backend/src/engine/reverser.py`
- Create: `web/backend/tests/test_reverse_execution.py`
- Modify: `web/backend/src/engine/closer.py`
- Modify: `web/backend/src/engine/commands.py`
- Modify: `web/backend/src/app.py`

**Interfaces:**
- Produces: `closer.close_parent(ctx, parent_id, reason) -> bool`。
- Produces: `reverser.handle_reverse(ctx, payload) -> None`。
- Produces: `CommandQueue.reverse(parent_id, signal, reason) -> Command`。

- [ ] **Step 1: 写反转顺序和失败中止测试**

```python
from types import SimpleNamespace
import pytest

from src.engine import reverser


@pytest.mark.asyncio
async def test_reverse_opens_only_after_close_succeeds(monkeypatch):
    calls = []

    async def close_parent(ctx, parent_id, reason, enqueue_retry):
        calls.append(("close", parent_id, reason, enqueue_retry))
        return True

    async def open_signal(ctx, payload):
        calls.append(("open", payload["signal"]["action"]))

    monkeypatch.setattr(reverser.closer, "close_parent", close_parent)
    monkeypatch.setattr(reverser.orchestrator, "handle_open", open_signal)
    ctx = SimpleNamespace(repo=SimpleNamespace())

    await reverser.handle_reverse(ctx, {
        "parent_id": 41,
        "reason": "REVERSE_SHORT",
        "signal": {"signal_id": "sig-r", "action": "REVERSE_SHORT"},
    })

    assert calls == [
        ("close", 41, "REVERSE_SHORT", False),
        ("open", "REVERSE_SHORT"),
    ]


@pytest.mark.asyncio
async def test_reverse_does_not_open_when_close_fails(monkeypatch):
    opened = []

    async def close_parent(ctx, parent_id, reason, enqueue_retry):
        return False

    async def open_signal(ctx, payload):
        opened.append(payload)

    monkeypatch.setattr(reverser.closer, "close_parent", close_parent)
    monkeypatch.setattr(reverser.orchestrator, "handle_open", open_signal)
    repo = SimpleNamespace(
        set_signal_status=AsyncMock(),
    )
    ctx = SimpleNamespace(repo=repo)
    signal = {"signal_id": "sig-r", "action": "REVERSE_SHORT"}

    await reverser.handle_reverse(ctx, {
        "parent_id": 41, "reason": "REVERSE_SHORT", "signal": signal,
    })

    assert opened == []
    repo.set_signal_status.assert_awaited_once_with(
        "sig-r", "REJECTED", "reverse_close_failed")
```

- [ ] **Step 2: 运行反转测试并确认失败**

Run:

```powershell
cd web/backend
python -m pytest tests/test_reverse_execution.py -v
```

Expected: import fails because `src.engine.reverser` does not exist。

- [ ] **Step 3: 抽取返回布尔结果的单交易平仓函数**

Refactor current `handle_normal_close` body into:

```python
async def close_parent(
    ctx: Ctx,
    parent_id: int,
    reason: str,
    enqueue_retry: bool = True,
) -> bool:
```

Return `False` when:

- parent is missing or not closeable；
- sell leg remains open；
- managed hedge remains non-zero；
- execution reports incomplete closure。

Return `True` only after parent transitions to `CLOSED`。

Keep wrapper:

```python
async def handle_normal_close(ctx: Ctx, payload: dict) -> None:
    await close_parent(
        ctx,
        int(payload["parent_id"]),
        payload.get("reason", "NORMAL"),
    )
```

Keep the existing retry queue behavior only when `enqueue_retry=True`。When called by reversal, pass `enqueue_retry=False`; the boolean result is authoritative for the current reverse command。

- [ ] **Step 4: 实现反转处理器**

```python
from ..context import Ctx
from . import closer, orchestrator


async def handle_reverse(ctx: Ctx, payload: dict) -> None:
    signal = payload["signal"]
    closed = await closer.close_parent(
        ctx,
        int(payload["parent_id"]),
        payload.get("reason", signal["action"]),
        enqueue_retry=False,
    )
    if not closed:
        await ctx.repo.set_signal_status(
            signal["signal_id"], "REJECTED", "reverse_close_failed")
        return
    await orchestrator.handle_open(ctx, {"signal": signal})
```

- [ ] **Step 5: 注册 `REVERSE` 命令**

Add to `CommandQueue`:

```python
def reverse(self, parent_id: int, signal: dict, reason: str) -> Command:
    return Command(
        "REVERSE",
        PRIO_NORMAL_CLOSE,
        {"parent_id": parent_id, "signal": signal, "reason": reason},
    )
```

Add to `app.py`:

```python
"REVERSE": reverser.handle_reverse,
```

- [ ] **Step 6: 运行反转、平仓、worker 测试**

Run:

```powershell
cd web/backend
python -m pytest tests/test_reverse_execution.py tests/test_state_machine.py tests/test_force_confirmation.py -v
```

Expected: all selected tests pass and reverse failure never calls open。

- [ ] **Step 7: 提交安全反转**

```powershell
git add web/backend/src/engine web/backend/src/app.py web/backend/tests/test_reverse_execution.py
git commit -m "feat: execute safe close-before-open reversals"
```

---

### Task 6: 关键交易信号 API 与简化页面

**Files:**
- Modify: `web/backend/src/api.py`
- Modify: `web/backend/tests/test_dashboard_pages.py`
- Modify: `web/frontend/index.html`
- Modify: `web/frontend/tests/tradingview-setup.test.mjs`
- Modify: `web/frontend/tests/multi-strategy-center.test.mjs`
- Generated: `web/backend/static/index.html`

**Interfaces:**
- `GET /api/tradingview_setup` returns schema v2 setup metadata。
- `GET /api/signals` returns only open/close/reverse signal items and source summaries。
- Frontend generator emits only the v2 protocol fields。

- [ ] **Step 1: 改写后端 API 失败测试**

Replace heartbeat merge expectations with:

```python
@pytest.mark.asyncio
async def test_signal_feed_contains_only_key_trading_events(tmp_path):
    db = Database(tmp_path / "signals.db")
    conn = await db.open()
    try:
        repo = Repo(conn)
        await repo.insert_signal(
            "sig-open", "OPEN_REQUEST", "MACD Strategy 1m Test",
            "PUT_CREDIT", {
                "source": "tradingview",
                "source_id": "src-macd-1m",
                "source_name": "MACD Strategy 1m Test",
                "signal_code": "MacdLE",
                "action": "OPEN_LONG",
                "symbol": "BTCUSDT",
                "timeframe": "1",
            })
        await repo.insert_signal(
            "sig-reverse", "REVERSE_REQUEST", "MACD Strategy 1m Test",
            "CALL_CREDIT", {
                "source": "tradingview",
                "source_id": "src-macd-1m",
                "source_name": "MACD Strategy 1m Test",
                "signal_code": "MacdSE",
                "action": "REVERSE_SHORT",
            })
        await repo.record_pine_heartbeat({
            "event": "STATUS", "strategy": "legacy", "bar_ts": 1,
            "received_at": 1,
        })
        request = make_signal_request(repo)

        result = await signal_feed(request, limit=20)

        assert [item["kind"] for item in result["items"]] == [
            "REVERSE_SIGNAL", "OPEN_SIGNAL",
        ]
        assert result["summary"]["reverse_signals"] == 1
        assert "heartbeats" not in result["summary"]
        assert result["strategies"][0]["source_name"] == (
            "MACD Strategy 1m Test")
        assert result["strategies"][0]["source_id"] == "src-macd-1m"
```

Update setup expectation:

```python
assert result["schema_version"] == "2"
assert result["condition"] == "策略 → 订单成交"
assert "account_name" not in result
```

- [ ] **Step 2: 运行后端页面测试并确认失败**

Run:

```powershell
cd web/backend
python -m pytest tests/test_dashboard_pages.py -v
```

Expected: existing API still includes heartbeats and v1 setup metadata。

- [ ] **Step 3: 改造 `/api/signals`**

Remove `recent_pine_heartbeats` merge and all heartbeat fields。

Classify item kinds by stored action:

```python
action = str(payload.get("action") or "")
item["kind"] = (
    "REVERSE_SIGNAL" if action.startswith("REVERSE_")
    else "OPEN_SIGNAL" if action.startswith("OPEN_")
    else "CLOSE_SIGNAL"
)
```

Group with:

```python
source_id = str(payload.get("source_id") or payload.get("tv_id") or "")
source_name = str(
    payload.get("source_name") or item.get("strategy") or "未命名来源")
```

Summary fields are exactly:

```python
{
    "total": ...,
    "open_signals": ...,
    "close_signals": ...,
    "reverse_signals": ...,
    "accepted": ...,
    "rejected": ...,
    "pending": ...,
}
```

- [ ] **Step 4: 改造 setup API**

Return:

```python
{
    "schema_version": "2",
    "webhook_path": "/webhook/signal",
    "legacy_webhook_path": "/webhook/tradingview",
    "passphrase": ctx.cfg.env.webhook_auth_token,
    "passphrase_configured": bool(ctx.cfg.env.webhook_auth_token),
    "condition": "策略 → 订单成交",
    "message_mode": "generic_position_transition",
}
```

- [ ] **Step 5: 先改前端测试定义极简 UI**

`tradingview-setup.test.mjs` must assert:

```javascript
assert.match(html,/id="tvSourceName"/);
assert.doesNotMatch(html,/id="tvAccountName"/);
assert.doesNotMatch(html,/id="tvAlertId"/);
assert.doesNotMatch(html,/id="tvPassphrase"/);
assert.match(html,/"schema_version": "2"/);
assert.match(html,/"source_id": \$\{JSON\.stringify\(sourceId\)\}/);
assert.match(html,/"source_name": \$\{JSON\.stringify\(sourceName\)\}/);
assert.match(html,/"signal_code": "\{\{strategy\.order\.id\}\}"/);
assert.match(html,/"previous_position": "\{\{strategy\.prev_market_position\}\}"/);
assert.match(html,/"market_position": "\{\{strategy\.market_position\}\}"/);
assert.doesNotMatch(html,/sell_delta|dte_min|group_split_count/);
assert.match(html,/订单成交/);
assert.doesNotMatch(html,/alert\(\) 函数调用/);
```

`multi-strategy-center.test.mjs` must assert:

```javascript
assert.match(page,/开仓 \/ 平仓 \/ 反转/);
assert.match(html,/item\.source_name/);
assert.match(html,/payload\.signal_code/);
assert.match(html,/payload\.action/);
assert.doesNotMatch(page,/近期在线|连接回报|连接正常|连接超时/);
```

- [ ] **Step 6: 运行 Node 测试并确认失败**

Run:

```powershell
cd web/frontend
node --test tests/tradingview-setup.test.mjs tests/multi-strategy-center.test.mjs
```

Expected: tests fail against current complex setup and heartbeat UI。

- [ ] **Step 7: 实现极简接入页**

Visible inputs:

```html
<label for="tvSourceName">TradingView 信号来源名称</label>
<input id="tvSourceName" placeholder="例如 MACD Strategy 1m Test"
       oninput="updateTvTemplate()">
```

Keep `passphrase` and generated `source_id` only in JavaScript state, not visible form controls。

Build message:

```javascript
function buildTvAlertTemplate(){
  const sourceName=$("tvSourceName").value.trim();
  const sourceId=ensureTvSourceId();
  const secret=tvSetup?.passphrase||"";
  return `{
  "schema_version": "2",
  "passphrase": ${JSON.stringify(secret)},
  "source_id": ${JSON.stringify(sourceId)},
  "source_name": ${JSON.stringify(sourceName)},
  "signal_code": "{{strategy.order.id}}",
  "explicit_action": "{{strategy.order.alert_message}}",
  "previous_position": "{{strategy.prev_market_position}}",
  "market_position": "{{strategy.market_position}}",
  "symbol": "{{ticker}}",
  "timeframe": "{{interval}}",
  "bar_time": "{{time}}",
  "event_time": "{{timenow}}"
}`;
}
```

- [ ] **Step 8: 实现按来源名称显示的策略卡和关键事件列表**

Card title uses:

```javascript
item.source_name||"未命名来源"
```

Signal row identity uses:

```javascript
`${payload.signal_code||"—"} · ${payload.action||"—"}`
```

Remove connection age calculations, heartbeat filters, online badges, and all text implying no signal equals disconnection。

- [ ] **Step 9: 运行 API 和前端测试**

Run:

```powershell
cd web/backend
python -m pytest tests/test_dashboard_pages.py -v
cd ../frontend
node --test tests/tradingview-setup.test.mjs tests/multi-strategy-center.test.mjs
```

Expected: selected Python and Node tests pass。

- [ ] **Step 10: 构建生产静态页并复跑同步测试**

Run:

```powershell
cd web/frontend
npm run build
node --test tests/*.test.mjs
```

Expected: Vite build exits 0 and all frontend tests pass, including source/static equality。

- [ ] **Step 11: 提交 API 与页面**

```powershell
git add web/backend/src/api.py web/backend/tests/test_dashboard_pages.py web/frontend/index.html web/frontend/tests web/backend/static/index.html
git commit -m "feat: simplify TradingView signal center"
```

---

### Task 7: 停止心跳并补齐 MACD Testnet 验收说明

**Files:**
- Modify: `pine/btc_credit_spread_v2.pine`
- Modify: `pine/tv_strategy_a_standard.pine`
- Modify: `pine/tv_strategy_b_relaxed.pine`
- Modify: `web/backend/tests/test_pine_alert_delivery.py`
- Create: `docs/MACD一分测试指南.md`
- Modify: `docs/运行手册.md`

**Interfaces:**
- Pine only emits order-fill strategy events。
- Runbook gives exact built-in MACD alert setup and observable pass/fail evidence。

- [ ] **Step 1: 写 Pine 无心跳失败测试**

Add:

```python
@pytest.mark.parametrize("filename", [
    "btc_credit_spread_v2.pine",
    "tv_strategy_a_standard.pine",
    "tv_strategy_b_relaxed.pine",
])
def test_pine_strategies_do_not_emit_idle_heartbeats(filename):
    source = (PINE_DIR / filename).read_text(encoding="utf-8")

    assert r'\"event\":\"STATUS\"' not in source
    assert "f_status_json" not in source
    assert "heartbeat_on" not in source
    assert "alert(f_status_json" not in source
```

- [ ] **Step 2: 运行 Pine 测试并确认失败**

Run:

```powershell
cd web/backend
python -m pytest tests/test_pine_alert_delivery.py -v
```

Expected: all three heartbeat assertions fail against current scripts。

- [ ] **Step 3: 删除 Pine 心跳生成逻辑**

From all three Pine scripts remove:

- `heartbeat_on` input；
- `lastStatusT` and `statusCanFire`；
- `f_status_json()`；
- `alert(f_status_json(), alert.freq_all)` block；
- comments instructing users to select alert function calls。

Change the header instruction to:

```pine
// Alert 条件只选择“订单成交”；消息使用管理页面生成的通用 JSON 模板。
```

Do not change strategy entry/exit conditions in this task。

- [ ] **Step 4: 写 MACD 1 分钟测试指南**

The document must contain these exact checkpoints:

```markdown
1. Confirm `/status` reports `deribit_env=testnet`.
2. Enable trading only after checking the Testnet account.
3. Add TradingView built-in `MACD Strategy`.
4. Set the chart interval to `1m`.
5. Create an alert with condition `MACD Strategy → Order fills`.
6. Enter source name `MACD Strategy 1m Test` in the backend setup page.
7. Paste the generated Webhook URL and schema-v2 JSON.
8. Verify `MacdLE` or `MacdSE` appears with its normalized action.
9. Verify Deribit Testnet position matches the backend execution result.
10. On reversal, verify the old parent is `CLOSED` before the new parent enters `OPENING/OPEN`.
```

Also include rollback:

```markdown
- Click “暂停新开仓”.
- If a Testnet position remains, use “全账户强平”.
- Confirm Deribit Testnet is flat before clearing the force-close lock.
```

- [ ] **Step 5: 更新运行手册**

Replace all heartbeat setup and online-state language with:

- only order-fill events are sent；
- quiet periods are normal；
- source cards are created on first key trade signal；
- execution parameters live in `config/base.yaml` under `signal_execution`。

- [ ] **Step 6: 运行 Pine 与文档相关测试**

Run:

```powershell
cd web/backend
python -m pytest tests/test_pine_alert_delivery.py tests/test_dashboard_pages.py -v
```

Expected: selected tests pass。

- [ ] **Step 7: 提交心跳移除与测试指南**

```powershell
git add pine docs web/backend/tests/test_pine_alert_delivery.py
git commit -m "docs: add MACD testnet signal walkthrough"
```

---

### Task 8: 全量验证与交付检查

**Files:**
- Verify only; modify a failing file only through a new red-green cycle。

**Interfaces:**
- Consumes all deliverables from Tasks 1–7。
- Produces fresh test, build, and repository evidence。

- [ ] **Step 1: 运行完整后端测试**

Run:

```powershell
cd web/backend
python -m pytest -q
```

Expected: exit 0 with zero failed tests。

- [ ] **Step 2: 运行完整前端测试**

Run:

```powershell
cd web/frontend
node --test tests/*.test.mjs
```

Expected: exit 0 with zero failed tests。

- [ ] **Step 3: 重新构建生产静态文件**

Run:

```powershell
cd web/frontend
npm run build
```

Expected: exit 0 and `web/backend/static/index.html` is regenerated。

- [ ] **Step 4: 复跑生产静态同步与关键协议测试**

Run:

```powershell
cd web/frontend
node --test tests/tradingview-setup.test.mjs tests/multi-strategy-center.test.mjs
cd ../backend
python -m pytest tests/test_signal_protocol.py tests/test_signal_profile.py tests/test_tradingview_strategy_webhook.py tests/test_reverse_execution.py -q
```

Expected: both commands exit 0。

- [ ] **Step 5: 检查没有心跳或复杂 v2 字段残留**

Run:

```powershell
rg -n "heartbeat_on|f_status_json|近期在线|连接回报|plot_2|sell_delta.*strategy.order" pine web/frontend/index.html web/backend/static/index.html docs/MACD一分测试指南.md
```

Expected: no matches。

- [ ] **Step 6: 检查 diff 和未跟踪文件边界**

Run:

```powershell
git diff --check
git status --short
git log --oneline -10
```

Expected: no whitespace errors; only the user’s pre-existing untracked files remain; implementation commits are visible。

- [ ] **Step 7: 对照设计验收标准**

Verify from fresh evidence:

1. schema-v2 payload has source, code, positions and timestamps only；
2. MACD entry/reversal mappings are tested；
3. reverse close failure prevents open；
4. Testnet defaults fill execution parameters；
5. Mainnet default profile is rejected；
6. API and UI omit heartbeats；
7. legacy payload regression tests pass；
8. frontend source and production static file match。

- [ ] **Step 8: 准备人工 MACD Testnet 验收**

Provide the user:

- clickable `docs/MACD一分测试指南.md`；
- exact local start command `.\start.ps1`；
- instruction to confirm Testnet before enabling trading；
- expected first visible event: `MacdLE` or `MacdSE` plus normalized action；
- reminder that no signal during a quiet minute is not a connection failure。
