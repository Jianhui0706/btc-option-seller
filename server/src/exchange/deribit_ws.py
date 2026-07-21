"""Minimal asyncio JSON-RPC-over-WebSocket client for Deribit v2 API.

支持:请求/响应、订阅回调、client_credentials 认证与令牌自动刷新、
服务端心跳(test_request -> public/test)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import websockets

log = logging.getLogger("deribit.ws")

MAINNET_URL = "wss://www.deribit.com/ws/api/v2"
TESTNET_URL = "wss://test.deribit.com/ws/api/v2"


class DeribitError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message} {data if data is not None else ''}")
        self.code = code
        self.message = message
        self.data = data


class DeribitWS:
    def __init__(
        self,
        url: str = TESTNET_URL,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        scope: Optional[str] = None,
    ):
        self.url = url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self._ws: Optional[websockets.ClientConnection] = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._sub_callbacks: dict[str, Callable[[str, Any], Awaitable[None] | None]] = {}
        self._sub_private: dict[str, bool] = {}   # channel -> 是否私有,重连后恢复订阅用
        self._recv_task: Optional[asyncio.Task] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._access_token: Optional[str] = None
        self._closed = False

    # ---------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        await self._connect_once()

    async def _connect_once(self) -> None:
        self._ws = await websockets.connect(self.url, max_size=2**24)
        self._recv_task = asyncio.create_task(self._recv_loop())
        # 让服务器每 30s 心跳一次,连接死掉能尽快发现
        await self.request("public/set_heartbeat", {"interval": 30})
        if self.client_id and self.client_secret:
            await self._auth()
        # 断线重连后恢复全部订阅
        pub = [ch for ch, priv in self._sub_private.items() if not priv]
        priv = [ch for ch, priv in self._sub_private.items() if priv]
        if pub:
            await self.request("public/subscribe", {"channels": pub})
        if priv:
            await self.request("private/subscribe", {"channels": priv})

    async def close(self) -> None:
        self._closed = True
        for task in (self._refresh_task, self._recv_task):
            if task:
                task.cancel()
        if self._ws:
            await self._ws.close()

    @property
    def authenticated(self) -> bool:
        return self._access_token is not None

    # ---------------------------------------------------------------- core rpc

    async def request(self, method: str, params: Optional[dict] = None, timeout: float = 10.0) -> Any:
        assert self._ws is not None, "not connected"
        msg_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(msg_id, None)

    async def subscribe(
        self,
        channels: list[str],
        callback: Callable[[str, Any], Awaitable[None] | None],
        private: bool = False,
    ) -> list[str]:
        for ch in channels:
            self._sub_callbacks[ch] = callback
            self._sub_private[ch] = private
        method = "private/subscribe" if private else "public/subscribe"
        return await self.request(method, {"channels": channels})

    # ---------------------------------------------------------------- auth

    async def _auth(self) -> None:
        params = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if self.scope:
            params["scope"] = self.scope
        result = await self.request(
            "public/auth",
            params,
        )
        self._access_token = result["access_token"]
        expires_in = result.get("expires_in", 900)
        scope = result.get("scope")
        if self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._refresh_loop(expires_in))
        log.info("authenticated, token expires in %ss, scope=%s", expires_in, scope)

    async def _refresh_loop(self, expires_in: float) -> None:
        while not self._closed:
            await asyncio.sleep(max(60.0, expires_in / 2))
            try:
                await self._auth()
            except Exception as exc:  # noqa: BLE001
                log.warning("re-auth failed: %s", exc)

    # ---------------------------------------------------------------- recv loop

    async def _recv_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if "id" in msg and msg["id"] in self._pending:
                    fut = self._pending[msg["id"]]
                    if fut.done():
                        continue
                    if "error" in msg:
                        err = msg["error"]
                        fut.set_exception(
                            DeribitError(err.get("code", -1), err.get("message", "?"), err.get("data"))
                        )
                    else:
                        fut.set_result(msg.get("result"))
                elif msg.get("method") == "subscription":
                    params = msg["params"]
                    channel = params["channel"]
                    cb = self._sub_callbacks.get(channel)
                    if cb:
                        try:
                            res = cb(channel, params["data"])
                            if asyncio.iscoroutine(res):
                                asyncio.create_task(res)
                        except Exception:  # noqa: BLE001
                            log.exception("subscription callback failed: %s", channel)
                elif msg.get("method") == "heartbeat":
                    if msg.get("params", {}).get("type") == "test_request":
                        asyncio.create_task(self._pong())
        except websockets.ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001
            log.exception("recv loop crashed, treating as disconnect")
        # 连接断开:让所有等待中的请求立刻失败,并触发自动重连
        if self._closed:
            return
        log.error("websocket closed unexpectedly (%s), scheduling reconnect", self.url)
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("websocket closed"))
        self._access_token = None
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """指数退避自动重连;成功后 _connect_once 会恢复心跳、认证与全部订阅。"""
        delay = 2.0
        while not self._closed:
            try:
                await self._connect_once()
                log.info("reconnected to %s, restored %d subscription(s)",
                         self.url, len(self._sub_private))
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("reconnect to %s failed: %s (retry in %.0fs)", self.url, exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

    async def _pong(self) -> None:
        try:
            await self.request("public/test")
        except Exception:  # noqa: BLE001
            pass


def now_ms() -> int:
    return int(time.time() * 1000)
