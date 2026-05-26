"""
Entry point. Startup sequence:
  1. Configure logging
  2. Run backtest gate — halt and exit(2) if any gate fails
  3. Fetch live USDT balance
  4. Start WebSocket feeds
  5. Run instrument engines until shutdown signal

Daily reset: position sizer counters cleared at UTC midnight.
"""
import asyncio
import logging
import os
import sys
import time
from typing import Dict

from broker.rest_client import CryptoComRestClient
from broker.ws_client import CryptoComWSClient
from backtest.engine import run_startup_backtest
from bot_engine import InstrumentEngine
from config import settings
from risk.position_sizer import PositionSizer


def _configure_logging() -> None:
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _run_backtest_gate(rest: CryptoComRestClient) -> None:
    btc_result, eth_result = run_startup_backtest(rest)
    failed = [r for r in (btc_result, eth_result) if not r.gate_pass]
    if failed:
        for r in failed:
            logging.critical(
                "STARTUP GATE FAILED — %s: %s", r.instrument, r.gate_failures
            )
        logging.critical("Bot halted: backtest gates not cleared. Fix strategy or lower MIN_BACKTEST_TRADES.")
        sys.exit(2)
    logging.info("All backtest gates passed — proceeding to live trading.")


async def _daily_reset_loop(sizer: PositionSizer) -> None:
    """Resets daily PnL counters at UTC midnight."""
    while True:
        now  = time.gmtime()
        secs_until_midnight = (23 - now.tm_hour) * 3600 + (59 - now.tm_min) * 60 + (60 - now.tm_sec)
        await asyncio.sleep(secs_until_midnight + 1)
        sizer.reset_daily()


async def main() -> None:
    _configure_logging()
    logger = logging.getLogger("main")
    logger.info("Bot starting. DRY_RUN=%s", settings.DRY_RUN)

    rest  = CryptoComRestClient()

    # ── Startup gate ─────────────────────────────────────────────────────────
    _run_backtest_gate(rest)

    # ── Fetch live balance ────────────────────────────────────────────────────
    if not settings.DRY_RUN:
        try:
            balance = rest.get_usdt_balance()
            if balance <= 0:
                logger.critical("Live balance fetch returned 0 — check API credentials and account")
                sys.exit(1)
            logger.info("Live USDT balance: %.4f", balance)
        except Exception as exc:
            logger.critical("Balance fetch failed: %s — halting", exc)
            sys.exit(1)
    else:
        balance = settings.ACCOUNT_CAPITAL
        logger.info("[DRY_RUN] Using simulated balance: %.2f", balance)

    sizer = PositionSizer()
    sizer.update_balance(balance)

    # ── Create instrument engines ─────────────────────────────────────────────
    btc_engine = InstrumentEngine(
        instrument=settings.BTC_INSTRUMENT,
        strategy_class=settings.BTC_STRATEGY_CLASS,
        rest_client=rest,
        sizer=sizer,
    )
    eth_engine = InstrumentEngine(
        instrument=settings.ETH_INSTRUMENT,
        strategy_class=settings.ETH_STRATEGY_CLASS,
        rest_client=rest,
        sizer=sizer,
    )
    engines: Dict[str, InstrumentEngine] = {
        settings.BTC_INSTRUMENT: btc_engine,
        settings.ETH_INSTRUMENT: eth_engine,
    }

    # ── WebSocket callbacks ───────────────────────────────────────────────────
    async def on_candle(instrument: str, candle: dict) -> None:
        eng = engines.get(instrument)
        if eng: await eng.on_candlestick(instrument, candle)

    async def on_book(instrument: str, book: dict) -> None:
        eng = engines.get(instrument)
        if eng: await eng.on_book(instrument, book)

    async def on_order(order: dict) -> None:
        instrument = order.get("instrument_name", "")
        eng = engines.get(instrument)
        if eng: await eng.on_order_update(order)

    # ── Configure and start WebSocket client ──────────────────────────────────
    ws = CryptoComWSClient(candle_cb=on_candle, book_cb=on_book, order_cb=on_order)
    ws.subscribe_candlestick(settings.BTC_INSTRUMENT, settings.BTC_CANDLE_TF)
    ws.subscribe_candlestick(settings.ETH_INSTRUMENT, settings.ETH_CANDLE_TF)
    ws.subscribe_book(settings.BTC_INSTRUMENT)
    ws.subscribe_book(settings.ETH_INSTRUMENT)

    loop = asyncio.get_event_loop()

    async def run() -> None:
        await ws.start()
        reset_task = loop.create_task(_daily_reset_loop(sizer))
        logger.info("Bot live. Monitoring %s and %s.",
                    settings.BTC_INSTRUMENT, settings.ETH_INSTRUMENT)
        try:
            # Run until cancelled (Railway SIGTERM / KeyboardInterrupt)
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        finally:
            reset_task.cancel()
            await ws.stop()
            logger.info("Bot shutdown complete.")

    try:
        await run()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down")


if __name__ == "__main__":
    asyncio.run(main())
