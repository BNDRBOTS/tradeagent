"""
Position sizing and all circuit breakers.
Sizing: risk-based (fixed fractional) capped at MAX_POSITION_PCT of balance.
Circuit breakers: daily drawdown, minimum balance, max daily trades.
FIX: Daily drawdown denominator uses current balance, not static ACCOUNT_CAPITAL.
"""
import logging
import math
from dataclasses import dataclass
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class SizeResult:
    quantity:             float
    notional_usd:         float
    stop_price:           float
    target_price:         float
    stop_distance_usd:    float
    target_distance_usd:  float
    effective_risk_usd:   float
    effective_risk_pct:   float
    capped:               bool


def _round_down(value: float, tick: float) -> float:
    if tick <= 0:
        return value
    return math.floor(value / tick) * tick


class PositionSizer:
    def __init__(self):
        self._balance:      float = settings.ACCOUNT_CAPITAL
        self._session_peak: float = settings.ACCOUNT_CAPITAL
        self._daily_pnl:    float = 0.0
        self._daily_trades: int   = 0

    # ── Balance management ────────────────────────────────────────────────────

    def update_balance(self, balance: float) -> None:
        self._balance = balance
        if balance > self._session_peak:
            self._session_peak = balance

    def record_trade_pnl(self, pnl: float) -> None:
        self._daily_pnl    += pnl
        self._daily_trades += 1
        self._balance      += pnl
        if self._balance > self._session_peak:
            self._session_peak = self._balance

    def reset_daily(self) -> None:
        logger.info("Daily risk reset | balance=%.4f daily_pnl=%.4f trades=%d",
                    self._balance, self._daily_pnl, self._daily_trades)
        self._daily_pnl    = 0.0
        self._daily_trades = 0

    # ── Spread ────────────────────────────────────────────────────────────────

    def get_spread_pct(self, bid: float, ask: float) -> float:
        if bid <= 0 or ask <= 0:
            return 1.0
        mid = (bid + ask) / 2.0
        return (ask - bid) / mid

    def check_spread(self, bid: float, ask: float) -> bool:
        sp = self.get_spread_pct(bid, ask)
        if sp >= settings.SPREAD_GUARD_THRESHOLD_PCT:
            logger.debug("Spread guard: %.5f%% >= %.4f%%",
                         sp * 100, settings.SPREAD_GUARD_THRESHOLD_PCT * 100)
            return False
        return True

    # ── Sizing ────────────────────────────────────────────────────────────────

    def calculate_size(self, entry_price: float, stop_distance_usd: float,
                       target_distance_usd: float, instrument: str,
                       override_risk_pct: Optional[float] = None) -> Optional[SizeResult]:
        """
        override_risk_pct: if provided, uses this instead of RISK_PCT_PER_TRADE.
        Used by bot_engine to scale position size by Ω tier multiplier.
        """
        if stop_distance_usd <= 0:
            logger.error("Invalid stop_distance=%.6f for %s", stop_distance_usd, instrument)
            return None
        if entry_price <= 0:
            logger.error("Invalid entry_price=%.6f for %s", entry_price, instrument)
            return None

        risk_pct  = override_risk_pct if override_risk_pct is not None else settings.RISK_PCT_PER_TRADE
        risk_usd  = self._balance * risk_pct
        qty_risk  = risk_usd / stop_distance_usd
        cap_usd   = self._balance * settings.MAX_POSITION_PCT
        qty_cap   = cap_usd / entry_price
        qty_raw   = min(qty_risk, qty_cap)
        capped    = (qty_raw == qty_cap) and (qty_cap < qty_risk)

        tick = (settings.BTC_QTY_TICK if instrument == settings.BTC_INSTRUMENT
                else settings.ETH_QTY_TICK)
        quantity = _round_down(qty_raw, tick)

        if quantity <= 0:
            logger.info("Size=0 for %s (balance=%.4f risk_usd=%.4f stop_d=%.4f)",
                        instrument, self._balance, risk_usd, stop_distance_usd)
            return None

        notional = quantity * entry_price
        if notional < settings.MIN_ORDER_NOTIONAL:
            logger.info("Notional %.4f < min %.2f for %s", notional, settings.MIN_ORDER_NOTIONAL, instrument)
            return None

        stop_price   = entry_price - stop_distance_usd
        target_price = entry_price + target_distance_usd

        return SizeResult(
            quantity=quantity,
            notional_usd=round(notional, 4),
            stop_price=round(stop_price, 2),
            target_price=round(target_price, 2),
            stop_distance_usd=round(stop_distance_usd, 4),
            target_distance_usd=round(target_distance_usd, 4),
            effective_risk_usd=round(quantity * stop_distance_usd, 4),
            effective_risk_pct=round(quantity * stop_distance_usd / self._balance, 6),
            capped=capped,
        )

    # ── Circuit breakers ──────────────────────────────────────────────────────

    def check_daily_drawdown(self) -> bool:
        if self._daily_pnl < 0:
            # FIX: use current balance as denominator, not ACCOUNT_CAPITAL
            denominator = max(self._balance, settings.MIN_ACCOUNT_BALANCE)
            dd = abs(self._daily_pnl) / denominator
            if dd >= settings.DAILY_DRAWDOWN_LIMIT:
                logger.critical(
                    "CIRCUIT BREAKER daily_drawdown: %.2f%% >= %.0f%% | balance=%.4f",
                    dd * 100, settings.DAILY_DRAWDOWN_LIMIT * 100, self._balance)
                return False
        return True

    def check_min_balance(self) -> bool:
        if self._balance < settings.MIN_ACCOUNT_BALANCE:
            logger.critical(
                "CIRCUIT BREAKER min_balance: %.4f < %.2f",
                self._balance, settings.MIN_ACCOUNT_BALANCE)
            return False
        return True

    def check_max_daily_trades(self) -> bool:
        if self._daily_trades >= settings.MAX_TRADES_PER_DAY:
            logger.info("Daily trade limit reached: %d/%d",
                        self._daily_trades, settings.MAX_TRADES_PER_DAY)
            return False
        return True

    def all_circuit_breakers_pass(self) -> bool:
        return (self.check_min_balance() and
                self.check_daily_drawdown() and
                self.check_max_daily_trades())

    def get_snapshot(self) -> dict:
        """Return current sizer state for the dashboard store."""
        from config import settings
        dd_pct = (abs(self._daily_pnl) / max(self._balance, settings.MIN_ACCOUNT_BALANCE)
                  if self._daily_pnl < 0 else 0.0)
        return dict(
            balance=self._balance,
            session_peak=self._session_peak,
            daily_pnl=self._daily_pnl,
            daily_trades=self._daily_trades,
            max_daily_trades=settings.MAX_TRADES_PER_DAY,
            min_balance=settings.MIN_ACCOUNT_BALANCE,
            drawdown_limit=settings.DAILY_DRAWDOWN_LIMIT,
        )
