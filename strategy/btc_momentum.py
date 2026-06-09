"""
BTC/USDT H1 Momentum Strategy.
Entry: D1 EMA trend gate + ADX rising + MACD bullish cross + volume confirmation.
Exit: ATR-based stop and target returned to bot_engine for OCO placement.
FIX: volume field is 'vv' (Crypto.com quote/USD volume), not 'volume_usd'.
"""
import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Tuple

from config import settings
from strategy.indicators import adx, atr, ema, last_valid, macd, sma

logger = logging.getLogger(__name__)


@dataclass
class BTCAuditEntry:
    timestamp:        str
    close:            float
    d1_ema_fast:      float
    d1_ema_slow:      float
    trend_gate_pass:  bool
    adx_value:        float
    adx_rising:       bool
    adx_pass:         bool
    macd_line:        float
    macd_signal_line: float
    macd_cross:       bool
    volume_usd:       float
    volume_sma:       float
    volume_pass:      bool
    atr_value:        float
    atr_baseline:     float
    atr_spike_pass:   bool
    spread_pct:       float
    spread_pass:      bool
    cooldown_pass:    bool
    signal:           str


class BTCMomentumStrategy:
    def __init__(self):
        self._h1:      List[Dict] = []
        self._d1c:     List[float] = []
        self._bar:     int = 0
        self._last_sig: int = -999
        regime = settings.BTC_REGIME_MODE
        if regime == "CONSOLIDATION_RECOVERY":
            self._ef = settings.BTC_EMA_FAST_REGIME
            self._es = settings.BTC_EMA_SLOW_REGIME
        else:
            self._ef = settings.BTC_EMA_FAST_STD
            self._es = settings.BTC_EMA_SLOW_STD
        logger.info("BTC strategy init | regime=%s | D1 EMA %d/%d", regime, self._ef, self._es)

    def push_h1_candle(self, c: Dict) -> None:
        self._h1.append(c)
        if len(self._h1) > 500:
            self._h1 = self._h1[-500:]
        self._bar += 1

    def push_d1_candle(self, c: Dict) -> None:
        try:
            self._d1c.append(float(c["c"]))
        except (KeyError, ValueError):
            pass
        if len(self._d1c) > 300:
            self._d1c = self._d1c[-300:]

    def evaluate(self, spread_pct: float = 0.0) -> BTCAuditEntry:
        nan = float("nan")
        n = len(self._h1)
        req = max(settings.ATR_BASELINE_PERIOD,
                  settings.MACD_SLOW + settings.MACD_SIGNAL,
                  settings.ADX_PERIOD * 2,
                  settings.BTC_VOLUME_MA_PERIOD)
        if n < req:
            return self._empty("INSUFFICIENT_H1_DATA", spread_pct)

        closes = [float(c["c"]) for c in self._h1]
        highs  = [float(c["h"]) for c in self._h1]
        lows   = [float(c["l"]) for c in self._h1]
        # FIX: 'vv' = quote/USD volume on Crypto.com; 'v' = base volume fallback
        vols   = [float(c.get("vv", c.get("v", 0))) for c in self._h1]
        ts     = self._h1[-1].get("t", "?")

        # ── D1 trend gate ─────────────────────────────────────────────────────
        tg = False
        d1f = nan
        d1s = nan
        if len(self._d1c) >= self._es:
            f_series = ema(self._d1c, self._ef)
            s_series = ema(self._d1c, self._es)
            d1f = last_valid(f_series)
            d1s = last_valid(s_series)
            if not math.isnan(d1f) and not math.isnan(d1s) and d1s > 0:
                sep = (d1f - d1s) / d1s
                tg = d1f > d1s and sep >= settings.BTC_MIN_EMA_SEP_PCT

        # ── ADX ───────────────────────────────────────────────────────────────
        adx_series, _, _ = adx(highs, lows, closes, settings.ADX_PERIOD)
        av = last_valid(adx_series)
        lb = settings.ADX_RISING_LOOKBACK
        recent_adx = [v for v in adx_series[-lb - 1:] if not math.isnan(v)]
        ar = len(recent_adx) >= 2 and recent_adx[-1] > recent_adx[0]
        ap = not math.isnan(av) and av >= settings.ADX_ENTRY_THRESHOLD and ar

        # ── MACD bullish crossover ────────────────────────────────────────────
        ml, sl, _ = macd(closes, settings.MACD_FAST, settings.MACD_SLOW, settings.MACD_SIGNAL)
        mv = last_valid(ml)
        sv = last_valid(sl)
        prev_ml = ml[-2] if len(ml) >= 2 else nan
        prev_sl = sl[-2] if len(sl) >= 2 else nan
        mc = (not any(math.isnan(x) for x in [mv, sv, prev_ml, prev_sl])
              and prev_ml <= prev_sl and mv > sv)

        # ── Volume confirmation ───────────────────────────────────────────────
        vsma_series = sma(vols, settings.BTC_VOLUME_MA_PERIOD)
        vsv = last_valid(vsma_series)
        vp = (not math.isnan(vsv) and vsv > 0
              and vols[-1] >= vsv * settings.BTC_VOLUME_MULTIPLIER)

        # ── ATR spike guard ───────────────────────────────────────────────────
        atr_series = atr(highs, lows, closes, settings.ATR_PERIOD)
        atr_val = last_valid(atr_series)
        valid_atrs = [v for v in atr_series if not math.isnan(v)]
        bp = min(settings.ATR_BASELINE_PERIOD, len(valid_atrs))
        atr_base = last_valid(sma(valid_atrs, bp)) if bp > 0 else nan
        asp = (not math.isnan(atr_val) and
               atr_val < atr_base * settings.ATR_SPIKE_MULTIPLIER
               if not math.isnan(atr_base) else not math.isnan(atr_val))

        # ── Spread and cooldown ───────────────────────────────────────────────
        sgp = spread_pct < settings.SPREAD_GUARD_THRESHOLD_PCT
        cp  = (self._bar - self._last_sig) >= settings.REENTRY_COOLDOWN_BARS

        all_pass = tg and ap and mc and vp and asp and sgp and cp
        sig = "LONG" if all_pass else "NONE"
        if sig == "LONG":
            self._last_sig = self._bar
            logger.info("BTC LONG signal | bar=%d close=%.2f adx=%.1f macd=%.4f vol_ratio=%.2f",
                        self._bar, closes[-1], av if not math.isnan(av) else 0,
                        mv if not math.isnan(mv) else 0,
                        vols[-1] / vsv if not math.isnan(vsv) and vsv > 0 else 0)
        else:
            logger.debug("BTC bar=%d NONE | tg=%s adx=%s mc=%s vol=%s asp=%s spread=%s cd=%s",
                         self._bar, tg, ap, mc, vp, asp, sgp, cp)

        return BTCAuditEntry(
            timestamp=str(ts), close=closes[-1],
            d1_ema_fast=round(d1f, 2) if not math.isnan(d1f) else nan,
            d1_ema_slow=round(d1s, 2) if not math.isnan(d1s) else nan,
            trend_gate_pass=tg,
            adx_value=round(av, 2) if not math.isnan(av) else nan,
            adx_rising=ar, adx_pass=ap,
            macd_line=round(mv, 4) if not math.isnan(mv) else nan,
            macd_signal_line=round(sv, 4) if not math.isnan(sv) else nan,
            macd_cross=mc,
            volume_usd=round(vols[-1], 0),
            volume_sma=round(vsv, 0) if not math.isnan(vsv) else nan,
            volume_pass=vp,
            atr_value=round(atr_val, 2) if not math.isnan(atr_val) else nan,
            atr_baseline=round(atr_base, 2) if not math.isnan(atr_base) else nan,
            atr_spike_pass=asp, spread_pct=spread_pct, spread_pass=sgp,
            cooldown_pass=cp, signal=sig,
        )

    def get_stop_and_target(self) -> Tuple[float, float]:
        if len(self._h1) < settings.ATR_PERIOD + 1:
            return 672.04, 1008.06
        closes = [float(c["c"]) for c in self._h1]
        highs  = [float(c["h"]) for c in self._h1]
        lows   = [float(c["l"]) for c in self._h1]
        atr_series = atr(highs, lows, closes, settings.ATR_PERIOD)
        av = last_valid(atr_series)
        if math.isnan(av):
            return 672.04, 1008.06
        return av * settings.ATR_MULTIPLIER_STOP, av * settings.ATR_MULTIPLIER_TARGET

    def _empty(self, reason: str, sp: float) -> BTCAuditEntry:
        nan = float("nan")
        return BTCAuditEntry(
            timestamp=reason, close=0.0,
            d1_ema_fast=nan, d1_ema_slow=nan, trend_gate_pass=False,
            adx_value=nan, adx_rising=False, adx_pass=False,
            macd_line=nan, macd_signal_line=nan, macd_cross=False,
            volume_usd=0.0, volume_sma=nan, volume_pass=False,
            atr_value=nan, atr_baseline=nan, atr_spike_pass=False,
            spread_pct=sp, spread_pass=False, cooldown_pass=False, signal="NONE",
        )
