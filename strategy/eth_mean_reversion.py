"""
ETH/USDT H1 Mean Reversion Strategy.
Entry: Bollinger Band false-breakout reversal + RSI confirmation + volume.
Both LONG (oversold reversal) and SHORT (overbought reversal) signals.
FIX: volume field 'vv'. Session range filter conditional (disabled if UPPER=0).
"""
import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Tuple

from config import settings
from strategy.indicators import atr, bollinger_bands, last_valid, rsi, sma

logger = logging.getLogger(__name__)


@dataclass
class ETHAuditEntry:
    timestamp:        str
    close:            float
    bb_upper:         float
    bb_lower:         float
    bb_middle:        float
    price_below_lower: bool
    price_above_upper: bool
    rsi_value:        float
    rsi_oversold:     bool
    rsi_overbought:   bool
    in_session_range: bool
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


class ETHMeanReversionStrategy:
    def __init__(self):
        self._h1:      List[Dict] = []
        self._bar:     int = 0
        self._last_sig: int = -999
        logger.info("ETH mean-reversion strategy init | BB(%d,%.1f) RSI(%d) OB=%.0f OS=%.0f",
                    settings.MR_BB_PERIOD, settings.MR_BB_STD,
                    settings.MR_RSI_PERIOD, settings.MR_RSI_OVERBOUGHT, settings.MR_RSI_OVERSOLD)

    def push_h1_candle(self, c: Dict) -> None:
        self._h1.append(c)
        if len(self._h1) > 500:
            self._h1 = self._h1[-500:]
        self._bar += 1

    def evaluate(self, spread_pct: float = 0.0) -> ETHAuditEntry:
        nan = float("nan")
        n = len(self._h1)
        req = max(settings.MR_BB_PERIOD, settings.MR_RSI_PERIOD + 1,
                  settings.ETH_VOLUME_MA_PERIOD, settings.ATR_BASELINE_PERIOD)
        if n < req:
            return self._empty("INSUFFICIENT_H1_DATA", spread_pct)

        closes = [float(c["c"]) for c in self._h1]
        highs  = [float(c["h"]) for c in self._h1]
        lows   = [float(c["l"]) for c in self._h1]
        # FIX: 'vv' = quote/USD volume
        vols   = [float(c.get("vv", c.get("v", 0))) for c in self._h1]
        ts     = self._h1[-1].get("t", "?")
        price  = closes[-1]
        prev   = closes[-2] if n >= 2 else price

        # ── Bollinger Bands false-breakout reversal ───────────────────────────
        bbu, bbm, bbl = bollinger_bands(closes, settings.MR_BB_PERIOD, settings.MR_BB_STD)
        bu = last_valid(bbu)
        bm = last_valid(bbm)
        bl = last_valid(bbl)
        # False breakout: prior candle was outside band, current is back inside
        prev_below_lower = (not math.isnan(bl)) and prev < bl
        prev_above_upper = (not math.isnan(bu)) and prev > bu
        rev_long  = prev_below_lower and price >= bl
        rev_short = prev_above_upper and price <= bu

        # ── RSI ───────────────────────────────────────────────────────────────
        rsi_series = rsi(closes, settings.MR_RSI_PERIOD)
        rv = last_valid(rsi_series)
        ros = not math.isnan(rv) and rv <= settings.MR_RSI_OVERSOLD
        rob = not math.isnan(rv) and rv >= settings.MR_RSI_OVERBOUGHT

        # ── Session range (FIX: conditional — disabled if UPPER=0) ───────────
        range_active = (settings.SESSION_RANGE_UPPER > 0 and
                        settings.SESSION_RANGE_UPPER > settings.SESSION_RANGE_LOWER)
        inr = (settings.SESSION_RANGE_LOWER <= price <= settings.SESSION_RANGE_UPPER
               if range_active else True)

        # ── Volume confirmation ───────────────────────────────────────────────
        vsma_series = sma(vols, settings.ETH_VOLUME_MA_PERIOD)
        vsv = last_valid(vsma_series)
        vp = (not math.isnan(vsv) and vsv > 0
              and vols[-1] >= vsv * settings.ETH_VOLUME_MULTIPLIER)

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

        guards = asp and sgp and cp

        if   rev_long  and ros and vp and inr and guards:
            sig = "LONG";  self._last_sig = self._bar
        elif rev_short and rob and vp and inr and guards:
            sig = "SHORT"; self._last_sig = self._bar
        else:
            sig = "NONE"

        if sig != "NONE":
            logger.info("ETH %s signal | bar=%d close=%.4f rsi=%.1f vol_ratio=%.2f",
                        sig, self._bar, price,
                        rv if not math.isnan(rv) else 0.0,
                        vols[-1] / vsv if not math.isnan(vsv) and vsv > 0 else 0.0)
        else:
            logger.debug("ETH bar=%d NONE | rlong=%s rshort=%s ros=%s rob=%s vol=%s inr=%s guards=%s",
                         self._bar, rev_long, rev_short, ros, rob, vp, inr, guards)

        return ETHAuditEntry(
            timestamp=str(ts), close=price,
            bb_upper=round(bu, 4) if not math.isnan(bu) else nan,
            bb_lower=round(bl, 4) if not math.isnan(bl) else nan,
            bb_middle=round(bm, 4) if not math.isnan(bm) else nan,
            price_below_lower=price < bl if not math.isnan(bl) else False,
            price_above_upper=price > bu if not math.isnan(bu) else False,
            rsi_value=round(rv, 2) if not math.isnan(rv) else nan,
            rsi_oversold=ros, rsi_overbought=rob, in_session_range=inr,
            volume_usd=round(vols[-1], 0),
            volume_sma=round(vsv, 0) if not math.isnan(vsv) else nan,
            volume_pass=vp,
            atr_value=round(atr_val, 4) if not math.isnan(atr_val) else nan,
            atr_baseline=round(atr_base, 4) if not math.isnan(atr_base) else nan,
            atr_spike_pass=asp, spread_pct=spread_pct, spread_pass=sgp,
            cooldown_pass=cp, signal=sig,
        )

    def get_stop_and_target(self) -> Tuple[float, float]:
        if len(self._h1) < settings.ATR_PERIOD + 1:
            return 23.01, 34.52
        closes = [float(c["c"]) for c in self._h1]
        highs  = [float(c["h"]) for c in self._h1]
        lows   = [float(c["l"]) for c in self._h1]
        atr_series = atr(highs, lows, closes, settings.ATR_PERIOD)
        av = last_valid(atr_series)
        if math.isnan(av):
            return 23.01, 34.52
        return av * settings.ATR_MULTIPLIER_STOP, av * settings.ATR_MULTIPLIER_TARGET

    def _empty(self, reason: str, sp: float) -> ETHAuditEntry:
        nan = float("nan")
        return ETHAuditEntry(
            timestamp=reason, close=0.0,
            bb_upper=nan, bb_lower=nan, bb_middle=nan,
            price_below_lower=False, price_above_upper=False,
            rsi_value=nan, rsi_oversold=False, rsi_overbought=False,
            in_session_range=False, volume_usd=0.0, volume_sma=nan,
            volume_pass=False, atr_value=nan, atr_baseline=nan,
            atr_spike_pass=False, spread_pct=sp, spread_pass=False,
            cooldown_pass=False, signal="NONE",
        )
