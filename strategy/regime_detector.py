"""
Bayesian Market Regime Detector.

Tracks P(market is in momentum regime) per instrument using log-odds Bayesian updating.
Every candle's audit entry provides signals that shift the regime estimate.

States (mapped from BayesianObserver trust levels):
  HOSTILE       < 0.20 → pure mean-reversion regime
  SUSPICIOUS  0.20-0.40 → MR-leaning
  UNCERTAIN   0.40-0.60 → mixed / unknown
  TRUSTING    0.60-0.80 → momentum-leaning
  COMPLIANT     > 0.80 → pure momentum regime

Bot routing:
  BTC momentum strategy should only fire when trust > 0.45
  ETH mean reversion should only fire when trust < 0.55
  In UNCERTAIN band both can operate (strategies self-filter via their own conditions)

Sources: BayesianObserver (Divinity Engine 4.1 and 5.0),
         NEGSPACE pattern convergence (≥3 independent families required for high certainty).
"""
import math
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Signal sensitivity — matches BayesianMind(signal_strength * 2.5) from Divinity 4.1
_SENSITIVITY = 1.8


@dataclass
class RegimeState:
    trust:      float  # P(momentum regime) 0-1
    state:      str    # HOSTILE / SUSPICIOUS / UNCERTAIN / TRUSTING / COMPLIANT
    regime:     str    # MOMENTUM / MEAN_REVERSION / MIXED
    n_updates:  int    = 0

    @classmethod
    def from_trust(cls, trust: float, n: int) -> "RegimeState":
        if   trust > 0.80: state, regime = "COMPLIANT",   "MOMENTUM"
        elif trust > 0.60: state, regime = "TRUSTING",    "MOMENTUM"
        elif trust > 0.40: state, regime = "UNCERTAIN",   "MIXED"
        elif trust > 0.20: state, regime = "SUSPICIOUS",  "MEAN_REVERSION"
        else:              state, regime = "HOSTILE",     "MEAN_REVERSION"
        return cls(trust=round(trust, 4), state=state, regime=regime, n_updates=n)


class RegimeDetector:
    """
    One instance per instrument. Receives audit entries and maintains
    a Bayesian estimate of whether the market is trending (momentum) or
    reverting (mean-reversion).

    Prior: 0.5 (agnostic — unknown regime at startup)
    """

    def __init__(self, instrument: str, initial_trust: float = 0.5):
        self._instrument = instrument
        self._trust      = initial_trust
        self._n_updates  = 0

    def update_from_btc_audit(self, audit) -> RegimeState:
        """Update from BTCAuditEntry. BTC is momentum-oriented."""
        signal = 0.0

        # ADX — primary trend strength indicator
        if not math.isnan(audit.adx_value):
            if audit.adx_value > 30 and audit.adx_rising:
                signal += 1.2   # strong trending market
            elif audit.adx_value > 25 and audit.adx_rising:
                signal += 0.7   # moderate trend
            elif audit.adx_value < 20:
                signal -= 0.6   # weak trend → MR territory

        # MACD cross — momentum confirmation
        if audit.macd_cross:
            signal += 0.4

        # Volume — trend accompanied by volume = real momentum
        if audit.volume_pass:
            signal += 0.3
        else:
            signal -= 0.1  # no volume = suspect

        # D1 trend gate — macro regime confirmation
        if audit.trend_gate_pass:
            signal += 0.5
        else:
            signal -= 0.3

        # ATR spike = volatility regime, not trend regime → reduce confidence
        if not audit.atr_spike_pass:
            signal -= 0.4

        return self._apply(signal)

    def update_from_eth_audit(self, audit) -> RegimeState:
        """Update from ETHAuditEntry. ETH is mean-reversion-oriented."""
        signal = 0.0

        # BB false breakout signals = mean reversion regime (negative for momentum)
        if audit.price_below_lower or audit.price_above_upper:
            signal -= 0.6   # price at extremes = MR territory
        elif not audit.price_below_lower and not audit.price_above_upper:
            signal += 0.2   # price in middle = mild trending

        # RSI extremes = MR conditions
        if audit.rsi_oversold or audit.rsi_overbought:
            signal -= 0.5
        elif 40 < (audit.rsi_value or 50) < 60:
            signal += 0.3   # RSI neutral = mild momentum

        # Volume during reversal = real MR, not just price extreme
        if audit.volume_pass:
            signal -= 0.2   # confirms MR signal

        # ATR spike during ETH MR = volatility, not clean reversal
        if not audit.atr_spike_pass:
            signal -= 0.3

        return self._apply(signal)

    def _apply(self, signal: float) -> RegimeState:
        """Bayesian update via log-odds form."""
        prior_odds      = self._trust / max(1.0 - self._trust, 1e-9)
        likelihood      = math.exp(signal * _SENSITIVITY)
        posterior_odds  = prior_odds * likelihood
        self._trust     = posterior_odds / (1.0 + posterior_odds)
        self._n_updates += 1

        state = RegimeState.from_trust(self._trust, self._n_updates)
        logger.debug(
            "REGIME [%s] signal=%.2f trust=%.3f state=%s regime=%s",
            self._instrument, signal, self._trust, state.state, state.regime,
        )
        return state

    @property
    def state(self) -> RegimeState:
        return RegimeState.from_trust(self._trust, self._n_updates)

    def btc_should_trade(self) -> bool:
        """BTC momentum strategy allowed when market leans momentum."""
        return self._trust >= 0.40

    def eth_should_trade(self) -> bool:
        """ETH mean reversion strategy allowed when market leans MR."""
        return self._trust <= 0.60

    def get_regime_multiplier(self, strategy_class: str) -> float:
        """
        Returns a 0-1 confidence multiplier for the strategy given current regime.
        Used to scale the intelligence amplifier — wrong regime → lower amplifier.
        """
        if strategy_class == "MOMENTUM":
            # Scales 0→0.3 at trust=0.40, 1.0 at trust=0.80+
            return min(1.0, max(0.3, (self._trust - 0.40) / 0.40))
        else:  # MEAN_REVERSION
            # Scales 0→0.3 at trust=0.60, 1.0 at trust=0.20-
            return min(1.0, max(0.3, (0.60 - self._trust) / 0.40))
