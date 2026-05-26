"""
Backtesting engine with real OHLCV data from Crypto.com REST API.

Fill model:
  - Entry: taker fill at next-bar open (no look-ahead)
  - Exit: ROUND_TRIP_FEE_RATE applied once per trade (covers entry + exit combined)
  - No separate entry-fee balance deduction (FIX: was double-counted previously)

Gates (must ALL pass before live trading is permitted):
  - win_rate     >= MIN_WIN_RATE
  - avg_rr       >= MIN_RR_RATIO
  - n_trades     >= MIN_BACKTEST_TRADES
  - max_drawdown <= MAX_BACKTEST_DRAWDOWN
"""
import logging, math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    instrument: str
    direction: str
    entry_bar: int
    entry_price: float
    exit_bar: int
    exit_price: float
    quantity: float
    stop_price: float
    target_price: float
    exit_reason: str
    gross_pnl: float
    fee_cost: float
    net_pnl: float
    rr_achieved: float


@dataclass
class BacktestResult:
    instrument: str
    n_trades: int
    win_rate: float
    avg_rr: float
    total_net_pnl: float
    max_drawdown: float
    sharpe: float
    gate_pass: bool
    gate_failures: List[str] = field(default_factory=list)
    trades: List[BacktestTrade] = field(default_factory=list)


def _fetch_candles(client, instrument: str, timeframe: str) -> List[Dict]:
    logger.info("Fetching candles: %s %s (max 50 per API call)", instrument, timeframe)
    try:
        candles = client.get_candlesticks(instrument, timeframe, count=50)
    except Exception as exc:
        logger.error("Failed to fetch candles for %s: %s", instrument, exc)
        return []
    if not candles:
        logger.warning("No candle data returned for %s", instrument)
    logger.info("Fetched %d candles for %s", len(candles), instrument)
    return candles


