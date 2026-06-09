"""
Backtest engine. Fetches real OHLCV via paginated REST calls.
Fill model: taker fee both sides, realistic slippage on entry (next open).
No look-ahead: signals computed on candles[0..i], fill on candles[i+1].
Startup gate: all 4 conditions must pass or main.py calls sys.exit(2).
FIX: D1 candles fetched and fed to BTCMomentumStrategy before backtest loop.
FIX: Paginated candle fetch — single 50-bar call cannot produce 100 trades.
"""
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from broker.rest_client import CryptoComRestClient
from config import settings
from strategy.btc_momentum import BTCMomentumStrategy
from strategy.eth_mean_reversion import ETHMeanReversionStrategy
from strategy.indicators import atr, last_valid

logger = logging.getLogger(__name__)

# Candle interval in milliseconds — used for pagination
_TF_MS: Dict[str, int] = {
    "1m":  60_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "4h":  14_400_000,
    "1D":  86_400_000,
}


@dataclass
class BacktestTrade:
    instrument:  str
    direction:   str
    entry_bar:   int
    entry_price: float
    exit_bar:    int
    exit_price:  float
    quantity:    float
    stop_price:  float
    target_price: float
    exit_reason: str
    gross_pnl:   float
    fee_cost:    float
    net_pnl:     float
    rr_achieved: float


@dataclass
class BacktestResult:
    instrument:   str
    n_trades:     int
    win_rate:     float
    avg_rr:       float
    total_net_pnl: float
    max_drawdown: float
    sharpe:       float
    gate_pass:    bool
    gate_failures: List[str] = field(default_factory=list)
    trades:       List[BacktestTrade] = field(default_factory=list)


def _fetch_paginated(client: CryptoComRestClient,
                     instrument: str, timeframe: str, days: int) -> List[Dict]:
    """
    Paginate backwards by end_ts until we have enough candles to cover `days`.
    Deduplicates by timestamp. Returns chronological order.
    """
    interval_ms = _TF_MS.get(timeframe, 3_600_000)
    needed      = int(days * 86_400_000 / interval_ms) + 10
    logger.info("Fetching %s %s — target %d candles (~%d days)", instrument, timeframe, needed, days)

    all_candles: Dict[int, Dict] = {}
    end_ts      = int(time.time() * 1000)
    max_iters   = math.ceil(needed / 50) + 3

    for iteration in range(max_iters):
        try:
            batch = client.get_candlesticks(instrument, timeframe, count=50, end_ts=end_ts)
        except Exception as exc:
            logger.warning("Candle fetch error on iter %d: %s", iteration, exc)
            time.sleep(2)
            continue

        if not batch:
            break

        for c in batch:
            all_candles[int(c["t"])] = c

        oldest_ts = min(int(c["t"]) for c in batch)
        if len(all_candles) >= needed:
            break

        end_ts = oldest_ts - 1
        time.sleep(0.15)  # stay within rate limit

    candles = sorted(all_candles.values(), key=lambda c: c["t"])
    logger.info("Fetched %d %s %s candles (needed %d)", len(candles), instrument, timeframe, needed)
    return candles


