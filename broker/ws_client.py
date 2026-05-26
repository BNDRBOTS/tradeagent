"""
Crypto.com Exchange v1 WebSocket client.
Two connections: market (public) and user (private).
Heartbeat, reconnect on drop, cancel-on-disconnect set on every session reconnect.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Callable, Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from config import settings

logger = logging.getLogger(__name__)

# Callback type aliases
CandleCB = Callable[[str, Dict], Any]   # (instrument, candle)
BookCB   = Callable[[str, Dict], Any]   # (instrument, book)
OrderCB  = Callable[[Dict], Any]        # (order_update)


class _WSBase:
    """Shared reconnect loop and heartbeat for a single WebSocket URL."""

    def __init__(self, url: str, name: str):
        self._url   = url
        self._name  = name
        self._ws    = None
        self._running = False
        self._subscribed: list = []

    async def _connect(self):
        logger.info("[%s] Connecting to %s", self._name, self._url)
        self._ws = await websockets.connect(
            self._url, ping_interval=20, ping_timeout=30, close_timeout=10,
        )
        logger.info("[%s] Connected", self._name)
        await self._on_connect()

    async def _on_connect(self):
        """Override to authenticate and resubscribe after each connect."""
        pass

    async def _send(self, msg: Dict) -> None:
        if self._ws is None: return
        try:
            await self._ws.send(json.dumps(msg))
        except Exception as exc:
            logger.warning("[%s] send failed: %s", self._name, exc)

    async def _recv_loop(self):
        while self._running:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=35.0)
                msg = json.loads(raw)
                await self._handle(msg)
            except asyncio.TimeoutError:
                logger.debug("[%s] recv timeout — sending heartbeat", self._name)
                await self._send({"id": int(time.time() * 1000), "method": "public/heartbeat"})
            except ConnectionClosed as exc:
                logger.warning("[%s] Connection closed: %s", self._name, exc)
                raise
            except json.JSONDecodeError as exc:
                logger.warning("[%s] Bad JSON: %s", self._name, exc)

    async def _handle(self, msg: Dict) -> None:
        """Override to process messages."""
        pass

    async def run_forever(self):
        self._running = True
        backoff = 1.0
        while self._running:
            try:
                await self._connect()
                backoff = 1.0
                await self._recv_loop()
            except ConnectionClosed:
                pass
            except Exception as exc:
                logger.error("[%s] Unexpected error: %s", self._name, exc)
            if self._running:
                logger.info("[%s] Reconnecting in %.0fs", self._name, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()


class MarketWSClient(_WSBase):
    """Public market WebSocket: candlestick + order book."""

    def __init__(self,
                 candle_cb: Optional[CandleCB] = None,
                 book_cb: Optional[BookCB] = None):
        super().__init__(settings.WS_MARKET_URL, "MarketWS")
        self._candle_cb = candle_cb
        self._book_cb   = book_cb
        self._subs: list = []

    def subscribe_candlestick(self, instrument: str, timeframe: str) -> None:
        ch = f"candlestick.{timeframe}.{instrument}"
        if ch not in self._subs: self._subs.append(ch)

    def subscribe_book(self, instrument: str, depth: int = 10) -> None:
        ch = f"book.{instrument}.{depth}"
        if ch not in self._subs: self._subs.append(ch)

    async def _on_connect(self) -> None:
        if not self._subs: return
        await self._send({
            "id": int(time.time() * 1000),
            "method": "subscribe",
            "params": {"channels": self._subs},
        })
        logger.info("[MarketWS] Subscribed: %s", self._subs)

    async def _handle(self, msg: Dict) -> None:
        method = msg.get("method", "")
        if method == "public/heartbeat":
            await self._send({
                "id": msg.get("id", 0),
                "method": "public/respond-heartbeat",
            })
            return
        if method != "subscribe": return
        result = msg.get("result", {})
        if not result: return
        channel: str = result.get("channel", "")
        data_list = result.get("data", [])

        if channel.startswith("candlestick.") and self._candle_cb:
            parts = channel.split(".")
            # channel = "candlestick.<tf>.<instrument>"
            instrument = ".".join(parts[2:]) if len(parts) > 2 else "UNKNOWN"
            for candle in data_list:
                try:
                    base_vol = float(candle.get("v", 0))
                    close    = float(candle.get("c", 0))
                    candle["volume_usd"] = base_vol * close
                    await self._candle_cb(instrument, candle)
                except Exception as exc:
                    logger.error("[MarketWS] candle_cb error: %s", exc)

        elif channel.startswith("book.") and self._book_cb:
            parts = channel.split(".")
            instrument = parts[1] if len(parts) > 1 else "UNKNOWN"
            for book in data_list:
                try:
                    await self._book_cb(instrument, book)
                except Exception as exc:
                    logger.error("[MarketWS] book_cb error: %s", exc)


class UserWSClient(_WSBase):
    """Private user WebSocket: order fills and balance updates."""

    def __init__(self, order_cb: Optional[OrderCB] = None):
        super().__init__(settings.WS_USER_URL, "UserWS")
        self._order_cb = order_cb

    def _build_auth_sig(self) -> Dict:
        nonce  = int(time.time() * 1000)
        req_id = nonce
        method = "public/auth"
        raw    = f"{method}{req_id}{settings.API_KEY}{nonce}"
        sig    = hmac.new(
            settings.API_SECRET.encode(), raw.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "id":      req_id,
            "method":  method,
            "api_key": settings.API_KEY,
            "sig":     sig,
            "nonce":   nonce,
        }

    async def _on_connect(self) -> None:
        if not settings.API_KEY or not settings.API_SECRET:
            logger.error("[UserWS] No API credentials — user stream disabled")
            return
        # Authenticate
        await self._send(self._build_auth_sig())
        # cancel-on-disconnect re-set on every reconnect (not just first connect)
        await self._send({
            "id":     int(time.time() * 1000),
            "method": "private/set-cancel-on-disconnect",
            "params": {"scope": "CONNECTION"},
        })

    async def _subscribe_user_channels(self) -> None:
        await self._send({
            "id":     int(time.time() * 1000),
            "method": "subscribe",
            "params": {"channels": ["user.order", "user.balance"]},
        })
        logger.info("[UserWS] Subscribed to user.order + user.balance")

    async def _handle(self, msg: Dict) -> None:
        method = msg.get("method", "")
        if method == "public/heartbeat":
            await self._send({
                "id":     msg.get("id", 0),
                "method": "public/respond-heartbeat",
            })
            return
        # Auth response — subscribe user channels once authenticated
        if method == "public/auth" and msg.get("code", -1) == 0:
            logger.info("[UserWS] Auth OK — subscribing user channels")
            await self._subscribe_user_channels()
            return
        if method == "public/auth" and msg.get("code", -1) != 0:
            logger.critical("[UserWS] Auth FAILED code=%s — check API credentials",
                            msg.get("code"))
            return
        if method != "subscribe": return
        result = msg.get("result", {})
        if not result: return
        channel = result.get("channel", "")
        data_list = result.get("data", [])

        if channel == "user.order" and self._order_cb:
            for order in data_list:
                try:
                    await self._order_cb(order)
                except Exception as exc:
                    logger.error("[UserWS] order_cb error: %s", exc)


class CryptoComWSClient:
    """Facade: wraps MarketWSClient + UserWSClient and exposes start/stop."""

    def __init__(self,
                 candle_cb: Optional[CandleCB] = None,
                 book_cb: Optional[BookCB] = None,
                 order_cb: Optional[OrderCB] = None):
        self._market = MarketWSClient(candle_cb=candle_cb, book_cb=book_cb)
        self._user   = UserWSClient(order_cb=order_cb)
        self._tasks  = []

    def subscribe_candlestick(self, instrument: str, timeframe: str) -> None:
        self._market.subscribe_candlestick(instrument, timeframe)

    def subscribe_book(self, instrument: str, depth: int = 10) -> None:
        self._market.subscribe_book(instrument, depth)

    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        self._tasks = [
            loop.create_task(self._market.run_forever()),
            loop.create_task(self._user.run_forever()),
        ]
        logger.info("[WSClient] Both WebSocket loops started")

    async def stop(self) -> None:
        await self._market.stop()
        await self._user.stop()
        for t in self._tasks:
            t.cancel()
            try: await t
            except asyncio.CancelledError: pass
        logger.info("[WSClient] WebSocket loops stopped")
