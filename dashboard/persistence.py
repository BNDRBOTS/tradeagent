"""
Async SQLite trade persistence.
Path priority:
  1. RAILWAY_VOLUME_MOUNT_PATH env var (Railway persistent Volume)
  2. ./trades.db (local / fallback — resets on Railway redeploy without Volume)

On startup, logs which path is in use so the operator knows whether
trade history survives restarts.
"""
import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)

_vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
DB_PATH = os.path.join(_vol, "trades.db") if _vol else "trades.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument      TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    fill_price      REAL    NOT NULL,
    exit_price      REAL    NOT NULL,
    quantity        REAL    NOT NULL,
    stop_price      REAL    NOT NULL,
    target_price    REAL    NOT NULL,
    exit_reason     TEXT    NOT NULL,
    realized_pnl    REAL    NOT NULL,
    entry_ts        REAL    NOT NULL,
    exit_ts         REAL    NOT NULL,
    duration_minutes REAL   NOT NULL
)
"""

_db_lock = asyncio.Lock()
_initialized = False


async def init_db() -> None:
    global _initialized
    if _initialized:
        return
    logger.info("Trade DB path: %s%s", DB_PATH,
                " (Railway Volume — persistent)" if _vol else " (local — resets on redeploy)")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_CREATE_TABLE)
        await db.commit()
    _initialized = True
    logger.info("Trade DB initialized")


async def insert_trade(instrument: str, direction: str, fill_price: float,
                       exit_price: float, quantity: float, stop_price: float,
                       target_price: float, exit_reason: str, realized_pnl: float,
                       entry_ts: float, exit_ts: float) -> int:
    dur = round((exit_ts - entry_ts) / 60.0, 1) if exit_ts > entry_ts else 0.0
    async with _db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """INSERT INTO trades
                   (instrument, direction, fill_price, exit_price, quantity,
                    stop_price, target_price, exit_reason, realized_pnl,
                    entry_ts, exit_ts, duration_minutes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (instrument, direction, fill_price, exit_price, quantity,
                 stop_price, target_price, exit_reason, realized_pnl,
                 entry_ts, exit_ts, dur),
            )
            await db.commit()
            return cur.lastrowid


async def fetch_trades(limit: int = 50) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def fetch_daily_summary() -> Dict[str, Any]:
    """Realized PnL and trade count for today (UTC)."""
    today_start = float(int(time.time() // 86400) * 86400)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """SELECT COUNT(*) as n, COALESCE(SUM(realized_pnl),0) as pnl
               FROM trades WHERE exit_ts >= ?""",
            (today_start,),
        )
        row = await cur.fetchone()
        return {"today_trades": row[0], "today_pnl": round(row[1], 6)}
