"""
InstrumentEngine — wires WS data → strategy → regime gate → signal scoring
→ intelligence amplifier → risk → order submission → state machine → dashboard.

FIX: _pending_conditions, _pending_omega_score, _pending_omega_tier now stored
at signal-fire time and passed correctly to intelligence.record_trade at close.
Previously signal_conditions={} and omega_score=0.0 were passed — intelligence
engine was completely blind, load-bearing analysis and recursive amplifier had
no data to learn from.
"""
import asyncio
import logging
import math
import time
from typing import Dict, Optional

from broker.rest_client import CryptoComRestClient
from config import settings
from dashboard.state_store import store, PositionSnap
from risk.position_sizer import PositionSizer
from state.machine import PositionStateMachine
from strategy.btc_momentum import BTCMomentumStrategy
from strategy.eth_mean_reversion import ETHMeanReversionStrategy
from strategy.regime_detector import RegimeDetector
from strategy.signal_scorer import score_signal, OmegaTier
from strategy.intelligence import TradeIntelligence

logger = logging.getLogger(__name__)


def _push_risk(sizer: PositionSizer) -> None:
    s = sizer.get_snapshot()
    store.update_risk(**s)


def _pos_snap(pos) -> Optional[PositionSnap]:
    if pos is None:
        return None
    return PositionSnap(
        direction=pos.direction,
        fill_price=pos.fill_price or pos.entry_price,
        quantity=pos.fill_qty or pos.quantity,
        stop_price=pos.stop_price,
        target_price=pos.target_price,
        entry_ts=pos.entry_ts,
        unrealized_pnl=0.0,
        bars_held=0,
    )


def _extract_conditions(audit) -> Dict[str, bool]:
    """Extract boolean signal conditions from an audit entry."""
    conditions: Dict[str, bool] = {}
    for field in ("trend_gate_pass", "adx_pass", "macd_cross", "volume_pass",
                  "atr_spike_pass", "spread_pass", "cooldown_pass",
                  "rsi_oversold", "rsi_overbought", "in_session_range",
                  "price_below_lower", "price_above_upper"):
        if hasattr(audit, field):
            conditions[field] = bool(getattr(audit, field))
    return conditions


