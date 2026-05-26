"""
ETH/USDT H1 Mean Reversion Strategy.
Signals: Bollinger Band false-breakout (prev bar outside, current bar back inside) +
         RSI oversold/overbought + session range filter + volume confirmation.
Supports both LONG (buy dip) and SHORT (sell rip) signals.
"""
import logging, math
from dataclasses import dataclass
from typing import Dict, List, Tuple
from config import settings
from strategy.indicators import atr, bollinger_bands, last_valid, rsi, sma

logger = logging.getLogger(__name__)


@dataclass
class ETHAuditEntry:
    timestamp: str; close: float
    bb_upper: float; bb_lower: float; bb_middle: float
    price_below_lower: bool; price_above_upper: bool
    rsi_value: float; rsi_oversold: bool; rsi_overbought: bool
    in_session_range: bool
    volume_usd: float; volume_sma: float; volume_pass: bool
    atr_value: float; atr_baseline: float; atr_spike_pass: bool
    spread_pct: float; spread_pass: bool; cooldown_pass: bool; signal: str


class ETHMeanReversionStrategy:
    def __init__(self):
        self._h1: List[Dict] = []
        self._bar = 0
        self._last_sig_bar = -999

    def push_h1_candle(self, c: Dict) -> None:
        self._h1.append(c)
        if len(self._h1) > 500: self._h1 = self._h1[-500:]
        self._bar += 1

    def push_d1_candle(self, c: Dict) -> None:
        pass  # ETH MR uses intraday levels only; D1 not required

    def evaluate(self, spread_pct: float = 0.0) -> ETHAuditEntry:
        nan = float("nan")
        n = len(self._h1)
        min_bars = max(settings.MR_BB_PERIOD, settings.MR_RSI_PERIOD + 1,
                       settings.ETH_VOLUME_MA_PERIOD, settings.ATR_BASELINE_PERIOD)
        if n < min_bars:
            return self._empty("INSUFFICIENT_DATA", spread_pct)

        closes = [float(c["c"]) for c in self._h1]
        highs  = [float(c["h"]) for c in self._h1]
        lows   = [float(c["l"]) for c in self._h1]
        vols   = [float(c.get("volume_usd", 0)) for c in self._h1]
        ts = str(self._h1[-1].get("t", "?"))
        price = closes[-1]
        prev  = closes[-2] if n >= 2 else price

        # ── Bollinger Bands ───────────────────────────────────────────────────
        bbu, bbm, bbl = bollinger_bands(closes, settings.MR_BB_PERIOD, settings.MR_BB_STD)
        bu = last_valid(bbu); bm = last_valid(bbm); bl = last_valid(bbl)

        # False breakout: previous bar outside band, current bar back inside
        rev_long  = (not math.isnan(bl)) and prev < bl and price >= bl
        rev_short = (not math.isnan(bu)) and prev > bu and price <= bu

        # Also track raw position for audit
        pbl = price < bl if not math.isnan(bl) else False
        pab = price > bu if not math.isnan(bu) else False

        # ── RSI ───────────────────────────────────────────────────────────────
        rv = last_valid(rsi(closes, settings.MR_RSI_PERIOD))
        ros = (not math.isnan(rv)) and rv <= settings.MR_RSI_OVERSOLD
        rob = (not math.isnan(rv)) and rv >= settings.MR_RSI_OVERBOUGHT

        # ── Session range filter ──────────────────────────────────────────────
        inr = settings.SESSION_RANGE_LOWER <= price <= settings.SESSION_RANGE_UPPER

        # ── Volume confirmation ───────────────────────────────────────────────
        vsma_vals = sma(vols, settings.ETH_VOLUME_MA_PERIOD)
        vsv = last_valid(vsma_vals)
        vp = (not math.isnan(vsv) and vsv > 0 and
              vols[-1] >= vsv * settings.ETH_VOLUME_MULTIPLIER)

        # ── ATR spike filter ─────────────────────────────────────────────────
        atv = atr(highs, lows, closes, settings.ATR_PERIOD)
        av  = last_valid(atv)
        valid_atrs = [v for v in atv if not math.isnan(v)]
        bp = min(settings.ATR_BASELINE_PERIOD, len(valid_atrs))
        ab = sum(valid_atrs[-bp:]) / bp if bp > 0 else nan
        asp = (not math.isnan(av) and not math.isnan(ab) and
               av < ab * settings.ATR_SPIKE_MULTIPLIER)

        # ── Guards ────────────────────────────────────────────────────────────
        sgp = spread_pct < settings.SPREAD_GUARD_THRESHOLD_PCT
        cp  = (self._bar - self._last_sig_bar) >= settings.REENTRY_COOLDOWN_BARS
        guards = asp and sgp and cp

        if   rev_long  and ros and vp and inr and guards: sig = "LONG"
        elif rev_short and rob and vp and inr and guards: sig = "SHORT"
        else:                                              sig = "NONE"

        if sig != "NONE":
            self._last_sig_bar = self._bar
            logger.info("ETH %s signal bar=%d close=%.4f rsi=%.1f",
                        sig, self._bar, price, rv if not math.isnan(rv) else 0)
        else:
            logger.debug("ETH NONE bar=%d rlong=%s rshort=%s ros=%s rob=%s vol=%s guards=%s",
                         self._bar, rev_long, rev_short, ros, rob, vp, guards)

        return ETHAuditEntry(
            timestamp=ts, close=price,
            bb_upper=round(bu, 4) if not math.isnan(bu) else nan,
            bb_lower=round(bl, 4) if not math.isnan(bl) else nan,
            bb_middle=round(bm, 4) if not math.isnan(bm) else nan,
            price_below_lower=pbl, price_above_upper=pab,
            rsi_value=round(rv, 2) if not math.isnan(rv) else nan,
            rsi_oversold=ros, rsi_overbought=rob,
            in_session_range=inr,
            volume_usd=round(vols[-1], 0),
            volume_sma=round(vsv, 0) if not math.isnan(vsv) else nan,
            volume_pass=vp,
            atr_value=round(av, 4) if not math.isnan(av) else nan,
            atr_baseline=round(ab, 4) if not math.isnan(ab) else nan,
            atr_spike_pass=asp,
            spread_pct=spread_pct, spread_pass=sgp,
            cooldown_pass=cp, signal=sig,
        )

    def get_stop_and_target(self) -> Tuple[float, float]:
        """Returns (stop_distance_usd, target_distance_usd) based on live ATR."""
        if not self._h1:
            return 23.01, 34.52  # fallback: validated doc live values
        closes = [float(c["c"]) for c in self._h1]
        highs  = [float(c["h"]) for c in self._h1]
        lows   = [float(c["l"]) for c in self._h1]
        atv = atr(highs, lows, closes, settings.ATR_PERIOD)
        av  = last_valid(atv)
        if math.isnan(av) or av <= 0:
            return 23.01, 34.52
        return av * settings.ATR_MULTIPLIER_STOP, av * settings.ATR_MULTIPLIER_TARGET

    def _empty(self, reason: str, sp: float) -> ETHAuditEntry:
        nan = float("nan")
        return ETHAuditEntry(
            timestamp=reason, close=0.0,
            bb_upper=nan, bb_lower=nan, bb_middle=nan,
            price_below_lower=False, price_above_upper=False,
            rsi_value=nan, rsi_oversold=False, rsi_overbought=False,
            in_session_range=False,
            volume_usd=0.0, volume_sma=nan, volume_pass=False,
            atr_value=nan, atr_baseline=nan, atr_spike_pass=False,
            spread_pct=sp, spread_pass=False, cooldown_pass=False, signal="NONE",
        )
