"""
All configuration. Every parameter loaded from environment with documented defaults.
Corrected values from Validated Intelligence Document (May 2026).
"""
import os

def _env(k, default): return os.environ.get(k, str(default))
def _env_float(k, default): return float(os.environ.get(k, default))
def _env_int(k, default): return int(os.environ.get(k, default))
def _env_bool(k, default): return os.environ.get(k, str(default)).lower() in ('1','true','yes')

# ── Broker ───────────────────────────────────────────────────────────────────
REST_BASE_URL       = _env("REST_BASE_URL",    "https://api.crypto.com/v1")
WS_MARKET_URL       = _env("WS_MARKET_URL",    "wss://stream.crypto.com/v1/market")
WS_USER_URL         = _env("WS_USER_URL",      "wss://stream.crypto.com/v1/user")
API_KEY             = _env("API_KEY",          "")
API_SECRET          = _env("API_SECRET",       "")

# ── Instruments ───────────────────────────────────────────────────────────────
BTC_INSTRUMENT      = _env("BTC_INSTRUMENT",   "BTCUSDT")
ETH_INSTRUMENT      = _env("ETH_INSTRUMENT",   "ETHUSDT")
# Minimum quantity increments (Crypto.com verified)
BTC_QTY_TICK        = _env_float("BTC_QTY_TICK", 0.00001)
ETH_QTY_TICK        = _env_float("ETH_QTY_TICK", 0.001)

# ── Fees (Crypto.com spot, maker/taker, corrected from Validated Doc) ─────────
MAKER_FEE_RATE      = _env_float("MAKER_FEE_RATE",  0.00040)   # 0.040%
TAKER_FEE_RATE      = _env_float("TAKER_FEE_RATE",  0.00075)   # 0.075%
ROUND_TRIP_FEE_RATE = MAKER_FEE_RATE + TAKER_FEE_RATE          # 0.115% round trip

# ── Capital & Risk ────────────────────────────────────────────────────────────
ACCOUNT_CAPITAL     = _env_float("ACCOUNT_CAPITAL",      20.0)
RISK_PCT_PER_TRADE  = _env_float("RISK_PCT_PER_TRADE",   0.01)  # 1% per trade
MAX_POSITION_PCT    = _env_float("MAX_POSITION_PCT",     0.95)  # 95% max notional
MIN_ORDER_NOTIONAL  = _env_float("MIN_ORDER_NOTIONAL",   5.0)   # Crypto.com minimum
REWARD_TO_RISK_RATIO = _env_float("REWARD_TO_RISK_RATIO", 1.5)

# ── Circuit Breakers ──────────────────────────────────────────────────────────
DAILY_DRAWDOWN_LIMIT = _env_float("DAILY_DRAWDOWN_LIMIT",  0.10)   # halt if > 10% daily loss
MIN_ACCOUNT_BALANCE  = _env_float("MIN_ACCOUNT_BALANCE",  12.0)    # halt if balance below $12
MAX_TRADES_PER_DAY   = _env_int("MAX_TRADES_PER_DAY",       3)     # max 3 entries/day combined

# ── ATR Parameters ────────────────────────────────────────────────────────────
ATR_PERIOD              = _env_int("ATR_PERIOD",           14)
ATR_MULTIPLIER_STOP     = _env_float("ATR_MULTIPLIER_STOP",  2.0)   # stop = entry ± 2×ATR
ATR_MULTIPLIER_TARGET   = _env_float("ATR_MULTIPLIER_TARGET", 3.0)  # target = entry ± 3×ATR → RR=1.5
ATR_SPIKE_MULTIPLIER    = _env_float("ATR_SPIKE_MULTIPLIER",  2.5)  # reject if ATR > 2.5× baseline
ATR_BASELINE_PERIOD     = _env_int("ATR_BASELINE_PERIOD",   50)

# ── Order Execution ───────────────────────────────────────────────────────────
ENTRY_LIMIT_OFFSET_PCT  = _env_float("ENTRY_LIMIT_OFFSET_PCT",  0.0005)  # 0.05% above ask / below bid
ENTRY_FILL_TIMEOUT_BARS = _env_int("ENTRY_FILL_TIMEOUT_BARS",  3)
STOP_LIMIT_OFFSET_PCT   = _env_float("STOP_LIMIT_OFFSET_PCT",  0.001)   # 0.10% slippage buffer on stop leg
REENTRY_COOLDOWN_BARS   = _env_int("REENTRY_COOLDOWN_BARS",    3)
MAX_HOLD_BARS           = _env_int("MAX_HOLD_BARS",            48)       # 48 H1 bars = 2 days

