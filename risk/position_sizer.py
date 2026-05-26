"""
Position sizer, spread guard, and all circuit breakers.
FIX: daily drawdown check uses > (not >=) — exactly at limit does NOT trigger halt.
FIX: calculate_size direction parameter adjusts stop/target price direction for SHORT.
"""
import logging, math
from dataclasses import dataclass
from typing import Optional
from config import settings

logger = logging.getLogger(__name__)


@dataclass
class SizeResult:
    quantity: float
    notional_usd: float
    stop_price: float
    target_price: float
    stop_distance_usd: float
    target_distance_usd: float
    effective_risk_usd: float
    effective_risk_pct: float
    capped: bool


def _round_down(value: float, tick: float) -> float:
    if tick <= 0: return value
    return math.floor(value / tick) * tick


class PositionSizer:
    def __init__(self):
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._balance: float = settings.ACCOUNT_CAPITAL

    def update_balance(self, b: float) -> None:
        self._balance = b

    def record_trade_pnl(self, pnl: float) -> None:
        self._daily_pnl += pnl
        self._daily_trades += 1

    def reset_daily(self) -> None:
        self._daily_pnl = 0.0
        self._daily_trades = 0
        logger.info("Daily risk counters reset. Balance=%.4f", self._balance)

    def get_spread_pct(self, bid: float, ask: float) -> float:
        if bid <= 0 or ask <= 0: return 1.0
        mid = (bid + ask) / 2.0
        if mid == 0: return 1.0
        return (ask - bid) / mid

    def check_spread(self, bid: float, ask: float) -> bool:
        if bid <= 0 or ask <= 0:
            logger.debug("Spread guard: invalid bid/ask %.4f/%.4f", bid, ask)
            return False
        sp = self.get_spread_pct(bid, ask)
        if sp >= settings.SPREAD_GUARD_THRESHOLD_PCT:
            logger.warning("Spread guard: %.5f%% >= threshold %.4f%%",
                           sp * 100, settings.SPREAD_GUARD_THRESHOLD_PCT * 100)
            return False
        return True

    def calculate_size(self, entry_price: float, stop_distance_usd: float,
                       target_distance_usd: float, instrument: str,
                       direction: str = "LONG") -> Optional[SizeResult]:
        if entry_price <= 0:
            logger.error("calculate_size: entry_price must be > 0, got %.6f", entry_price)
            return None
        if stop_distance_usd <= 0:
            logger.error("calculate_size: stop_distance_usd must be > 0, got %.6f", stop_distance_usd)
            return None

        risk_usd = self._balance * settings.RISK_PCT_PER_TRADE
        qty_risk = risk_usd / stop_distance_usd
        qty_cap  = (self._balance * settings.MAX_POSITION_PCT) / entry_price
        qty_raw  = min(qty_risk, qty_cap)
        capped   = qty_raw == qty_cap and qty_cap < qty_risk

        tick = settings.BTC_QTY_TICK if instrument == settings.BTC_INSTRUMENT else settings.ETH_QTY_TICK
        qty  = _round_down(qty_raw, tick)

        if qty <= 0:
            logger.info("calculate_size: qty=0 after tick rounding — entry blocked")
            return None

        notional = qty * entry_price
        if notional < settings.MIN_ORDER_NOTIONAL:
            logger.info("calculate_size: notional=%.4f < min=%.2f — entry blocked", notional, settings.MIN_ORDER_NOTIONAL)
            return None

        # Direction-aware stop and target prices
        if direction == "LONG":
            stop_price   = entry_price - stop_distance_usd
            target_price = entry_price + target_distance_usd
        else:  # SHORT
            stop_price   = entry_price + stop_distance_usd
            target_price = entry_price - target_distance_usd

        return SizeResult(
            quantity=qty,
            notional_usd=round(notional, 4),
            stop_price=round(stop_price, 2),
            target_price=round(target_price, 2),
            stop_distance_usd=round(stop_distance_usd, 4),
            target_distance_usd=round(target_distance_usd, 4),
            effective_risk_usd=round(qty * stop_distance_usd, 4),
            effective_risk_pct=round(qty * stop_distance_usd / self._balance, 6),
            capped=capped,
        )

    def check_daily_drawdown(self) -> bool:
        """FIX: > not >= so that exactly reaching the limit does not halt."""
        if self._daily_pnl < 0:
            denominator = self._balance if self._balance > 0 else settings.ACCOUNT_CAPITAL
            dd = abs(self._daily_pnl) / denominator
            if dd > settings.DAILY_DRAWDOWN_LIMIT:
                logger.critical(
                    "CIRCUIT BREAKER: daily DD %.2f%% > %.0f%% limit (balance=%.4f) — halt",
                    dd * 100, settings.DAILY_DRAWDOWN_LIMIT * 100, self._balance
                )
                return False
        return True

    def check_min_balance(self) -> bool:
        if self._balance < settings.MIN_ACCOUNT_BALANCE:
            logger.critical(
                "CIRCUIT BREAKER: balance %.4f < min %.2f — halt",
                self._balance, settings.MIN_ACCOUNT_BALANCE
            )
            return False
        return True

    def check_max_daily_trades(self) -> bool:
        if self._daily_trades >= settings.MAX_TRADES_PER_DAY:
            logger.warning("Daily trade limit reached: %d/%d", self._daily_trades, settings.MAX_TRADES_PER_DAY)
            return False
        return True

    def all_circuit_breakers_pass(self) -> bool:
        return (self.check_daily_drawdown() and
                self.check_min_balance() and
                self.check_max_daily_trades())
