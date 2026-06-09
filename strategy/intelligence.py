"""
Trade Intelligence Engine.

Learns from closed trade history using three frameworks from the uploaded documents:

1. RASA Event Sourcing (RASA_CALM_json.txt):
   Trade history = append-only event log. Fully replayable. Every condition that fired
   at entry time is recorded with the trade outcome. State is always reconstructable.

2. Recursive Amplifier S(x) (THE SYNTHESIS / Divinity 4.1):
   S(x) = x × (1 + ln(viral_coefficient × embedding_depth))
   viral = how often this exact signal configuration led to a winning continuation
   embedding = how deeply this setup type historically restructured price action
   Returns lambda_val (≥1.0) that amplifies the Ω score for setups with proven history.

3. Load-Bearing Analysis (BNDR / InsightAmplifier):
   After N trades, identify which signal conditions are actually load-bearing for win rate.
   "Remove this and [specific consequence] occurs" — proved, not assumed.
   Conditions whose removal causes the biggest win rate drop = load bearers.
   Their weights get elevated in the scoring formula.

4. Decision Autopsy (InsightAmplifier DECISION_AUTOPSY):
   For losing trades: stated rationale (all conditions passed) vs actual driver (what
   actually caused the loss). Suppression mechanisms: spread too wide? regime wrong?
   Bad ATR? This feeds back into countermeasure weighting.

Pattern Convergence Gate (NEGSPACE / BNDR):
   Signal only escalates to STRATEGIC+ tier if ≥3 independent condition families
   converge above SCI threshold. Already partially enforced by strategy conditions,
   but formalized and measured here.

All data is in-memory with SQLite backup via persistence.py path.
"""
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# How many trades before consolidation runs
_CONSOLIDATION_THRESHOLD = 15
# Minimum trades needed to produce a meaningful amplifier
_MIN_TRADES_FOR_LEARNING = 10
# Minimum match rate for "similar setup" lookup
_SIMILARITY_THRESHOLD = 0.70


@dataclass
class TradeEvent:
    """
    Append-only event record. RASA event sourcing model applied to trades.
    Every condition that contributed to the signal decision is logged.
    """
    timestamp:          float
    instrument:         str
    direction:          str    # LONG | SHORT
    signal_conditions:  Dict[str, bool]    # {condition_name: passed}
    spread_pct:         float
    regime_state:       str    # MOMENTUM | MEAN_REVERSION | MIXED
    omega_score:        float
    omega_tier:         str
    outcome:            str    # WIN | STOP | TIMEOUT | FORCED
    realized_pnl:       float
    entry_price:        float
    exit_price:         float
    bars_held:          int


@dataclass
class AutopsyResult:
    """
    Decision Autopsy: stated rationale vs actual driver of the loss.
    From InsightAmplifier DECISION_AUTOPSY schema.
    """
    stated_rationale:   List[str]   # conditions that passed
    actual_driver:      str         # what likely caused the loss
    suppression:        str         # how it was hidden at entry time
    recurrence_risk:    float       # 0-1, how likely same driver activates again


@dataclass
class IntelligenceReport:
    """Output of consolidation — the "dream" summary."""
    instrument:             str
    n_trades_analyzed:      int
    win_rate:               float
    load_bearing_conditions: List[Tuple[str, float]]  # (condition, weight)
    weak_conditions:        List[Tuple[str, float]]   # conditions that don't help
    best_regime:            str    # which regime produced best outcomes
    avg_omega_winners:      float
    avg_omega_losers:       float
    regime_win_rates:       Dict[str, float]
    autopsy_patterns:       List[str]  # recurring actual drivers in losses


