"""
Instrument engine: WS data → strategy → risk → orders → state machine.
One instance per instrument. All execution is async.
FIX: uses sm.current_bar (public property) instead of sm._current_bar.
"""
import asyncio
import logging
import time
from typing import Dict, Optional

from broker.rest_client import CryptoComRestClient
from config import settings
from risk.position_sizer import PositionSizer
from state.machine import PositionStateMachine
from strategy.btc_momentum import BTCMomentumStrategy
from strategy.eth_mean_reversion import ETHMeanReversionStrategy

logger = logging.getLogger(__name__)


class InstrumentEngine:
    def __init__(self,
                 instrument: str,
                 strategy_class: str,
                 rest_client: CryptoComRestClient,
                 sizer: PositionSizer):
        self._instrument      = instrument
        self._rest            = rest_client
        self._sizer           = sizer
        self._sm              = PositionStateMachine(instrument)
        self._strategy_class  = strategy_class

        if strategy_class == "MOMENTUM":
            self._strategy = BTCMomentumStrategy()
        else:
            self._strategy = ETHMeanReversionStrategy()

        self._last_bid: float  = 0.0
        self._last_ask: float  = 0.0
        self._spread_pct: float = 0.0
        self._last_candle_ts: int = 0

    # ── D1 candle feed (called by main.py for BTC trend gate) ───────────────
    def push_d1_candle(self, candle: Dict) -> None:
        if hasattr(self._strategy, "push_d1_candle"):
            self._strategy.push_d1_candle(candle)

    # ── Book update ─────────────────────────────────────────────────────────
    async def on_book(self, instrument: str, book: Dict) -> None:
        if instrument != self._instrument: return
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            try:
                self._last_bid  = float(bids[0][0])
                self._last_ask  = float(asks[0][0])
                self._spread_pct = self._sizer.get_spread_pct(self._last_bid, self._last_ask)
            except (IndexError, ValueError) as exc:
                logger.warning("[%s] on_book parse error: %s", self._instrument, exc)

    # ── Candlestick (main evaluation loop) ──────────────────────────────────
    async def on_candlestick(self, instrument: str, candle: Dict) -> None:
        if instrument != self._instrument: return
        ts = int(candle.get("t", 0))
        if ts == self._last_candle_ts: return   # deduplicate
        self._last_candle_ts = ts

        self._sm.tick_bar()
        self._strategy.push_h1_candle(candle)
        audit = self._strategy.evaluate(spread_pct=self._spread_pct)
        logger.debug("[AUDIT][%s] %s", self._instrument, audit)

        # ── Position management: open ─────────────────────────────────────
        if self._sm.is_open():
            if self._sm.check_max_hold():
                await self._force_close_position("MAX_HOLD_EXCEEDED")
            return

        # ── Position management: entry pending (timeout check) ────────────
        if self._sm.is_entry_pending():
            pos = self._sm.position
            # FIX: use public property current_bar (not _current_bar)
            if pos and (self._sm.current_bar - pos.entry_bar) >= settings.ENTRY_FILL_TIMEOUT_BARS:
                logger.warning("[%s] Entry order timeout — cancelling %s",
                               self._instrument, pos.entry_order_id)
                try:
                    self._rest.cancel_order(self._instrument, pos.entry_order_id)
                except Exception as exc:
                    logger.error("[%s] cancel_order failed: %s", self._instrument, exc)
                self._sm.on_entry_timeout()
            return

        # ── Signal evaluation ─────────────────────────────────────────────
        if audit.signal not in ("LONG", "SHORT"): return

        if not self._sizer.all_circuit_breakers_pass():
            logger.warning("[%s] Circuit breaker blocked entry", self._instrument)
            return

        stop_d, target_d = self._strategy.get_stop_and_target()
        current_price = float(candle.get("c", 0))
        if current_price <= 0: return

        size = self._sizer.calculate_size(
            entry_price=current_price,
            stop_distance_usd=stop_d,
            target_distance_usd=target_d,
            instrument=self._instrument,
            direction=audit.signal,
        )
        if size is None: return

        # Aggressive taker entry: cross the spread
        if audit.signal == "LONG":
            limit_price = round(self._last_ask * (1 + settings.ENTRY_LIMIT_OFFSET_PCT), 2) if self._last_ask > 0 else round(current_price * 1.001, 2)
        else:
            limit_price = round(self._last_bid * (1 - settings.ENTRY_LIMIT_OFFSET_PCT), 2) if self._last_bid > 0 else round(current_price * 0.999, 2)

        client_oid = f"{self._instrument}_{int(time.time() * 1000)}"
        try:
            resp = self._rest.create_limit_order(
                instrument=self._instrument,
                side="BUY" if audit.signal == "LONG" else "SELL",
                price=limit_price,
                quantity=size.quantity,
                client_oid=client_oid,
                post_only=False,   # taker entry — post_only would reject
            )
            order_id = resp.get("order_id", client_oid)
            self._sm.on_entry_submitted(
                order_id=order_id, direction=audit.signal,
                entry_price=limit_price, quantity=size.quantity,
                stop_price=size.stop_price, target_price=size.target_price,
            )
            logger.info("[ENTRY] %s %s qty=%.6f @ %.4f stop=%.4f target=%.4f",
                        self._instrument, audit.signal, size.quantity,
                        limit_price, size.stop_price, size.target_price)
        except Exception as exc:
            logger.error("[%s] Entry order submission failed: %s", self._instrument, exc)

    # ── Order update handler ─────────────────────────────────────────────────
    async def on_order_update(self, order: Dict) -> None:
        if order.get("instrument_name") != self._instrument: return

        status    = order.get("status", "")
        order_id  = order.get("order_id", "")
        fill_price = float(order.get("avg_price", 0) or 0)
        cum_qty    = float(order.get("cumulative_quantity", 0) or 0)

        if status == "FILLED":
            pos = self._sm.position

            # Entry fill → place OCO
            if pos and order_id == pos.entry_order_id and self._sm.is_entry_pending():
                await self._on_entry_filled(pos, fill_price, cum_qty)

            # Exit fill → record PnL
            elif pos and self._sm.is_open():
                await self._on_exit_filled(pos, order_id, fill_price)

        elif status in ("CANCELLED", "EXPIRED", "REJECTED"):
            pos = self._sm.position
            if pos and order_id == pos.entry_order_id and self._sm.is_entry_pending():
                logger.warning("[%s] Entry order %s: %s", self._instrument, order_id, status)
                self._sm.on_entry_cancelled(f"order {status}")

    async def _on_entry_filled(self, pos, fill_price: float, cum_qty: float) -> None:
        try:
            # Correct stop-limit price: SHORT stop is above entry, LONG stop below
            if pos.direction == "SHORT":
                stop_lim = pos.stop_price * (1 + settings.STOP_LIMIT_OFFSET_PCT)
            else:
                stop_lim = pos.stop_price * (1 - settings.STOP_LIMIT_OFFSET_PCT)
            stop_lim = round(stop_lim, 2)

            oco = self._rest.create_oco_order(
                instrument=self._instrument,
                quantity=cum_qty,
                stop_trigger=pos.stop_price,
                stop_limit=stop_lim,
                take_profit=pos.target_price,
                client_oid_prefix=f"oco_{int(time.time() * 1000)}",
                direction=pos.direction,
            )
            oco_list_id = oco.get("order_list_id", "")
            order_ids   = oco.get("order_ids", ["", ""])

            self._sm.on_entry_filled(fill_price, cum_qty, oco_list_id)
            self._sm.on_exit_submitted(
                stop_id=order_ids[0] if order_ids else "",
                tp_id=order_ids[1] if len(order_ids) > 1 else "",
                price=fill_price,
            )
            logger.info("[ENTRY FILL] %s @ %.4f qty=%.6f — OCO placed %s",
                        self._instrument, fill_price, cum_qty, oco_list_id)
        except Exception as exc:
            logger.critical("[%s] OCO placement failed: %s — emergency cancel", self._instrument, exc)
            await self._emergency_cancel()

    async def _on_exit_filled(self, pos, order_id: str, fill_price: float) -> None:
        # Classify by stored order_id first; fall back to price direction
        if order_id == pos.stop_order_id:
            exit_reason = "STOP"
        elif order_id == pos.target_order_id:
            exit_reason = "TARGET"
        else:
            # OCO fills sometimes arrive with the list order_id, not individual leg id
            if pos.direction == "LONG":
                exit_reason = "STOP" if fill_price <= pos.entry_price else "TARGET"
            else:
                exit_reason = "STOP" if fill_price >= pos.entry_price else "TARGET"
            logger.warning("[%s] Exit order_id %s unmatched — price fallback: %s",
                           self._instrument, order_id, exit_reason)
        closed = self._sm.on_exit_filled(fill_price, exit_reason)
        if closed:
            self._sizer.record_trade_pnl(closed.realized_pnl)

    # ── Force-close ──────────────────────────────────────────────────────────
    async def _force_close_position(self, reason: str) -> None:
        pos = self._sm.position
        if not pos: return
        logger.warning("[%s] Force-closing: %s", self._instrument, reason)
        try:
            self._rest.cancel_all_orders(self._instrument)
        except Exception as exc:
            logger.error("[%s] cancel_all_orders failed: %s", self._instrument, exc)

        side       = "SELL" if pos.direction == "LONG" else "BUY"
        exit_price = self._last_bid if side == "SELL" else self._last_ask
        if exit_price <= 0:
            exit_price = pos.entry_price  # last resort fallback

        try:
            self._rest.create_limit_order(
                instrument=self._instrument, side=side,
                price=round(exit_price, 2), quantity=pos.quantity, post_only=False,
            )
        except Exception as exc:
            logger.critical("[%s] Force close limit order failed: %s", self._instrument, exc)

        # Transition state machine regardless of order success to prevent permanent lock
        closed = self._sm.on_exit_filled(exit_price, f"FORCE_CLOSE:{reason}")
        if closed:
            self._sizer.record_trade_pnl(closed.realized_pnl)

    async def _emergency_cancel(self) -> None:
        try:
            self._rest.cancel_all_orders(self._instrument)
        except Exception as exc:
            logger.error("[%s] Emergency cancel failed: %s", self._instrument, exc)
        self._sm.on_entry_cancelled("emergency_cancel")
