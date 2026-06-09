"""
Shared in-process state bus. One singleton instance imported by both
bot_engine and the dashboard API. No IPC, no Redis — same asyncio loop.

Thread safety: all mutations go through asyncio.Lock. The FastAPI handlers
and bot callbacks run in the same event loop so there is no actual thread
contention, but the lock makes future refactoring safe.
"""
import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any


# ── Data shapes ───────────────────────────────────────────────────────────────

@dataclass
class PositionSnap:
    direction:      str
    fill_price:     float
    quantity:       float
    stop_price:     float
    target_price:   float
    entry_ts:       float
    unrealized_pnl: float = 0.0
    bars_held:      int   = 0


@dataclass
class EngineSnap:
    instrument:     str
    state:          str          # FLAT / ENTRY_PENDING / POSITION_OPEN / EXIT_PENDING
    last_bid:       float = 0.0
    last_ask:       float = 0.0
    spread_pct:     float = 0.0
    last_candle_ts: int   = 0
    position:       Optional[PositionSnap] = None


@dataclass
class CircuitSnap:
    min_balance_ok:      bool  = True
    daily_drawdown_ok:   bool  = True
    max_trades_ok:       bool  = True
    daily_drawdown_pct:  float = 0.0


@dataclass
class RiskSnap:
    balance:          float = 0.0
    session_peak:     float = 0.0
    daily_pnl:        float = 0.0
    daily_trades:     int   = 0
    max_daily_trades: int   = 3
    min_balance:      float = 12.0
    drawdown_limit:   float = 0.10
    circuits:         CircuitSnap = field(default_factory=CircuitSnap)


@dataclass
class BacktestSnap:
    instrument:     str
    gate_pass:      bool        = False
    n_trades:       int         = 0
    win_rate:       float       = 0.0
    avg_rr:         float       = 0.0
    max_drawdown:   float       = 0.0
    sharpe:         float       = 0.0
    gate_failures:  List[str]   = field(default_factory=list)


@dataclass
class ClosedTradeSnap:
    id:               int
    instrument:       str
    direction:        str
    fill_price:       float
    exit_price:       float
    quantity:         float
    stop_price:       float
    target_price:     float
    exit_reason:      str
    realized_pnl:     float
    entry_ts:         float
    exit_ts:          float
    duration_minutes: float


# ── Singleton store ───────────────────────────────────────────────────────────

class StateStore:
    def __init__(self):
        self._lock           = asyncio.Lock()
        self.kill_event      = asyncio.Event()
        self._kill_triggered = False
        self._dry_run        = True
        self._start_time     = time.time()
        self._trade_counter  = 0

        self._engines: Dict[str, EngineSnap] = {}
        self._risk           = RiskSnap()
        self._backtest: Dict[str, BacktestSnap] = {}
        self._recent_trades: List[ClosedTradeSnap] = []   # last 200 in memory

    # ── Reads (no lock needed for simple reads in same event loop) ────────────

    def snapshot(self) -> Dict[str, Any]:
        return {
            "dry_run":        self._dry_run,
            "kill_triggered": self._kill_triggered,
            "uptime_seconds": int(time.time() - self._start_time),
            "risk":           asdict(self._risk),
            "engines":        {k: _engine_to_dict(v) for k, v in self._engines.items()},
            "backtest":       {k: asdict(v) for k, v in self._backtest.items()},
        }

    def recent_trades(self, limit: int = 50) -> List[Dict]:
        return [asdict(t) for t in self._recent_trades[-limit:]]

    # ── Writes ────────────────────────────────────────────────────────────────

    def set_startup(self, dry_run: bool) -> None:
        self._dry_run    = dry_run
        self._start_time = time.time()

    def set_backtest_results(self, results) -> None:
        """Accept list or pair of BacktestResult dataclass instances."""
        if not isinstance(results, (list, tuple)):
            results = [results]
        for r in results:
            self._backtest[r.instrument] = BacktestSnap(
                instrument=r.instrument,
                gate_pass=r.gate_pass,
                n_trades=r.n_trades,
                win_rate=r.win_rate,
                avg_rr=r.avg_rr,
                max_drawdown=r.max_drawdown,
                sharpe=r.sharpe,
                gate_failures=list(r.gate_failures),
            )

    def register_engine(self, instrument: str) -> None:
        if instrument not in self._engines:
            self._engines[instrument] = EngineSnap(instrument=instrument, state="FLAT")

    def update_engine_state(self, instrument: str, state: str,
                            position: Optional[PositionSnap] = None) -> None:
        if instrument not in self._engines:
            self._engines[instrument] = EngineSnap(instrument=instrument, state=state)
        else:
            self._engines[instrument].state    = state
            self._engines[instrument].position = position

    def update_engine_book(self, instrument: str, bid: float, ask: float,
                           spread_pct: float, candle_ts: int = 0) -> None:
        if instrument not in self._engines:
            self._engines[instrument] = EngineSnap(instrument=instrument, state="FLAT")
        eng = self._engines[instrument]
        eng.last_bid       = bid
        eng.last_ask       = ask
        eng.spread_pct     = spread_pct
        if candle_ts:
            eng.last_candle_ts = candle_ts
        # Refresh unrealized PnL with latest prices
        if eng.position:
            pos = eng.position
            if pos.direction == "LONG":
                eng.position.unrealized_pnl = round((bid - pos.fill_price) * pos.quantity, 6)
            else:
                eng.position.unrealized_pnl = round((pos.fill_price - ask) * pos.quantity, 6)

    def update_risk(self, balance: float, session_peak: float, daily_pnl: float,
                    daily_trades: int, max_daily_trades: int,
                    min_balance: float, drawdown_limit: float) -> None:
        import math
        dd_pct = abs(daily_pnl) / max(balance, min_balance) if daily_pnl < 0 else 0.0
        self._risk = RiskSnap(
            balance=balance,
            session_peak=session_peak,
            daily_pnl=daily_pnl,
            daily_trades=daily_trades,
            max_daily_trades=max_daily_trades,
            min_balance=min_balance,
            drawdown_limit=drawdown_limit,
            circuits=CircuitSnap(
                min_balance_ok=balance >= min_balance,
                daily_drawdown_ok=dd_pct < drawdown_limit,
                max_trades_ok=daily_trades < max_daily_trades,
                daily_drawdown_pct=round(dd_pct, 4),
            ),
        )

    def record_closed_trade(self, instrument: str, direction: str,
                            fill_price: float, exit_price: float,
                            quantity: float, stop_price: float,
                            target_price: float, exit_reason: str,
                            realized_pnl: float, entry_ts: float,
                            exit_ts: float) -> ClosedTradeSnap:
        self._trade_counter += 1
        dur = round((exit_ts - entry_ts) / 60.0, 1) if exit_ts > entry_ts else 0.0
        t = ClosedTradeSnap(
            id=self._trade_counter,
            instrument=instrument, direction=direction,
            fill_price=fill_price, exit_price=exit_price,
            quantity=quantity, stop_price=stop_price,
            target_price=target_price, exit_reason=exit_reason,
            realized_pnl=realized_pnl, entry_ts=entry_ts,
            exit_ts=exit_ts, duration_minutes=dur,
        )
        self._recent_trades.append(t)
        if len(self._recent_trades) > 200:
            self._recent_trades = self._recent_trades[-200:]
        return t

    def trigger_kill(self) -> None:
        self._kill_triggered = True
        self.kill_event.set()


def _engine_to_dict(e: EngineSnap) -> Dict:
    d = asdict(e)
    return d


# Module-level singleton — import this everywhere
store = StateStore()
