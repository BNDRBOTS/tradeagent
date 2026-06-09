"""
Position state machine. 4 states:
  FLAT → ENTRY_PENDING → POSITION_OPEN → EXIT_PENDING → FLAT

Every transition is logged with timestamp, price, and reason.
Stores the full position object so bot_engine can read current stop/target/order IDs.
"""
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

logger = logging.getLogger(__name__)


class State(Enum):
    FLAT          = auto()
    ENTRY_PENDING = auto()
    POSITION_OPEN = auto()
    EXIT_PENDING  = auto()


@dataclass
class Position:
    instrument:      str
    direction:       str        # "LONG" | "SHORT"
    entry_order_id:  str
    entry_price:     float
    quantity:        float
    stop_price:      float
    target_price:    float
    stop_order_id:   str  = ""
    target_order_id: str  = ""
    oco_list_id:     str  = ""
    fill_price:      float = 0.0
    fill_qty:        float = 0.0
    entry_bar:       int   = 0
    realized_pnl:    float = 0.0
    exit_price:      float = 0.0
    exit_reason:     str   = ""
    entry_ts:        float = field(default_factory=time.time)
    exit_ts:         float = 0.0


class PositionStateMachine:
    def __init__(self, instrument: str):
        self._instrument  = instrument
        self._state       = State.FLAT
        self._position:   Optional[Position] = None
        self._current_bar = 0

    # ── Queries ──────────────────────────────────────────────────────────────

    @property
    def state(self) -> State:
        return self._state

    @property
    def position(self) -> Optional[Position]:
        return self._position

    def is_flat(self) -> bool:
        return self._state == State.FLAT

    def is_entry_pending(self) -> bool:
        return self._state == State.ENTRY_PENDING

    def is_open(self) -> bool:
        return self._state in (State.POSITION_OPEN, State.EXIT_PENDING)

    def tick_bar(self) -> None:
        self._current_bar += 1

    def check_max_hold(self) -> bool:
        from config import settings
        if self._position and self._state == State.POSITION_OPEN:
            held = self._current_bar - self._position.entry_bar
            return held >= settings.MAX_HOLD_BARS
        return False

    # ── Transitions ──────────────────────────────────────────────────────────

    def on_entry_submitted(self, order_id: str, direction: str, entry_price: float,
                           quantity: float, stop_price: float, target_price: float) -> None:
        if self._state != State.FLAT:
            logger.error("[SM][%s] on_entry_submitted in state %s — ignored", self._instrument, self._state)
            return
        self._position = Position(
            instrument=self._instrument,
            direction=direction,
            entry_order_id=order_id,
            entry_price=entry_price,
            quantity=quantity,
            stop_price=stop_price,
            target_price=target_price,
            entry_bar=self._current_bar,
        )
        self._state = State.ENTRY_PENDING
        logger.info("[SM][%s] FLAT → ENTRY_PENDING | dir=%s qty=%.6f entry=%.4f stop=%.4f target=%.4f",
                    self._instrument, direction, quantity, entry_price, stop_price, target_price)

    def on_entry_filled(self, fill_price: float, fill_qty: float, oco_list_id: str = "") -> None:
        if self._state != State.ENTRY_PENDING or not self._position:
            logger.error("[SM][%s] on_entry_filled in state %s — ignored", self._instrument, self._state)
            return
        self._position.fill_price  = fill_price
        self._position.fill_qty    = fill_qty
        self._position.oco_list_id = oco_list_id
        self._state = State.POSITION_OPEN
        logger.info("[SM][%s] ENTRY_PENDING → POSITION_OPEN | fill=%.4f qty=%.6f",
                    self._instrument, fill_price, fill_qty)

    def on_exit_submitted(self, stop_id: str, tp_id: str, price: float) -> None:
        if self._state != State.POSITION_OPEN or not self._position:
            logger.error("[SM][%s] on_exit_submitted in state %s — ignored", self._instrument, self._state)
            return
        self._position.stop_order_id   = stop_id
        self._position.target_order_id = tp_id
        self._state = State.EXIT_PENDING
        logger.info("[SM][%s] POSITION_OPEN → EXIT_PENDING | stop_id=%s tp_id=%s at_price=%.4f",
                    self._instrument, stop_id, tp_id, price)

    def on_exit_filled(self, fill_price: float, exit_reason: str) -> Optional[Position]:
        if self._state not in (State.POSITION_OPEN, State.EXIT_PENDING) or not self._position:
            logger.error("[SM][%s] on_exit_filled in state %s — ignored", self._instrument, self._state)
            return None
        pos = self._position
        pos.exit_price  = fill_price
        pos.exit_reason = exit_reason
        pos.exit_ts     = time.time()
        if pos.direction == "LONG":
            gross = (fill_price - pos.fill_price) * pos.fill_qty
        else:
            gross = (pos.fill_price - fill_price) * pos.fill_qty
        from config import settings
        fee = pos.fill_qty * fill_price * settings.ROUND_TRIP_FEE_RATE
        pos.realized_pnl = gross - fee
        closed = self._position
        self._position = None
        self._state    = State.FLAT
        logger.info(
            "[SM][%s] → FLAT | reason=%s fill=%.4f pnl=%.4f (gross=%.4f fee=%.4f)",
            self._instrument, exit_reason, fill_price, pos.realized_pnl, gross, fee,
        )
        return closed

    def on_entry_timeout(self) -> None:
        if self._state != State.ENTRY_PENDING:
            return
        logger.warning("[SM][%s] ENTRY_PENDING → FLAT | entry timeout", self._instrument)
        self._position = None
        self._state    = State.FLAT

    def on_entry_cancelled(self, reason: str = "") -> None:
        if self._state not in (State.ENTRY_PENDING, State.FLAT):
            return
        logger.warning("[SM][%s] → FLAT | entry cancelled: %s", self._instrument, reason)
        self._position = None
        self._state    = State.FLAT