class InstrumentEngine:
    def __init__(self, instrument: str, strategy_class: str,
                 rest_client: CryptoComRestClient, sizer: PositionSizer):
        self._instrument     = instrument
        self._rest           = rest_client
        self._sizer          = sizer
        self._sm             = PositionStateMachine(instrument)

        if strategy_class == "MOMENTUM":
            self._strategy = BTCMomentumStrategy()
        else:
            self._strategy = ETHMeanReversionStrategy()

        self._strategy_class = strategy_class
        self._regime         = RegimeDetector(instrument)
        self._intel          = TradeIntelligence(instrument)

        self._last_bid:       float = 0.0
        self._last_ask:       float = 0.0
        self._spread_pct:     float = 0.0
        self._last_candle_ts: int   = 0
        self._last_atr_val:   float = float("nan")
        self._last_atr_base:  float = float("nan")

        # Pending signal state — captured when signal fires, consumed at close
        # FIX: these were never populated, causing intelligence to receive {} and 0.0
        self._pending_conditions:  Dict[str, bool] = {}
        self._pending_omega_score: float = 0.0
        self._pending_omega_tier:  str   = "UNKNOWN"
        self._pending_regime:      str   = "MIXED"

        store.register_engine(instrument)
        store.update_engine_state(instrument, "FLAT", None)
        logger.info("InstrumentEngine ready: %s strategy=%s", instrument, strategy_class)

    def push_d1_candle(self, candle: Dict) -> None:
        if hasattr(self._strategy, "push_d1_candle"):
            self._strategy.push_d1_candle(candle)

    async def on_candlestick(self, instrument: str, candle: Dict) -> None:
        if instrument != self._instrument:
            return
        ts = int(candle.get("t", 0))
        if ts == self._last_candle_ts:
            return
        self._last_candle_ts = ts

        self._sm.tick_bar()
        self._strategy.push_h1_candle(candle)
        audit = self._strategy.evaluate(spread_pct=self._spread_pct)

        if hasattr(audit, "atr_value") and not math.isnan(audit.atr_value):
            self._last_atr_val = audit.atr_value
        if hasattr(audit, "atr_baseline") and not math.isnan(audit.atr_baseline):
            self._last_atr_base = audit.atr_baseline

        if self._strategy_class == "MOMENTUM":
            regime_state = self._regime.update_from_btc_audit(audit)
        else:
            regime_state = self._regime.update_from_eth_audit(audit)

        store.update_engine_book(
            instrument=instrument, bid=self._last_bid, ask=self._last_ask,
            spread_pct=self._spread_pct, candle_ts=ts,
        )

        logger.debug("[AUDIT][%s] signal=%s regime=%s trust=%.3f",
                     self._instrument, audit.signal, regime_state.regime, regime_state.trust)

        if self._sm.is_open():
            if self._sm.check_max_hold():
                await self._force_close_position("MAX_HOLD_EXCEEDED")
            return

        if self._sm.is_entry_pending():
            pos = self._sm.position
            if pos:
                age = self._sm._current_bar - pos.entry_bar
                if age >= settings.ENTRY_FILL_TIMEOUT_BARS:
                    logger.warning("[%s] Entry order timed out — cancelling", self._instrument)
                    try:
                        self._rest.cancel_order(self._instrument, pos.entry_order_id)
                    except Exception as exc:
                        logger.error("Cancel entry failed: %s", exc)
                    self._sm.on_entry_timeout()
                    store.update_engine_state(instrument, "FLAT", None)
                    _push_risk(self._sizer)
            return

        if audit.signal not in ("LONG", "SHORT"):
            return

        # ── Regime gate ───────────────────────────────────────────────────────
        if self._strategy_class == "MOMENTUM" and not self._regime.btc_should_trade():
            logger.info("[%s] Regime gate: trust=%.3f — MOMENTUM blocked in %s",
                        self._instrument, regime_state.trust, regime_state.regime)
            return
        if self._strategy_class == "MEAN_REVERSION" and not self._regime.eth_should_trade():
            logger.info("[%s] Regime gate: trust=%.3f — MR blocked in %s",
                        self._instrument, regime_state.trust, regime_state.regime)
            return

        if not self._sizer.all_circuit_breakers_pass():
            logger.info("[%s] Circuit breaker blocked %s", self._instrument, audit.signal)
            _push_risk(self._sizer)
            return

        # ── Extract conditions (BEFORE scoring — these are what intelligence learns from) ─
        conditions     = _extract_conditions(audit)
        stop_d, target_d = self._strategy.get_stop_and_target()
        sizer_snap       = self._sizer.get_snapshot()

        regime_mult = self._regime.get_regime_multiplier(self._strategy_class)
        intel_amp   = self._intel.omega_amplifier(conditions) * regime_mult

        score = score_signal(
            trend_gate_pass    = getattr(audit, "trend_gate_pass",   True),
            adx_pass           = getattr(audit, "adx_pass",          True),
            adx_value          = getattr(audit, "adx_value",         25.0),
            macd_cross         = getattr(audit, "macd_cross",        False),
            volume_pass        = getattr(audit, "volume_pass",       True),
            atr_spike_pass     = getattr(audit, "atr_spike_pass",    True),
            spread_pass        = getattr(audit, "spread_pass",       True),
            cooldown_pass      = getattr(audit, "cooldown_pass",     True),
            spread_pct         = self._spread_pct,
            stop_distance      = stop_d,
            target_distance    = target_d,
            balance            = sizer_snap["balance"],
            atr_val            = self._last_atr_val,
            atr_baseline       = self._last_atr_base,
            consecutive_losses = self._intel.get_consecutive_losses(),
            daily_pnl          = sizer_snap["daily_pnl"],
            intelligence_amplifier = intel_amp,
        )

        logger.info(
            "[SCORE][%s] Ω=%.0f tier=%s mult=%.1f regime=%s trust=%.2f amp=%.3f",
            self._instrument, score.omega_final, score.tier.label,
            score.size_multiplier, regime_state.regime, regime_state.trust, intel_amp,
        )

        if score.tier.value < settings.MIN_OMEGA_TIER:
            logger.info("[%s] Ω tier %s below minimum %d — skip",
                        self._instrument, score.tier.label, settings.MIN_OMEGA_TIER)
            return

        current_price = float(candle.get("c", 0))
        if current_price <= 0:
            return

        adjusted_risk_pct = settings.RISK_PCT_PER_TRADE * score.size_multiplier
        size = self._sizer.calculate_size(
            current_price, stop_d, target_d, self._instrument,
            override_risk_pct=adjusted_risk_pct,
        )
        if size is None:
            return

        if audit.signal == "LONG":
            entry_price  = round(self._last_ask * (1 + settings.ENTRY_LIMIT_OFFSET_PCT), 2)
            order_side   = "BUY"
            stop_price   = round(entry_price - stop_d, 2)
            target_price = round(entry_price + target_d, 2)
        else:
            entry_price  = round(self._last_bid * (1 - settings.ENTRY_LIMIT_OFFSET_PCT), 2)
            order_side   = "SELL"
            stop_price   = round(entry_price + stop_d, 2)
            target_price = round(entry_price - target_d, 2)

        if entry_price <= 0:
            entry_price  = current_price
            stop_price   = round(entry_price - stop_d, 2) if audit.signal == "LONG" else round(entry_price + stop_d, 2)
            target_price = round(entry_price + target_d, 2) if audit.signal == "LONG" else round(entry_price - target_d, 2)

        client_oid = f"{self._instrument}_{int(time.time() * 1000)}"
        try:
            resp     = self._rest.create_limit_order(
                instrument=self._instrument, side=order_side, price=entry_price,
                quantity=size.quantity, client_oid=client_oid, post_only=False,
            )
            order_id = resp.get("order_id", client_oid)
            self._sm.on_entry_submitted(
                order_id=order_id, direction=audit.signal,
                entry_price=entry_price, quantity=size.quantity,
                stop_price=stop_price, target_price=target_price,
            )

            # FIX: store signal context NOW while we have it — consumed at trade close
            self._pending_conditions  = conditions
            self._pending_omega_score = score.omega_final
            self._pending_omega_tier  = score.tier.label
            self._pending_regime      = regime_state.regime

            store.update_engine_state(instrument, "ENTRY_PENDING", _pos_snap(self._sm.position))
            _push_risk(self._sizer)
            logger.info(
                "[ENTRY] %s %s qty=%.6f price=%.4f stop=%.4f target=%.4f Ω_tier=%s",
                self._instrument, audit.signal, size.quantity,
                entry_price, stop_price, target_price, score.tier.label,
            )
        except Exception as exc:
            logger.error("[%s] Entry submission failed: %s", self._instrument, exc)

    async def on_book(self, instrument: str, book: Dict) -> None:
        if instrument != self._instrument:
            return
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            self._last_bid   = float(bids[0][0])
            self._last_ask   = float(asks[0][0])
            self._spread_pct = self._sizer.get_spread_pct(self._last_bid, self._last_ask)
            store.update_engine_book(
                instrument=instrument, bid=self._last_bid,
                ask=self._last_ask, spread_pct=self._spread_pct,
            )

    async def on_order_update(self, order: Dict) -> None:
        if order.get("instrument_name") != self._instrument:
            return
        status     = order.get("status", "")
        order_id   = str(order.get("order_id", ""))
        fill_price = float(order.get("avg_price", 0) or 0)
        cum_qty    = float(order.get("cumulative_quantity", 0) or 0)

        if status == "FILLED":
            pos = self._sm.position

            if pos and order_id == pos.entry_order_id and self._sm.is_entry_pending():
                try:
                    prefix = f"oco_{int(time.time() * 1000)}"
                    oco    = self._rest.create_oco_order(
                        instrument=self._instrument, quantity=cum_qty,
                        stop_trigger=pos.stop_price,
                        stop_limit=round(pos.stop_price * (1 - settings.STOP_LIMIT_OFFSET_PCT), 2),
                        take_profit=pos.target_price, direction=pos.direction,
                        client_oid_prefix=prefix,
                    )
                    list_id = oco.get("order_list_id", "")
                    oids    = oco.get("order_ids", ["", ""])
                    self._sm.on_entry_filled(fill_price, cum_qty, list_id)
                    self._sm.on_exit_submitted(
                        stop_id=oids[0] if oids else "",
                        tp_id=oids[1] if len(oids) > 1 else "",
                        price=fill_price,
                    )
                    store.update_engine_state(self._instrument, "EXIT_PENDING",
                                              _pos_snap(self._sm.position))
                    _push_risk(self._sizer)
                    logger.info("[ENTRY FILL] %s @ %.4f qty=%.6f direction=%s | OCO placed",
                                self._instrument, fill_price, cum_qty, pos.direction)
                except Exception as exc:
                    logger.critical("[%s] OCO failed after entry fill: %s", self._instrument, exc)
                    await self._emergency_cancel()
                return

            if pos and self._sm.is_open():
                if order_id and pos.stop_order_id and order_id == pos.stop_order_id:
                    exit_reason = "STOP"
                elif order_id and pos.target_order_id and order_id == pos.target_order_id:
                    exit_reason = "TARGET"
                else:
                    exit_reason = "STOP" if fill_price <= pos.stop_price * 1.005 else "TARGET"
                    logger.warning("[%s] Exit order_id not matched — heuristic: %s",
                                   self._instrument, exit_reason)

                outcome = "WIN" if exit_reason == "TARGET" else "STOP"
                closed  = self._sm.on_exit_filled(fill_price, exit_reason)
                if closed:
                    self._sizer.record_trade_pnl(closed.realized_pnl)
                    store.update_engine_state(self._instrument, "FLAT", None)
                    _push_risk(self._sizer)
                    self._record_to_intelligence(closed, exit_reason, outcome)
                    await self._persist_trade(closed, exit_reason)
                    logger.info("[EXIT] %s %s @ %.4f | pnl=%.4f reason=%s",
                                self._instrument, closed.direction,
                                fill_price, closed.realized_pnl, exit_reason)

        elif status in ("CANCELLED", "EXPIRED", "REJECTED"):
            pos = self._sm.position
            if pos and order_id == pos.entry_order_id and self._sm.is_entry_pending():
                logger.warning("[%s] Entry %s status=%s", self._instrument, order_id, status)
                self._sm.on_entry_cancelled(f"order status={status}")
                store.update_engine_state(self._instrument, "FLAT", None)
                _push_risk(self._sizer)
                self._clear_pending()

    async def _force_close_position(self, reason: str) -> None:
        pos = self._sm.position
        if not pos:
            return
        logger.warning("[%s] Force-closing: %s", self._instrument, reason)
        try:
            self._rest.cancel_all_orders(self._instrument)
        except Exception as exc:
            logger.error("Cancel all failed: %s", exc)

        close_side  = "SELL" if pos.direction == "LONG" else "BUY"
        close_price = self._last_bid if close_side == "SELL" else self._last_ask
        if close_price <= 0:
            close_price = pos.fill_price

        try:
            self._rest.create_limit_order(
                instrument=self._instrument, side=close_side,
                price=close_price, quantity=pos.fill_qty or pos.quantity, post_only=False,
            )
            closed = self._sm.on_exit_filled(close_price, reason)
            if closed:
                self._sizer.record_trade_pnl(closed.realized_pnl)
                store.update_engine_state(self._instrument, "FLAT", None)
                _push_risk(self._sizer)
                self._record_to_intelligence(closed, reason, "TIMEOUT")
                await self._persist_trade(closed, reason)
                logger.info("[FORCE_CLOSE] %s pnl=%.4f", self._instrument, closed.realized_pnl)
        except Exception as exc:
            logger.critical("[%s] Force close FAILED: %s", self._instrument, exc)

    async def _emergency_cancel(self) -> None:
        try:
            self._rest.cancel_all_orders(self._instrument)
        except Exception as exc:
            logger.error("Emergency cancel failed: %s", exc)
        self._sm.on_entry_cancelled("emergency_cancel_after_oco_failure")
        store.update_engine_state(self._instrument, "FLAT", None)
        _push_risk(self._sizer)
        self._clear_pending()

    def _record_to_intelligence(self, closed, exit_reason: str, outcome: str) -> None:
        """
        Record closed trade to intelligence engine.
        FIX: now uses _pending_conditions and _pending_omega_score captured at signal time.
        Previously passed signal_conditions={} and omega_score=0.0 — intelligence was blind.
        """
        try:
            bars_held = max(0, self._sm._current_bar - (closed.entry_bar if hasattr(closed, "entry_bar") else 0))
            self._intel.record_trade(
                direction=closed.direction,
                signal_conditions=self._pending_conditions,   # FIX: real data now
                spread_pct=self._spread_pct,
                regime_state=self._pending_regime,
                omega_score=self._pending_omega_score,        # FIX: real data now
                omega_tier=self._pending_omega_tier,
                outcome=outcome,
                realized_pnl=closed.realized_pnl,
                entry_price=closed.fill_price,
                exit_price=closed.exit_price,
                bars_held=bars_held,
            )
        except Exception as exc:
            logger.warning("Intelligence record failed (non-fatal): %s", exc)
        finally:
            self._clear_pending()

    def _clear_pending(self) -> None:
        self._pending_conditions  = {}
        self._pending_omega_score = 0.0
        self._pending_omega_tier  = "UNKNOWN"
        self._pending_regime      = "MIXED"

    async def _persist_trade(self, closed, exit_reason: str) -> None:
        store.record_closed_trade(
            instrument=self._instrument, direction=closed.direction,
            fill_price=closed.fill_price, exit_price=closed.exit_price,
            quantity=closed.fill_qty, stop_price=closed.stop_price,
            target_price=closed.target_price, exit_reason=exit_reason,
            realized_pnl=closed.realized_pnl,
            entry_ts=closed.entry_ts, exit_ts=closed.exit_ts,
        )
        try:
            from dashboard.persistence import insert_trade
            await insert_trade(
                instrument=self._instrument, direction=closed.direction,
                fill_price=closed.fill_price, exit_price=closed.exit_price,
                quantity=closed.fill_qty, stop_price=closed.stop_price,
                target_price=closed.target_price, exit_reason=exit_reason,
                realized_pnl=closed.realized_pnl,
                entry_ts=closed.entry_ts, exit_ts=closed.exit_ts,
            )
        except Exception as exc:
            logger.warning("SQLite insert failed (in-memory backup intact): %s", exc)