def _run_backtest(client, instrument: str, strategy_class: str, timeframe: str) -> BacktestResult:
    candles = _fetch_candles(client, instrument, timeframe)
    if len(candles) < 10:
        return BacktestResult(
            instrument=instrument, n_trades=0, win_rate=0.0, avg_rr=0.0,
            total_net_pnl=0.0, max_drawdown=1.0, sharpe=0.0, gate_pass=False,
            gate_failures=[f"INSUFFICIENT_DATA: {len(candles)} candles (need >=10)"],
        )

    if strategy_class == "MOMENTUM":
        from strategy.btc_momentum import BTCMomentumStrategy
        strat = BTCMomentumStrategy()
    else:
        from strategy.eth_mean_reversion import ETHMeanReversionStrategy
        strat = ETHMeanReversionStrategy()

    trades: List[BacktestTrade] = []
    balance = settings.ACCOUNT_CAPITAL
    peak    = balance
    max_dd  = 0.0

    in_pos    = False
    ep = tp = sp_price = eq = 0.0
    ebar      = 0
    direction = "LONG"
    last_sig  = -999

    for i, candle in enumerate(candles):
        h = float(candle["h"]); l = float(candle["l"]); c = float(candle["c"])
        strat.push_h1_candle(candle)

        # ── Manage open position ──────────────────────────────────────────
        if in_pos:
            exit_price = None; exit_reason = None

            if direction == "LONG":
                if l <= sp_price:
                    exit_price = sp_price; exit_reason = "STOP"
                elif h >= tp:
                    exit_price = tp; exit_reason = "TARGET"
                elif (i - ebar) >= settings.MAX_HOLD_BARS:
                    exit_price = c; exit_reason = "TIMEOUT"
            else:  # SHORT
                if h >= sp_price:
                    exit_price = sp_price; exit_reason = "STOP"
                elif l <= tp:
                    exit_price = tp; exit_reason = "TARGET"
                elif (i - ebar) >= settings.MAX_HOLD_BARS:
                    exit_price = c; exit_reason = "TIMEOUT"

            if exit_price is not None:
                fee = eq * ep * settings.ROUND_TRIP_FEE_RATE
                if direction == "LONG":
                    gross = (exit_price - ep) * eq
                    rr = (exit_price - ep) / (ep - sp_price) if ep != sp_price else 0.0
                else:
                    gross = (ep - exit_price) * eq
                    rr = (ep - exit_price) / (sp_price - ep) if sp_price != ep else 0.0
                net = gross - fee
                trades.append(BacktestTrade(
                    instrument=instrument, direction=direction,
                    entry_bar=ebar, entry_price=ep,
                    exit_bar=i, exit_price=exit_price,
                    quantity=eq, stop_price=sp_price, target_price=tp,
                    exit_reason=exit_reason, gross_pnl=gross, fee_cost=fee,
                    net_pnl=net, rr_achieved=rr,
                ))
                balance += net
                peak    = max(peak, balance)
                dd      = (peak - balance) / peak if peak > 0 else 0.0
                max_dd  = max(max_dd, dd)
                in_pos  = False
            continue

        # ── Look for signal ───────────────────────────────────────────────
        if (i - last_sig) < settings.REENTRY_COOLDOWN_BARS:
            continue

        audit = strat.evaluate(spread_pct=0.000013)   # live spread baseline

        if audit.signal not in ("LONG", "SHORT"):
            continue

        # No-look-ahead: fill on next bar's open
        if i + 1 >= len(candles):
            continue

        fp = float(candles[i + 1]["o"])
        if fp <= 0:
            continue

        stop_d, target_d = strat.get_stop_and_target()
        if stop_d <= 0:
            continue

        risk_usd = balance * settings.RISK_PCT_PER_TRADE
        qty_risk = risk_usd / stop_d
        qty_cap  = (balance * settings.MAX_POSITION_PCT) / fp
        qty_raw  = min(qty_risk, qty_cap)

        tick = settings.BTC_QTY_TICK if instrument == settings.BTC_INSTRUMENT else settings.ETH_QTY_TICK
        qty  = math.floor(qty_raw / tick) * tick

        if qty <= 0 or qty * fp < settings.MIN_ORDER_NOTIONAL:
            continue

        # FIX: no upfront balance deduction for entry fee
        # Fee accounted for in ROUND_TRIP_FEE_RATE at trade close
        ep = fp
        direction = audit.signal
        sp_price  = fp - stop_d   if direction == "LONG" else fp + stop_d
        tp        = fp + target_d if direction == "LONG" else fp - target_d
        eq        = qty
        ebar      = i + 1
        in_pos    = True
        last_sig  = i

    # ── Force-close any open position at last bar ─────────────────────────
    if in_pos and candles:
        last_c  = float(candles[-1]["c"])
        fee     = eq * ep * settings.ROUND_TRIP_FEE_RATE
        gross   = (last_c - ep) * eq if direction == "LONG" else (ep - last_c) * eq
        net     = gross - fee
        rr_val  = (gross / (eq * abs(ep - sp_price))) if sp_price != ep else 0.0
        trades.append(BacktestTrade(
            instrument=instrument, direction=direction,
            entry_bar=ebar, entry_price=ep,
            exit_bar=len(candles) - 1, exit_price=last_c,
            quantity=eq, stop_price=sp_price, target_price=tp,
            exit_reason="END_OF_DATA", gross_pnl=gross, fee_cost=fee,
            net_pnl=net, rr_achieved=rr_val,
        ))
        balance += net

    # ── Gate evaluation ───────────────────────────────────────────────────
    if not trades:
        return BacktestResult(
            instrument=instrument, n_trades=0, win_rate=0.0, avg_rr=0.0,
            total_net_pnl=0.0, max_drawdown=max_dd, sharpe=0.0, gate_pass=False,
            gate_failures=["NO_TRADES: strategy produced zero signals on available data"],
        )

    wins       = [t for t in trades if t.net_pnl > 0]
    wr         = len(wins) / len(trades)
    avg_rr     = sum(t.rr_achieved for t in trades) / len(trades)
    total_pnl  = sum(t.net_pnl for t in trades)
    pnl_series = [t.net_pnl / settings.ACCOUNT_CAPITAL for t in trades]

    if len(pnl_series) > 1:
        mean_r = sum(pnl_series) / len(pnl_series)
        std_r  = (sum((r - mean_r) ** 2 for r in pnl_series) / (len(pnl_series) - 1)) ** 0.5
        sharpe = (mean_r / std_r * (252 ** 0.5)) if std_r > 0 else 0.0
    else:
        sharpe = 0.0

    fails = []
    if wr < settings.MIN_WIN_RATE:
        fails.append(f"WIN_RATE {wr:.4f} < {settings.MIN_WIN_RATE}")
    if avg_rr < settings.MIN_RR_RATIO:
        fails.append(f"AVG_RR {avg_rr:.4f} < {settings.MIN_RR_RATIO}")
    if len(trades) < settings.MIN_BACKTEST_TRADES:
        fails.append(f"TRADE_COUNT {len(trades)} < {settings.MIN_BACKTEST_TRADES}")
    if max_dd > settings.MAX_BACKTEST_DRAWDOWN:
        fails.append(f"MAX_DRAWDOWN {max_dd:.4f} > {settings.MAX_BACKTEST_DRAWDOWN}")

    gate = len(fails) == 0
    status = "PASS" if gate else f"FAIL {fails}"
    logger.info(
        "BACKTEST %s: trades=%d wr=%.1f%% rr=%.2f dd=%.1f%% sharpe=%.2f → %s",
        instrument, len(trades), wr * 100, avg_rr, max_dd * 100, sharpe, status,
    )
    return BacktestResult(
        instrument=instrument, n_trades=len(trades), win_rate=wr, avg_rr=avg_rr,
        total_net_pnl=total_pnl, max_drawdown=max_dd, sharpe=sharpe,
        gate_pass=gate, gate_failures=fails, trades=trades,
    )


def run_startup_backtest(client) -> Tuple[BacktestResult, BacktestResult]:
    logger.info("=" * 60)
    logger.info("STARTUP BACKTEST GATE")
    logger.info("Gates: win_rate>=%.0f%% avg_rr>=%.1f trades>=%d drawdown<=%.0f%%",
                settings.MIN_WIN_RATE * 100, settings.MIN_RR_RATIO,
                settings.MIN_BACKTEST_TRADES, settings.MAX_BACKTEST_DRAWDOWN * 100)
    logger.info("=" * 60)

    btc = _run_backtest(client, settings.BTC_INSTRUMENT, "MOMENTUM",    settings.BTC_CANDLE_TF)
    eth = _run_backtest(client, settings.ETH_INSTRUMENT, "MEAN_REVERSION", settings.ETH_CANDLE_TF)

    for r in [btc, eth]:
        if r.gate_pass:
            logger.info("GATE PASS %s: wr=%.1f%% rr=%.2f trades=%d dd=%.1f%% sharpe=%.2f",
                        r.instrument, r.win_rate * 100, r.avg_rr,
                        r.n_trades, r.max_drawdown * 100, r.sharpe)
        else:
            logger.critical("GATE FAIL %s: %s", r.instrument, r.gate_failures)

    return btc, eth