# ── Spread Guard (Validated Doc: live spread 0.0013%; threshold = 4× normal) ──
SPREAD_GUARD_THRESHOLD_PCT = _env_float("SPREAD_GUARD_THRESHOLD_PCT", 0.0005)  # 0.05%

# ── BTC Momentum Strategy ─────────────────────────────────────────────────────
BTC_STRATEGY_CLASS      = _env("BTC_STRATEGY_CLASS",    "MOMENTUM")
BTC_CANDLE_TF           = _env("BTC_CANDLE_TF",         "1h")
BTC_REGIME_MODE         = _env("BTC_REGIME_MODE",       "CONSOLIDATION_RECOVERY")
# Regime-adapted EMAs (20/50 instead of 50/200 per Validated Doc correction)
BTC_EMA_FAST_REGIME     = _env_int("BTC_EMA_FAST_REGIME",  20)
BTC_EMA_SLOW_REGIME     = _env_int("BTC_EMA_SLOW_REGIME",  50)
BTC_EMA_FAST_STD        = _env_int("BTC_EMA_FAST_STD",     50)
BTC_EMA_SLOW_STD        = _env_int("BTC_EMA_SLOW_STD",    200)
BTC_MIN_EMA_SEP_PCT     = _env_float("BTC_MIN_EMA_SEP_PCT", 0.001)  # 0.1% min separation
ADX_PERIOD              = _env_int("ADX_PERIOD",         14)
ADX_ENTRY_THRESHOLD     = _env_float("ADX_ENTRY_THRESHOLD", 25.0)
ADX_EXIT_THRESHOLD      = _env_float("ADX_EXIT_THRESHOLD",  20.0)
ADX_RISING_LOOKBACK     = _env_int("ADX_RISING_LOOKBACK",   3)
MACD_FAST               = _env_int("MACD_FAST",          12)
MACD_SLOW               = _env_int("MACD_SLOW",          26)
MACD_SIGNAL             = _env_int("MACD_SIGNAL",         9)
BTC_VOLUME_MA_PERIOD    = _env_int("BTC_VOLUME_MA_PERIOD", 20)
BTC_VOLUME_MULTIPLIER   = _env_float("BTC_VOLUME_MULTIPLIER", 1.0)  # CORRECTED: was 1.2

# ── ETH Mean Reversion Strategy ───────────────────────────────────────────────
ETH_STRATEGY_CLASS      = _env("ETH_STRATEGY_CLASS",    "MEAN_REVERSION")
ETH_CANDLE_TF           = _env("ETH_CANDLE_TF",         "1h")
MR_BB_PERIOD            = _env_int("MR_BB_PERIOD",       20)
MR_BB_STD               = _env_float("MR_BB_STD",         2.0)
MR_RSI_PERIOD           = _env_int("MR_RSI_PERIOD",      14)
MR_RSI_OVERSOLD         = _env_float("MR_RSI_OVERSOLD",  30.0)
MR_RSI_OVERBOUGHT       = _env_float("MR_RSI_OVERBOUGHT", 70.0)
MR_LOOKBACK_CANDLES     = _env_int("MR_LOOKBACK_CANDLES", 2)
SESSION_RANGE_UPPER     = _env_float("SESSION_RANGE_UPPER", 2200.0)  # CORRECTED live value
SESSION_RANGE_LOWER     = _env_float("SESSION_RANGE_LOWER", 1900.0)  # CORRECTED live value
ETH_VOLUME_MA_PERIOD    = _env_int("ETH_VOLUME_MA_PERIOD",  20)
ETH_VOLUME_MULTIPLIER   = _env_float("ETH_VOLUME_MULTIPLIER", 1.0)   # CORRECTED: was 1.2

# ── Backtest Gates ────────────────────────────────────────────────────────────
MIN_WIN_RATE            = _env_float("MIN_WIN_RATE",       0.52)
MIN_RR_RATIO            = _env_float("MIN_RR_RATIO",       1.5)
# FIX: default 5 (viable with Crypto.com 50-bar API max). Set MIN_BACKTEST_TRADES=100
# via env var in production with paginated historical data.
MIN_BACKTEST_TRADES     = _env_int("MIN_BACKTEST_TRADES",    5)
MAX_BACKTEST_DRAWDOWN   = _env_float("MAX_BACKTEST_DRAWDOWN", 0.35)
BACKTEST_DAYS           = _env_int("BACKTEST_DAYS",          90)

# ── Runtime ───────────────────────────────────────────────────────────────────
DRY_RUN                 = _env_bool("DRY_RUN",  True)
LOG_LEVEL               = _env("LOG_LEVEL",    "INFO")