class TradeIntelligence:
    """
    One instance per instrument. Learns from closed trades.
    Produces:
    - omega_amplifier(conditions) → float (recursive S(x) amplifier for signal scoring)
    - condition_weight(name) → float (load-bearing analysis weight)
    - regime_gate(regime) → bool (should this regime allow trading?)
    """

    def __init__(self, instrument: str):
        self._instrument     = instrument
        self._events:  List[TradeEvent] = []   # append-only event log
        self._weights: Dict[str, float] = {}   # learned per-condition weights
        self._last_report: Optional[IntelligenceReport] = None
        self._consecutive_losses = 0
        self._last_consolidation_n = 0

    # ── Event recording ───────────────────────────────────────────────────────

    def record_trade(
        self,
        direction:         str,
        signal_conditions: Dict[str, bool],
        spread_pct:        float,
        regime_state:      str,
        omega_score:       float,
        omega_tier:        str,
        outcome:           str,
        realized_pnl:      float,
        entry_price:       float,
        exit_price:        float,
        bars_held:         int,
    ) -> None:
        """
        Record a closed trade as an event (RASA event sourcing).
        This is the only mutation — all analysis is replay of this log.
        """
        event = TradeEvent(
            timestamp=time.time(),
            instrument=self._instrument,
            direction=direction,
            signal_conditions=dict(signal_conditions),
            spread_pct=spread_pct,
            regime_state=regime_state,
            omega_score=omega_score,
            omega_tier=omega_tier,
            outcome=outcome,
            realized_pnl=realized_pnl,
            entry_price=entry_price,
            exit_price=exit_price,
            bars_held=bars_held,
        )
        self._events.append(event)

        if outcome == "STOP":
            self._consecutive_losses += 1
        elif outcome == "WIN":
            self._consecutive_losses = 0

        # Trigger consolidation
        new_trades = len(self._events) - self._last_consolidation_n
        if new_trades >= _CONSOLIDATION_THRESHOLD:
            self._consolidate()
            self._last_consolidation_n = len(self._events)

        logger.debug(
            "INTEL [%s] recorded trade: outcome=%s pnl=%.4f consecutive_losses=%d",
            self._instrument, outcome, realized_pnl, self._consecutive_losses,
        )

    # ── Recursive amplifier (S(x) from Divinity 4.1 / THE SYNTHESIS) ─────────

    def omega_amplifier(self, current_conditions: Dict[str, bool]) -> float:
        """
        S(x) = 1 + ln(viral × embedding)
        viral = how often similar setups led to winning continuation (self-reinforcement)
        embedding = how deeply this setup historically restructured price outcome

        Returns float ≥ 1.0 applied to Ω₀ before classification.
        Returns 1.0 (neutral) until sufficient trade history exists.
        """
        if len(self._events) < _MIN_TRADES_FOR_LEARNING:
            return 1.0

        similar = self._find_similar_setups(current_conditions)
        if len(similar) < 3:
            return 1.0  # insufficient similar history

        wins = [e for e in similar if e.outcome == "WIN"]
        viral = len(wins) / len(similar)  # 0-1: win rate on similar setups

        # Embedding = depth of outcome: how much did winners exceed 1R?
        # Normalize by expected stop distance as proxy for 1R
        win_pnls = [e.realized_pnl for e in wins if e.realized_pnl > 0]
        if win_pnls:
            avg_win = sum(win_pnls) / len(win_pnls)
            # Reference: $0.01 = 1 unit. embedding 10 = avg win 10× reference
            embedding = min(10.0, max(1.0, avg_win / 0.005 + 1))
        else:
            embedding = 1.0

        # Condition weight amplification from load-bearing analysis
        condition_boost = self._condition_weight_product(current_conditions)

        v_norm = viral
        e_norm = embedding / 10.0
        raw = v_norm * e_norm * condition_boost * 10.0
        if raw <= 0:
            return 1.0

        lambda_val = 1.0 + math.log(max(raw, 1.001))
        result = max(1.0, min(2.5, lambda_val))  # cap at 2.5× to prevent runaway

        logger.debug(
            "INTEL [%s] amplifier: viral=%.2f embedding=%.2f cond_boost=%.2f λ=%.3f",
            self._instrument, viral, embedding, condition_boost, result,
        )
        return result

    # ── Condition weight (load-bearing analysis) ──────────────────────────────

    def condition_weight(self, condition_name: str) -> float:
        """
        Returns learned weight for a condition (1.0 = neutral, >1 = load-bearing, <1 = drag).
        From BNDR Load-Bearing Analysis: remove this and [consequence] occurs.
        """
        return self._weights.get(condition_name, 1.0)

    def get_consecutive_losses(self) -> int:
        return self._consecutive_losses

    def get_last_report(self) -> Optional[IntelligenceReport]:
        return self._last_report

    # ── Consolidation (Dream Engine) ──────────────────────────────────────────

    def _consolidate(self) -> None:
        """
        Dream engine consolidation: replay all events, extract patterns.
        Runs after every CONSOLIDATION_THRESHOLD new trades.
        Updates self._weights with load-bearing analysis results.
        """
        n = len(self._events)
        if n < _MIN_TRADES_FOR_LEARNING:
            return

        wins   = [e for e in self._events if e.outcome == "WIN"]
        losses = [e for e in self._events if e.outcome in ("STOP", "FORCED")]
        total  = len(wins) + len(losses)
        if total == 0:
            return

        win_rate = len(wins) / total

        # ── Load-bearing analysis per condition ───────────────────────────────
        all_conditions: set = set()
        for event in self._events:
            all_conditions.update(event.signal_conditions.keys())

        new_weights: Dict[str, float] = {}
        load_bearers: List[Tuple[str, float]] = []
        weak: List[Tuple[str, float]] = []

        for cond in all_conditions:
            present_events = [e for e in self._events if e.signal_conditions.get(cond)]
            absent_events  = [e for e in self._events if not e.signal_conditions.get(cond)]

            if len(present_events) < 3:
                new_weights[cond] = 1.0
                continue

            present_wins = sum(1 for e in present_events if e.outcome == "WIN")
            absent_wins  = sum(1 for e in absent_events  if e.outcome == "WIN")
            present_total = len(present_events)
            absent_total  = len(absent_events)

            present_wr = present_wins / present_total
            absent_wr  = absent_wins  / absent_total if absent_total > 0 else win_rate

            # Weight = ratio of win rate when present vs absent
            # >1.0: condition helps. <1.0: condition absent helped more (drag).
            weight = present_wr / max(absent_wr, 0.10)
            new_weights[cond] = round(weight, 3)

            if weight > 1.2:
                load_bearers.append((cond, weight))
            elif weight < 0.85:
                weak.append((cond, weight))

        self._weights = new_weights
        load_bearers.sort(key=lambda x: x[1], reverse=True)
        weak.sort(key=lambda x: x[1])

        # ── Regime win rate analysis ───────────────────────────────────────────
        regime_stats: Dict[str, List[str]] = {}
        for e in self._events:
            regime_stats.setdefault(e.regime_state, []).append(e.outcome)
        regime_win_rates = {
            regime: outcomes.count("WIN") / max(len(outcomes), 1)
            for regime, outcomes in regime_stats.items()
        }
        best_regime = max(regime_win_rates, key=regime_win_rates.get) if regime_win_rates else "MIXED"

        # ── Omega score analysis ───────────────────────────────────────────────
        avg_omega_winners = (
            sum(e.omega_score for e in wins) / len(wins) if wins else 0.0
        )
        avg_omega_losers = (
            sum(e.omega_score for e in losses) / len(losses) if losses else 0.0
        )

        # ── Decision autopsy — recurring loss patterns ─────────────────────────
        autopsy_patterns: List[str] = []
        # Wide spread losses
        wide_spread_losses = [e for e in losses if e.spread_pct > 0.0003]
        if len(wide_spread_losses) > len(losses) * 0.3:
            autopsy_patterns.append(f"Wide spread (>{0.03:.2f}%) precedes {len(wide_spread_losses)} losses — tighten guard")
        # Wrong regime losses
        momentum_losses_in_mr = [
            e for e in losses
            if e.regime_state == "MEAN_REVERSION"
        ]
        if len(momentum_losses_in_mr) > 2:
            autopsy_patterns.append(f"Regime mismatch: {len(momentum_losses_in_mr)} losses in MR regime — regime gate needed")
        # Low Omega losses
        low_omega_losses = [e for e in losses if e.omega_tier in ("TACTICAL", "OPERATIONAL")]
        if len(low_omega_losses) > len(losses) * 0.4:
            autopsy_patterns.append(f"Low-quality signals losing: {len(low_omega_losses)} TACTICAL/OPERATIONAL losses — raise minimum tier")

        self._last_report = IntelligenceReport(
            instrument=self._instrument,
            n_trades_analyzed=n,
            win_rate=round(win_rate, 4),
            load_bearing_conditions=load_bearers[:5],
            weak_conditions=weak[:3],
            best_regime=best_regime,
            avg_omega_winners=round(avg_omega_winners, 2),
            avg_omega_losers=round(avg_omega_losers, 2),
            regime_win_rates={k: round(v, 4) for k, v in regime_win_rates.items()},
            autopsy_patterns=autopsy_patterns,
        )

        logger.info(
            "INTEL [%s] consolidation: n=%d wr=%.1f%% load_bearers=%s regime=%s",
            self._instrument, n, win_rate * 100,
            [c for c, _ in load_bearers[:3]], best_regime,
        )
        if autopsy_patterns:
            for p in autopsy_patterns:
                logger.warning("INTEL [%s] autopsy: %s", self._instrument, p)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _find_similar_setups(self, current: Dict[str, bool]) -> List[TradeEvent]:
        """Find historical events with ≥ SIMILARITY_THRESHOLD condition overlap."""
        if not current:
            return []
        similar = []
        for event in self._events:
            hist = event.signal_conditions
            if not hist:
                continue
            shared_keys = set(current.keys()) & set(hist.keys())
            if not shared_keys:
                continue
            matches = sum(1 for k in shared_keys if current.get(k) == hist.get(k))
            similarity = matches / len(shared_keys)
            if similarity >= _SIMILARITY_THRESHOLD:
                similar.append(event)
        return similar

    def _condition_weight_product(self, conditions: Dict[str, bool]) -> float:
        """
        Product of learned weights for conditions that fired.
        From load-bearing analysis: load-bearer present → high product.
        Capped to prevent runaway amplification.
        """
        if not self._weights or not conditions:
            return 1.0
        product = 1.0
        for cond, fired in conditions.items():
            if fired:
                w = self._weights.get(cond, 1.0)
                product *= max(0.5, min(2.0, w))
        return max(0.5, min(3.0, product))