def _run_backtest(client: CryptoComRestClient, instrument: str,
                  strategy_class: str, timeframe: str, days: int) -> BacktestResult:

    candles = _fetch_paginated(client, instrument, timeframe, days)
    if len(candles) < 30:
        return BacktestResult(
            instrument=instrument, n_trades=0, win_rate=0.0, avg_rr=0.0,
            total_net_pnl=0.0, max_drawdown=1.0, sharpe=0.0, gate_pass=False,
            gate_failures=[f"INSUFFICIENT_DATA: only {len(candles)} candles returned"],
        )

    strat: BTCMomentumStrategy | ETHMeanReversionStrategy

    if strategy_class == "MOMENTUM":
        strat = BTCMomentumStrategy()
        # FIX: fetch and feed D1 candles before backtest loop.
        # Without D1 feed, _d1c is always empty, trend_gate_pass is always False,
        # BTC produces 0 trades, MIN_BACKTEST_TRADES gate fails, bot never starts.
        try:
            d1_candles = _fetch_paginated(client, instrument, "1D", days + 60)
            for dc in d1_candles:
                strat.push_d1_candle(dc)
            logger.info("Fed %d D1 candles to BTC strategy", len(d1_candles))
        except Exception as exc:
            logger.warning("D1 candle fetch failed: %s — trend gate will be inactive", exc)
    else:
        strat = ETHMeanReversionStrategy()

    trades:   List[BacktestTrade] = []
    balance   = settings.ACCOUNT_CAPITAL
    peak      = balance
    max_dd    = 0.0
    in_pos    = False
    ep = sp = tp = eq = 0.0
    direction = "LONG"
    ebar      = 0
    last_sig  = -999

    for i, candle in enumerate(candles):
        h = float(candle["h"])
        l = float(candle["l"])
        c = float(candle["c"])

        strat.push_h1_candle(candle)

        # ── Manage open position ──────────────────────────────────────────────
        if in_pos:
            if direction == "LONG":
                if l <= sp:
                    fee = eq * sp * settings.ROUND_TRIP_FEE_RATE
                    net = (sp - ep) * eq - fee
                    trades.append(BacktestTrade(
                        instrument, "LONG", ebar, ep, i, sp, eq, sp, tp,
                        "STOP", (sp - ep) * eq, fee, net, -1.0))
                    balance += net; in_pos = False
                elif h >= tp:
                    fee   = eq * tp * settings.ROUND_TRIP_FEE_RATE
                    gross = (tp - ep) * eq
                    net   = gross - fee
                    rr    = (tp - ep) / (ep - sp) if (ep - sp) != 0 else settings.MIN_RR_RATIO
                    trades.append(BacktestTrade(
                        instrument, "LONG", ebar, ep, i, tp, eq, sp, tp,
                        "TARGET", gross, fee, net, rr))
                    balance += net; in_pos = False
                elif (i - ebar) >= settings.MAX_HOLD_BARS:
                    fee   = eq * c * settings.ROUND_TRIP_FEE_RATE
                    gross = (c - ep) * eq
                    net   = gross - fee
                    rr    = (c - ep) / (ep - sp) if (ep - sp) != 0 else 0.0
                    trades.append(BacktestTrade(
                        instrument, "LONG", ebar, ep, i, c, eq, sp, tp,
                        "TIMEOUT", gross, fee, net, rr))
                    balance += net; in_pos = False
            else:  # SHORT
                if h >= sp:
                    fee   = eq * sp * settings.ROUND_TRIP_FEE_RATE
                    gross = (ep - sp) * eq
                    net   = gross - fee
                    trades.append(BacktestTrade(
                        instrument, "SHORT", ebar, ep, i, sp, eq, sp, tp,
                        "STOP", gross, fee, net, -1.0))
                    balance += net; in_pos = False
                elif l <= tp:
                    fee   = eq * tp * settings.ROUND_TRIP_FEE_RATE
                    gross = (ep - tp) * eq
                    net   = gross - fee
                    rr    = (ep - tp) / (sp - ep) if (sp - ep) != 0 else settings.MIN_RR_RATIO
                    trades.append(BacktestTrade(
                        instrument, "SHORT", ebar, ep, i, tp, eq, sp, tp,
                        "TARGET", gross, fee, net, rr))
                    balance += net; in_pos = False
                elif (i - ebar) >= settings.MAX_HOLD_BARS:
                    fee   = eq * c * settings.ROUND_TRIP_FEE_RATE
                    gross = (ep - c) * eq
                    net   = gross - fee
                    trades.append(BacktestTrade(
                        instrument, "SHORT", ebar, ep, i, c, eq, sp, tp,
                        "TIMEOUT", gross, fee, net, 0.0))
                    balance += net; in_pos = False

            if not in_pos:
                peak   = max(peak, balance)
                dd     = (peak - balance) / peak if peak > 0 else 0.0
                max_dd = max(max_dd, dd)
            continue

        # ── Cooldown ──────────────────────────────────────────────────────────
        if (i - last_sig) < settings.REENTRY_COOLDOWN_BARS:
            continue

        # ── Evaluate signal ───────────────────────────────────────────────────
        audit = strat.evaluate(spread_pct=0.000013)
        if audit.signal not in ("LONG", "SHORT"):
            continue
        if i + 1 >= len(candles):
            continue  # no next candle to fill on

        # Fill on next candle open (no look-ahead into current candle)
        fp  = float(candles[i + 1]["o"])
        sd, td = strat.get_stop_and_target()
        if sd <= 0:
            continue

        # Sizing
        risk_usd  = balance * settings.RISK_PCT_PER_TRADE
        qty_risk  = risk_usd / sd
        qty_cap   = (balance * settings.MAX_POSITION_PCT) / fp
        qty_raw   = min(qty_risk, qty_cap)
        tick      = (settings.BTC_QTY_TICK if instrument == settings.BTC_INSTRUMENT
                     else settings.ETH_QTY_TICK)
        qty       = math.floor(qty_raw / tick) * tick

        if qty <= 0 or qty * fp < settings.MIN_ORDER_NOTIONAL:
            continue

        # Deduct entry fee
        balance -= qty * fp * settings.TAKER_FEE_RATE

        ep        = fp
        direction = audit.signal
        sp = (fp - sd) if direction == "LONG" else (fp + sd)
        tp = (fp + td) if direction == "LONG" else (fp - td)
        eq        = qty
        ebar      = i + 1
        in_pos    = True
        last_sig  = i

    # ── Aggregate statistics ─────────────────────────────────────────────────
    if not trades:
        return BacktestResult(
            instrument=instrument, n_trades=0, win_rate=0.0, avg_rr=0.0,
            total_net_pnl=0.0, max_drawdown=max_dd, sharpe=0.0, gate_pass=False,
            gate_failures=["NO_TRADES — check D1 feed, volume field 'vv', signal conditions"],
        )

    wins      = [t for t in trades if t.net_pnl > 0]
    win_rate  = len(wins) / len(trades)
    avg_rr    = sum(t.rr_achieved for t in trades) / len(trades)
    total_pnl = sum(t.net_pnl for t in trades)
    daily_r   = [t.net_pnl / settings.ACCOUNT_CAPITAL for t in trades]

    if len(daily_r) > 1:
        mean_r = sum(daily_r) / len(daily_r)
        std_r  = (sum((r - mean_r) ** 2 for r in daily_r) / (len(daily_r) - 1)) ** 0.5
        sharpe = (mean_r / std_r * (252 ** 0.5)) if std_r > 0 else 0.0
    else:
        sharpe = 0.0

    # ── Gate evaluation ───────────────────────────────────────────────────────
    failures: List[str] = []
    if win_rate   < settings.MIN_WIN_RATE:
        failures.append(f"WIN_RATE {win_rate:.4f} < {settings.MIN_WIN_RATE}")
    if avg_rr     < settings.MIN_RR_RATIO:
        failures.append(f"AVG_RR {avg_rr:.4f} < {settings.MIN_RR_RATIO}")
    if len(trades) < settings.MIN_BACKTEST_TRADES:
        failures.append(f"TRADE_COUNT {len(trades)} < {settings.MIN_BACKTEST_TRADES}")
    if max_dd     > settings.MAX_BACKTEST_DRAWDOWN:
        failures.append(f"MAX_DRAWDOWN {max_dd:.4f} > {settings.MAX_BACKTEST_DRAWDOWN}")

    gate_pass = len(failures) == 0
    status    = "PASS" if gate_pass else f"FAIL {failures}"
    logger.info(
        "BACKTEST %s | trades=%d wr=%.1f%% rr=%.2f dd=%.1f%% sharpe=%.2f → %s",
        instrument, len(trades), win_rate * 100, avg_rr, max_dd * 100, sharpe, status,
    )

    return BacktestResult(
        instrument=instrument, n_trades=len(trades),
        win_rate=win_rate, avg_rr=avg_rr,
        total_net_pnl=total_pnl, max_drawdown=max_dd,
        sharpe=sharpe, gate_pass=gate_pass, gate_failures=failures, trades=trades,
    )


def run_startup_backtest(
    client: CryptoComRestClient,
) -> Tuple[BacktestResult, BacktestResult]:
    logger.info("=" * 60)
    logger.info("STARTUP BACKTEST — verifying strategy viability before live trading")
    logger.info("=" * 60)

    btc_result = _run_backtest(
        client, settings.BTC_INSTRUMENT,
        settings.BTC_STRATEGY_CLASS,
        settings.BTC_CANDLE_TF,
        settings.BACKTEST_DAYS,
    )
    eth_result = _run_backtest(
        client, settings.ETH_INSTRUMENT,
        settings.ETH_STRATEGY_CLASS,
        settings.ETH_CANDLE_TF,
        settings.BACKTEST_DAYS,
    )

    for r in [btc_result, eth_result]:
        if r.gate_pass:
            logger.info("GATE PASS %-15s | wr=%.1f%% rr=%.2f trades=%d dd=%.1f%%",
                        r.instrument, r.win_rate * 100, r.avg_rr, r.n_trades, r.max_drawdown * 100)
        else:
            logger.critical("GATE FAIL %-15s | %s", r.instrument, r.gate_failures)

    return btc_result, eth_result
