"""
Crypto.com Exchange WebSocket client — market and user streams.
Market: H1 candles (BTC+ETH), D1 candles (BTC trend gate), order book, ticker.
User:   order updates, trade fills, balance updates.
FIX: Heartbeat timeout uses dedicated watchdog task, not inline check (was always False).
FIX: D1 channel subscribed; on_d1_candlestick callback added.
FIX: CoD confirmation read and logged before channel subscription.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Callable, Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from config import settings

logger = logging.getLogger(__name__)

_HEARTBEAT_TIMEOUT = 25  # seconds between messages before reconnect


class CryptoComWSClient:
    def __init__(
        self,
        on_candlestick:    Optional[Callable] = None,
        on_d1_candlestick: Optional[Callable] = None,
        on_book:           Optional[Callable] = None,
        on_ticker:         Optional[Callable] = None,
        on_order_update:   Optional[Callable] = None,
        on_trade_fill:     Optional[Callable] = None,
        on_balance_update: Optional[Callable] = None,
    ):
        self._on_candlestick    = on_candlestick
        self._on_d1_candlestick = on_d1_candlestick
        self._on_book           = on_book
        self._on_ticker         = on_ticker
        self._on_order_update   = on_order_update
        self._on_trade_fill     = on_trade_fill
        self._on_balance_update = on_balance_update
        self._running           = False

    # ── Auth helper ───────────────────────────────────────────────────────────

    def _auth_msg(self) -> Dict:
        nonce  = int(time.time() * 1000)
        req_id = nonce % 100000
        raw    = f"public/auth{req_id}{settings.API_KEY}{nonce}"
        sig    = hmac.new(settings.API_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
        return {
            "id": req_id, "method": "public/auth",
            "api_key": settings.API_KEY, "sig": sig, "nonce": nonce,
        }

    # ── Market stream ─────────────────────────────────────────────────────────

    async def _market_stream(self) -> None:
        channels = [
            f"candlestick.{settings.BTC_CANDLE_TF}.{settings.BTC_INSTRUMENT}",
            f"candlestick.{settings.ETH_CANDLE_TF}.{settings.ETH_INSTRUMENT}",
            f"candlestick.1D.{settings.BTC_INSTRUMENT}",  # FIX: D1 for BTC trend gate
            f"book.{settings.BTC_INSTRUMENT}.10",
            f"book.{settings.ETH_INSTRUMENT}.10",
            f"ticker.{settings.BTC_INSTRUMENT}",
            f"ticker.{settings.ETH_INSTRUMENT}",
        ]
        backoff = 1
        while self._running:
            try:
                logger.info("Connecting market WebSocket (%d channels)", len(channels))
                async with websockets.connect(
                    settings.WS_MARKET_URL,
                    ping_interval=None,
                    close_timeout=5,
                ) as ws:
                    backoff = 1
                    await ws.send(json.dumps({
                        "id": 1, "method": "subscribe",
                        "params": {"channels": channels},
                        "nonce": int(time.time() * 1000),
                    }))
                    logger.info("Market WS subscribed")

                    # FIX: heartbeat monitored by separate task, not inline after reset
                    last_msg_ts = [time.monotonic()]

                    async def _watchdog() -> None:
                        while True:
                            await asyncio.sleep(5)
                            age = time.monotonic() - last_msg_ts[0]
                            if age > _HEARTBEAT_TIMEOUT:
                                logger.warning("Market WS silent %.0fs — forcing reconnect", age)
                                await ws.close()
                                return

                    watchdog = asyncio.ensure_future(_watchdog())
                    try:
                        async for raw in ws:
                            last_msg_ts[0] = time.monotonic()
                            await self._handle_market(json.loads(raw), ws)
                    finally:
                        watchdog.cancel()

            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                logger.warning("Market WS disconnected: %s — retry in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as exc:
                logger.exception("Market WS unexpected error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle_market(self, msg: Dict, ws) -> None:
        method = msg.get("method", "")
        if method == "public/heartbeat":
            await ws.send(json.dumps({"id": msg.get("id"), "method": "public/respond-heartbeat"}))
            return
        if method != "subscribe":
            return

        result     = msg.get("result", {})
        channel    = result.get("channel", "")
        data       = result.get("data", [])
        instrument = result.get("instrument_name", "")

        if channel.startswith("candlestick"):
            if ".1D." in channel:
                if self._on_d1_candlestick:
                    for c in data:
                        await self._on_d1_candlestick(instrument, c)
            else:
                if self._on_candlestick:
                    for c in data:
                        await self._on_candlestick(instrument, c)
        elif channel.startswith("book"):
            if self._on_book:
                for b in data:
                    await self._on_book(instrument, b)
        elif channel.startswith("ticker"):
            if self._on_ticker:
                for t in data:
                    await self._on_ticker(instrument, t)

    # ── User stream ───────────────────────────────────────────────────────────

    async def _user_stream(self) -> None:
        channels = [
            f"user.order.{settings.BTC_INSTRUMENT}",
            f"user.order.{settings.ETH_INSTRUMENT}",
            f"user.trade.{settings.BTC_INSTRUMENT}",
            f"user.trade.{settings.ETH_INSTRUMENT}",
            "user.balance",
        ]
        backoff = 1
        while self._running:
            try:
                logger.info("Connecting user WebSocket")
                async with websockets.connect(
                    settings.WS_USER_URL,
                    ping_interval=None,
                    close_timeout=5,
                ) as ws:
                    backoff = 1

                    if not settings.DRY_RUN:
                        # Auth
                        await ws.send(json.dumps(self._auth_msg()))
                        auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                        if auth.get("code") != 0:
                            raise RuntimeError(f"User WS auth failed: {auth}")
                        logger.info("User WS authenticated")

                        # FIX: CoD — send and confirm before subscribing
                        cod_id = int(time.time() * 1000) % 100000
                        await ws.send(json.dumps({
                            "id": cod_id,
                            "method": "private/set-cancel-on-disconnect",
                            "api_key": settings.API_KEY,
                            "nonce": int(time.time() * 1000),
                            "params": {"scope": "CONNECTION"},
                        }))
                        try:
                            cod_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                            if cod_resp.get("code") == 0:
                                logger.info("Cancel-on-disconnect confirmed (user WS)")
                            else:
                                logger.warning("CoD response: %s", cod_resp)
                        except asyncio.TimeoutError:
                            logger.warning("CoD confirmation timeout — proceeding")

                    await ws.send(json.dumps({
                        "id": 2, "method": "subscribe",
                        "params": {"channels": channels},
                        "nonce": int(time.time() * 1000),
                    }))
                    logger.info("User WS subscribed")

                    async for raw in ws:
                        await self._handle_user(json.loads(raw), ws)

            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                logger.warning("User WS disconnected: %s — retry in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as exc:
                logger.exception("User WS unexpected error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle_user(self, msg: Dict, ws) -> None:
        method = msg.get("method", "")
        if method == "public/heartbeat":
            await ws.send(json.dumps({"id": msg.get("id"), "method": "public/respond-heartbeat"}))
            return
        if method != "subscribe":
            return

        result  = msg.get("result", {})
        channel = result.get("channel", "")
        data    = result.get("data", [])

        if channel.startswith("user.order") and self._on_order_update:
            for o in data:
                await self._on_order_update(o)
        elif channel.startswith("user.trade") and self._on_trade_fill:
            for t in data:
                await self._on_trade_fill(t)
        elif channel == "user.balance" and self._on_balance_update:
            await self._on_balance_update(data)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        await asyncio.gather(
            self._market_stream(),
            self._user_stream(),
        )

    def stop(self) -> None:
        self._running = False
