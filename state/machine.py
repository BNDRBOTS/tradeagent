"""
Position state machine: IDLE → ENTRY_PENDING → POSITION_OPEN → EXIT_PENDING → IDLE.
Every state transition logged with timestamp, price, and reason.
FIX: current_bar exposed as public property (was private _current_bar accessed externally).
"""
import logging, time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional
from config import settings

logger = logging.getLogger(__name__)


class PositionState(Enum):
    IDLE          = "IDLE"
    ENTRY_PENDING = "ENTRY_PENDING"
    POSITION_OPEN = "POSITION_OPEN"
    EXIT_PENDING  = "EXIT_PENDING"


@dataclass
class PositionRecord:
    instrument: str
    direction: str
    entry_order_id: str
    entry_price: float = 0.0
    quantity: float = 0.0
    stop_order_id: str = ""
    target_order_id: str = ""
    oco_list_id: str = ""
    entry_bar: int = 0
    fill_time: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    realized_pnl: float = 0.0
    exit_reason: str = ""


@dataclass
class Transition:
    from_state: str
    to_state: str
    timestamp: float
    price: float
    reason: str
    instrument: str


class PositionStateMachine:
    def __init__(self, instrument: str):
        self._instrument = instrument
        self._state = PositionState.IDLE
        self._position: Optional[PositionRecord] = None
        self._transitions: List[Transition] = []
        self._bar_count: int = 0

    # ── Public interface ─────────────────────────────────────────────────────
    @property
    def state(self) -> PositionState:
        return self._state

    @property
    def position(self) -> Optional[PositionRecord]:
        return self._position

    @property
    def current_bar(self) -> int:
        """FIX: exposes bar count via property instead of private attribute access."""
        return self._bar_count

    def tick_bar(self) -> None:
        self._bar_count += 1

    def is_idle(self) -> bool: return self._state == PositionState.IDLE
    def is_entry_pending(self) -> bool: return self._state == PositionState.ENTRY_PENDING
    def is_open(self) -> bool: return self._state == PositionState.POSITION_OPEN

    # ── State transitions ────────────────────────────────────────────────────
    def _transition(self, new_state: PositionState, price: float, reason: str) -> None:
        t = Transition(
            from_state=self._state.value, to_state=new_state.value,
            timestamp=time.time(), price=price, reason=reason,
            instrument=self._instrument,
        )
        self._transitions.append(t)
        logger.info("[STATE] %s %s → %s @ %.4f | %s",
                    self._instrument, self._state.value, new_state.value, price, reason)
        self._state = new_state

    def on_entry_submitted(self, order_id: str, direction: str, entry_price: float,
                           quantity: float, stop_price: float, target_price: float) -> None:
        if self._state != PositionState.IDLE:
            logger.error("on_entry_submitted called in state %s — ignored", self._state)
            return
        self._position = PositionRecord(
            instrument=self._instrument, direction=direction, entry_order_id=order_id,
            entry_price=entry_price, quantity=quantity,
            stop_price=stop_price, target_price=target_price,
            entry_bar=self._bar_count,
        )
        self._transition(PositionState.ENTRY_PENDING, entry_price, f"order {order_id} submitted")

    def on_entry_filled(self, fill_price: float, fill_qty: float, oco_list_id: str = "") -> None:
        if self._state != PositionState.ENTRY_PENDING:
            logger.warning("on_entry_filled called in state %s — ignored", self._state)
            return
        self._position.entry_price = fill_price
        self._position.quantity    = fill_qty
        self._position.oco_list_id = oco_list_id
        self._position.fill_time   = time.time()
        self._transition(PositionState.POSITION_OPEN, fill_price,
                         f"filled qty={fill_qty:.6f} @ {fill_price:.4f}")

    def on_entry_cancelled(self, reason: str) -> None:
        if self._state not in (PositionState.ENTRY_PENDING,):
            return
        self._position = None
        self._transition(PositionState.IDLE, 0.0, f"entry cancelled: {reason}")

    def on_entry_timeout(self) -> None:
        self.on_entry_cancelled(f"timeout at bar {self._bar_count}")

    def on_exit_submitted(self, stop_id: str, tp_id: str, price: float) -> None:
        if self._state != PositionState.POSITION_OPEN:
            logger.warning("on_exit_submitted called in state %s", self._state)
            return
        if self._position:
            self._position.stop_order_id   = stop_id
            self._position.target_order_id = tp_id
        logger.info("[STATE] %s OCO placed stop=%s tp=%s price=%.4f",
                    self._instrument, stop_id, tp_id, price)

    def on_exit_filled(self, exit_price: float, exit_reason: str) -> Optional[PositionRecord]:
        if self._state not in (PositionState.POSITION_OPEN, PositionState.EXIT_PENDING):
            logger.warning("on_exit_filled called in state %s — ignored", self._state)
            return None
        if not self._position:
            logger.error("on_exit_filled: no position record — state reset to IDLE")
            self._state = PositionState.IDLE
            return None

        pos = self._position
        fee = pos.quantity * pos.entry_price * settings.ROUND_TRIP_FEE_RATE
        if pos.direction == "LONG":
            pos.realized_pnl = (exit_price - pos.entry_price) * pos.quantity - fee
        else:  # SHORT
            pos.realized_pnl = (pos.entry_price - exit_price) * pos.quantity - fee

        pos.exit_reason = exit_reason
        logger.info("[PNL] %s dir=%s entry=%.4f exit=%.4f qty=%.6f pnl=%.6f reason=%s",
                    self._instrument, pos.direction, pos.entry_price, exit_price,
                    pos.quantity, pos.realized_pnl, exit_reason)

        self._position = None
        self._transition(PositionState.IDLE, exit_price, exit_reason)
        return pos

    def check_max_hold(self) -> bool:
        if self._state != PositionState.POSITION_OPEN or not self._position:
            return False
        held = self._bar_count - self._position.entry_bar
        if held >= settings.MAX_HOLD_BARS:
            logger.warning("[STATE] %s max hold %d bars (limit=%d) — force exit triggered",
                           self._instrument, held, settings.MAX_HOLD_BARS)
            return True
        return False

    def last_transitions(self, n: int = 10) -> List[Transition]:
        return self._transitions[-n:]
