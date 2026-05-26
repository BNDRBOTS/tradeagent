"""
BTC/USDT H1 Momentum Strategy.
Signals: D1 EMA trend gate + ADX rising + MACD bullish cross + volume confirmation + ATR spike filter.
Per-candle audit log: every boolean condition recorded regardless of signal outcome.
"""
import logging, math
from dataclasses import dataclass
from typing import Dict, List, Tuple
from config import settings
from strategy.indicators import adx, atr, ema, last_valid, macd, sma

logger = logging.getLogger(__name__)


@dataclass
class BTCAuditEntry:
    timestamp: str; close: float
    d1_ema_fast: float; d1_ema_slow: float; trend_gate_pass: bool
    adx_value: float; adx_rising: bool; adx_pass: bool
    macd_line: float; macd_signal: float; macd_cross: bool
    volume_usd: float; volume_sma: float; volume_pass: bool
    atr_value: float; atr_baseline: float; atr_spike_pass: bool
    spread_pct: float; spread_pass: bool; signal: str


class BTCMomentumStrategy:
    def __init__(self):
        self._h1: List[Dict] = []
        self._d1c: List[float] = []
        self._d1h: List[float] = []
        self._d1l: List[float] = []
        self._last_sig_bar = -999
        self._bar = 0
        regime = settings.BTC_REGIME_MODE
        if regime == "CONSOLIDATION_RECOVERY":
            self._ema_fast = settings.BTC_EMA_FAST_REGIME
            self._ema_slow = settings.BTC_EMA_SLOW_REGIME
        else:
            self._ema_fast = settings.BTC_EMA_FAST_STD
            self._ema_slow = settings.BTC_EMA_SLOW_STD
        logger.info("BTCMomentum init | regime=%s EMA=%d/%d", regime, self._ema_fast, self._ema_slow)

    def push_h1_candle(self, c: Dict) -> None:
        self._h1.append(c)
        if len(self._h1) > 500: self._h1 = self._h1[-500:]
        self._bar += 1

    def push_d1_candle(self, c: Dict) -> None:
        self._d1c.append(float(c["c"]))
        self._d1h.append(float(c["h"]))
        self._d1l.append(float(c["l"]))
        if len(self._d1c) > 300:
            self._d1c = self._d1c[-300:]
            self._d1h = self._d1h[-300:]
            self._d1l = self._d1l[-300:]

    def evaluate(self, spread_pct: float = 0.0) -> BTCAuditEntry:
        nan = float("nan")
        n = len(self._h1)
        min_bars = max(settings.ATR_BASELINE_PERIOD, settings.MACD_SLOW + settings.MACD_SIGNAL,
                       settings.ADX_PERIOD * 2, settings.BTC_VOLUME_MA_PERIOD)
        if n < min_bars:
            return self._empty("INSUFFICIENT_DATA", spread_pct)

        closes = [float(c["c"]) for c in self._h1]
        highs  = [float(c["h"]) for c in self._h1]
        lows   = [float(c["l"]) for c in self._h1]
        vols   = [float(c.get("volume_usd", 0)) for c in self._h1]
        ts = str(self._h1[-1].get("t", "?"))

        # ── Trend gate (D1 EMA) ───────────────────────────────────────────────
        tg = False; d1f = nan; d1s = nan
        if len(self._d1c) >= self._ema_slow:
            fast_e = ema(self._d1c, self._ema_fast)
            slow_e = ema(self._d1c, self._ema_slow)
            d1f = last_valid(fast_e); d1s = last_valid(slow_e)
            if not math.isnan(d1f) and not math.isnan(d1s) and d1s > 0:
                sep = (d1f - d1s) / d1s
                tg = d1f > d1s and sep >= settings.BTC_MIN_EMA_SEP_PCT

        # ── ADX ───────────────────────────────────────────────────────────────
        adxv, _, _ = adx(highs, lows, closes, settings.ADX_PERIOD)
        av = last_valid(adxv)
        lb = settings.ADX_RISING_LOOKBACK
        recent = [v for v in adxv[-(lb + 1):] if not math.isnan(v)]
        ar = len(recent) >= 2 and recent[-1] > recent[0]
        ap = (not math.isnan(av)) and av >= settings.ADX_ENTRY_THRESHOLD and ar

        # ── MACD bullish cross (prev bar below signal, current bar above) ────
        ml, sl, _ = macd(closes, settings.MACD_FAST, settings.MACD_SLOW, settings.MACD_SIGNAL)
        mv = last_valid(ml); sv = last_valid(sl)
        pm = ml[-2] if len(ml) >= 2 and not math.isnan(ml[-2]) else nan
        ps = sl[-2] if len(sl) >= 2 and not math.isnan(sl[-2]) else nan
        mc = (not math.isnan(mv) and not math.isnan(sv) and
              not math.isnan(pm) and not math.isnan(ps) and
              pm <= ps and mv > sv)

        # ── Volume confirmation ───────────────────────────────────────────────
        vsma_vals = sma(vols, settings.BTC_VOLUME_MA_PERIOD)
        vsv = last_valid(vsma_vals)
        vp = (not math.isnan(vsv) and vsv > 0 and
              vols[-1] >= vsv * settings.BTC_VOLUME_MULTIPLIER)

        # ── ATR spike filter ─────────────────────────────────────────────────
        atv = atr(highs, lows, closes, settings.ATR_PERIOD)
        av2 = last_valid(atv)
        valid_atrs = [v for v in atv if not math.isnan(v)]
        bp = min(settings.ATR_BASELINE_PERIOD, len(valid_atrs))
        ab = sum(valid_atrs[-bp:]) / bp if bp > 0 else nan
        asp = (not math.isnan(av2) and not math.isnan(ab) and
               av2 < ab * settings.ATR_SPIKE_MULTIPLIER)

        # ── Spread guard ──────────────────────────────────────────────────────
        sgp = spread_pct < settings.SPREAD_GUARD_THRESHOLD_PCT

        # ── Cooldown ──────────────────────────────────────────────────────────
        cp = (self._bar - self._last_sig_bar) >= settings.REENTRY_COOLDOWN_BARS

        sig = "LONG" if (tg and ap and mc and vp and asp and sgp and cp) else "NONE"
        if sig == "LONG":
            self._last_sig_bar = self._bar
            logger.info("BTC LONG signal bar=%d close=%.2f adx=%.1f macd=%.4f",
                        self._bar, closes[-1], av, mv)
        else:
            logger.debug("BTC NONE bar=%d tg=%s adx_p=%s macd=%s vol=%s spread=%s cooldown=%s",
                         self._bar, tg, ap, mc, vp, sgp, cp)

        return BTCAuditEntry(
            timestamp=ts, close=closes[-1],
            d1_ema_fast=round(d1f, 2) if not math.isnan(d1f) else nan,
            d1_ema_slow=round(d1s, 2) if not math.isnan(d1s) else nan,
            trend_gate_pass=tg,
            adx_value=round(av, 2) if not math.isnan(av) else nan,
            adx_rising=ar, adx_pass=ap,
            macd_line=round(mv, 6) if not math.isnan(mv) else nan,
            macd_signal=round(sv, 6) if not math.isnan(sv) else nan,
            macd_cross=mc,
            volume_usd=round(vols[-1], 0),
            volume_sma=round(vsv, 0) if not math.isnan(vsv) else nan,
            volume_pass=vp,
            atr_value=round(av2, 4) if not math.isnan(av2) else nan,
            atr_baseline=round(ab, 4) if not math.isnan(ab) else nan,
            atr_spike_pass=asp,
            spread_pct=spread_pct, spread_pass=sgp, signal=sig,
        )

    def get_stop_and_target(self) -> Tuple[float, float]:
        """Returns (stop_distance_usd, target_distance_usd) based on live ATR."""
        if not self._h1:
            return 672.04, 1008.06  # fallback: validated doc live values
        closes = [float(c["c"]) for c in self._h1]
        highs  = [float(c["h"]) for c in self._h1]
        lows   = [float(c["l"]) for c in self._h1]
        atv = atr(highs, lows, closes, settings.ATR_PERIOD)
        av = last_valid(atv)
        if math.isnan(av) or av <= 0:
            return 672.04, 1008.06
        return av * settings.ATR_MULTIPLIER_STOP, av * settings.ATR_MULTIPLIER_TARGET

    def _empty(self, reason: str, sp: float) -> BTCAuditEntry:
        nan = float("nan")
        return BTCAuditEntry(
            timestamp=reason, close=0.0,
            d1_ema_fast=nan, d1_ema_slow=nan, trend_gate_pass=False,
            adx_value=nan, adx_rising=False, adx_pass=False,
            macd_line=nan, macd_signal=nan, macd_cross=False,
            volume_usd=0.0, volume_sma=nan, volume_pass=False,
            atr_value=nan, atr_baseline=nan, atr_spike_pass=False,
            spread_pct=sp, spread_pass=False, signal="NONE",
        )
