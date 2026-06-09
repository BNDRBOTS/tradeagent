"""
Signal Quality Scorer — Divinity Engine 5.0 Ω formula applied to trading signals.

Every signal produces an Ω score instead of binary LONG/SHORT/NONE.
Ω determines position size tier:
  TACTICAL   (log10(Ω) < 3):  skip — signal not worth the fee
  OPERATIONAL(log10(Ω) < 5):  0.5× normal size
  STRATEGIC  (log10(Ω) < 7):  1.0× normal size
  APOTHEOSIS (log10(Ω) >= 7): 1.3× normal size (hard-capped)

Factor mapping from Divinity 5.0 to trading context:
  risk     → inverse volatility risk (low ATR spike = high score)
  ease     → execution quality (tight spread = high score)
  impact   → expected RR potential (ATR-based stop/target ratio)
  stealth  → signal condition density (n conditions fired / n total)
  resource → available capital as fraction of initial
  friction → current spread vs guard threshold
  delay    → bars held since cooldown (lower = fresher opportunity)

Adversarial delta (Δ): market resistance discounts the score.
  - Volatility spike above 2× baseline
  - Consecutive losing trades
  - Session drawdown approaching daily limit
  - Distance from circuit breaker thresholds

Sources: Divinity Engine 5.0 (APOTHEOSIS), Divinity Engine 4.1 (Tesseract Protocol),
         BNDR Load-Bearing Analysis (which factors actually carry the edge).
"""
import math
import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Dict, List, Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)

# ── Physics constants (from Divinity Engine 5.0) ──────────────────────────────
_ALPHA:     float = 2.0   # Risk exponent
_BETA:      float = 1.0   # Ease exponent
_GAMMA:     float = 3.0   # Impact exponent
_DELTA_EXP: float = 2.0   # Stealth exponent
_EPSILON:   float = 0.5   # Friction exponent
_ZETA:      float = 1.0   # Delay exponent
_ETA:       float = 2.5   # Countermeasure penalty exponent
_SAFE_MIN:  float = 0.001


class OmegaTier(IntEnum):
    TACTICAL    = 0   # log10(Ω) < 3  → skip
    OPERATIONAL = 1   # log10(Ω) < 5  → 0.5× size
    STRATEGIC   = 2   # log10(Ω) < 7  → 1.0× size
    APOTHEOSIS  = 3   # log10(Ω) >= 7 → 1.3× size

    @property
    def size_multiplier(self) -> float:
        return {0: 0.0, 1: 0.5, 2: 1.0, 3: 1.3}[self.value]

    @property
    def label(self) -> str:
        return {0: "TACTICAL", 1: "OPERATIONAL", 2: "STRATEGIC", 3: "APOTHEOSIS"}[self.value]


@dataclass
class ScoreResult:
    omega_0:          float   # base kinetic score before amplification
    omega_final:      float   # after adversarial delta
    tier:             OmegaTier
    size_multiplier:  float   # 0.0 / 0.5 / 1.0 / 1.3
    factors:          Dict[str, float]   # individual factor values
    delta:            float   # adversarial resistance discount
    countermeasures:  List[Tuple[str, float]]  # (name, resistance)
    skip:             bool


def _clamp(v: float, lo: float = 1.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, v))


def _normalize_spread(spread_pct: float) -> float:
    """Convert spread % to 1-10 ease score. Tight spread = 10."""
    ratio = spread_pct / max(settings.SPREAD_GUARD_THRESHOLD_PCT, 1e-8)
    return _clamp(10.0 - ratio * 9.0)


def _normalize_rr(stop_d: float, target_d: float) -> float:
    """Convert stop/target distances to 1-10 impact score."""
    if stop_d <= 0:
        return 1.0
    rr = target_d / stop_d
    return _clamp(rr * 2.0)


def _compute_adversarial_delta(
    atr_val: float,
    atr_baseline: float,
    consecutive_losses: int,
    daily_pnl: float,
    balance: float,
) -> Tuple[float, List[Tuple[str, float]]]:
    """
    Δ = exp(-penalty) where penalty = (total_resistance^ETA) / 100.
    Each adverse market condition is a countermeasure with probability and severity.
    Returns (delta, countermeasure_list).
    """
    countermeasures: List[Tuple[str, float]] = []

    # Volatility spike (ATR > 2× baseline)
    if not math.isnan(atr_val) and not math.isnan(atr_baseline) and atr_baseline > 0:
        vr = atr_val / atr_baseline
        if vr > 2.0:
            prob = min(1.0, (vr - 1.0) / 3.0)
            sev  = min(10.0, vr * 3.0)
            countermeasures.append(("volatility_spike", prob * sev))

    # Consecutive losses — signal regime may have changed
    if consecutive_losses >= 2:
        prob = min(1.0, consecutive_losses / 4.0)
        countermeasures.append(("consecutive_losses", prob * 6.0))

    # Session drawdown eating into daily limit
    if daily_pnl < 0 and balance > 0:
        dd_pct = abs(daily_pnl) / max(balance, settings.MIN_ACCOUNT_BALANCE)
        if dd_pct > settings.DAILY_DRAWDOWN_LIMIT * 0.5:
            prob = dd_pct / settings.DAILY_DRAWDOWN_LIMIT
            countermeasures.append(("session_drawdown", min(1.0, prob) * 7.0))

    if not countermeasures:
        return 1.0, []

    total_res = sum(r for _, r in countermeasures)
    penalty   = (total_res ** _ETA) / 100.0
    delta     = math.exp(-penalty)
    return delta, countermeasures


def score_signal(
    # Conditions from signal audit
    trend_gate_pass:   bool,
    adx_pass:          bool,
    adx_value:         float,
    macd_cross:        bool,
    volume_pass:       bool,
    atr_spike_pass:    bool,
    spread_pass:       bool,
    cooldown_pass:     bool,
    # Execution context
    spread_pct:        float,
    stop_distance:     float,
    target_distance:   float,
    balance:           float,
    # Market resistance context
    atr_val:           float,
    atr_baseline:      float,
    consecutive_losses: int,
    daily_pnl:         float,
    # Intelligence amplifier (from TradeIntelligence.omega_amplifier, default 1.0)
    intelligence_amplifier: float = 1.0,
) -> ScoreResult:
    """
    Compute Ω score for a signal. Returns ScoreResult with tier and size multiplier.
    Called by bot_engine after signal evaluation — before position sizing.
    """

    # ── Factor computation ────────────────────────────────────────────────────

    # Conditions that fired (stealth = signal density)
    conditions = [trend_gate_pass, adx_pass, macd_cross, volume_pass,
                  atr_spike_pass, spread_pass, cooldown_pass]
    n_passed   = sum(1 for c in conditions if c)
    n_total    = len(conditions)
    stealth    = _clamp((n_passed / n_total) * 10.0)

    # Risk: how clean is the setup? Low volatility = low risk = high score
    risk_base = 10.0 if atr_spike_pass else 3.0
    if not math.isnan(adx_value) and adx_value > 0:
        # High ADX = strong trend = signal is less risky (trending, not choppy)
        risk_adj = min(5.0, adx_value / 5.0)
        risk = _clamp(risk_base * 0.6 + risk_adj * 0.4)
    else:
        risk = _clamp(risk_base)

    # Ease: execution quality (spread tightness)
    ease = _normalize_spread(spread_pct)

    # Impact: expected profit potential relative to risk
    impact = _normalize_rr(stop_distance, target_distance)

    # Resource: account health
    resource = _clamp((balance / max(settings.ACCOUNT_CAPITAL, 1.0)) * 10.0)

    # Friction: spread cost relative to guard threshold
    friction = _clamp(
        (spread_pct / max(settings.SPREAD_GUARD_THRESHOLD_PCT, 1e-8)) * 10.0
    )

    # Delay: freshness — always low since we evaluate at candle close
    # Use consecutive_losses as proxy: high consecutive losses = stale/bad conditions
    delay = _clamp(1.0 + consecutive_losses * 1.5)

    factors = {
        "risk": round(risk, 3), "ease": round(ease, 3),
        "impact": round(impact, 3), "stealth": round(stealth, 3),
        "resource": round(resource, 3), "friction": round(friction, 3),
        "delay": round(delay, 3),
    }

    # ── Ω₀ base score ─────────────────────────────────────────────────────────
    f = max(friction, _SAFE_MIN)
    d = max(delay,    _SAFE_MIN)

    numerator = (
        (risk    ** _ALPHA)   *
        (ease    ** _BETA)    *
        (impact  ** _GAMMA)   *
        (stealth ** _DELTA_EXP) *
        resource
    )
    denominator = (f ** _EPSILON) * (d ** _ZETA)
    omega_0 = numerator / denominator

    # ── Adversarial delta ─────────────────────────────────────────────────────
    delta, cms = _compute_adversarial_delta(
        atr_val, atr_baseline, consecutive_losses, daily_pnl, balance
    )

    # ── Intelligence amplifier (from recursive S(x) iterator) ────────────────
    # intelligence_amplifier comes from TradeIntelligence.omega_amplifier()
    # It's 1.0 until we have enough trade history to learn from
    omega_final = omega_0 * delta * intelligence_amplifier

    # ── Classification ────────────────────────────────────────────────────────
    if omega_final <= 0:
        tier = OmegaTier.TACTICAL
    else:
        mag = math.log10(omega_final)
        if   mag < 3.0: tier = OmegaTier.TACTICAL
        elif mag < 5.0: tier = OmegaTier.OPERATIONAL
        elif mag < 7.0: tier = OmegaTier.STRATEGIC
        else:           tier = OmegaTier.APOTHEOSIS

    logger.debug(
        "Ω score: Ω₀=%.2f Δ=%.3f λ_intel=%.3f Ω_final=%.2f tier=%s",
        omega_0, delta, intelligence_amplifier, omega_final, tier.label,
    )

    return ScoreResult(
        omega_0=round(omega_0, 2),
        omega_final=round(omega_final, 2),
        tier=tier,
        size_multiplier=tier.size_multiplier,
        factors=factors,
        delta=round(delta, 4),
        countermeasures=cms,
        skip=tier == OmegaTier.TACTICAL,
    )
