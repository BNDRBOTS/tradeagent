"""
All bot parameters. Every value is overridable via environment variable.
Loaded once at import time. No mutations after startup.
"""
import os

def _f(key, default): return float(os.environ.get(key, default))
def _i(key, default): return int(os.environ.get(key, default))
def _s(key, default): return str(os.environ.get(key, default))
def _b(key, default): return os.environ.get(key, str(default)).lower() in ("1","true","yes")

# ── Broker ─────────────────────────────────────────────────────────────────────
API_KEY             = _s("CRYPTOCOM_API_KEY", "")
API_SECRET          = _s("CRYPTOCOM_API_SECRET", "")
REST_BASE_URL       = _s("REST_BASE_URL", "https://api.crypto.com/exchange/v1")
WS_MARKET_URL       = _s("WS_MARKET_URL", "wss://stream.crypto.com/exchange/v1/market")
WS_USER_URL         = _s("WS_USER_URL",   "wss://stream.crypto.com/exchange/v1/user")

# ── Instruments ────────────────────────────────────────────────────────────────
BTC_INSTRUMENT      = _s("BTC_INSTRUMENT",   "BTCUSD-PERP")
ETH_INSTRUMENT      = _s("ETH_INSTRUMENT",   "ETHUSD-PERP")
BTC_CANDLE_TF       = _s("BTC_CANDLE_TF",    "1h")
ETH_CANDLE_TF       = _s("ETH_CANDLE_TF",    "1h")
BTC_STRATEGY_CLASS  = _s("BTC_STRATEGY_CLASS","MOMENTUM")
ETH_STRATEGY_CLASS  = _s("ETH_STRATEGY_CLASS","MEAN_REVERSION")
BTC_QTY_TICK        = _f("BTC_QTY_TICK",     0.00001)
ETH_QTY_TICK        = _f("ETH_QTY_TICK",     0.0001)

# ── Operational ────────────────────────────────────────────────────────────────
DRY_RUN             = _b("DRY_RUN",          True)
LOG_LEVEL           = _s("LOG_LEVEL",        "INFO")

# ── Account & risk ─────────────────────────────────────────────────────────────
ACCOUNT_CAPITAL         = _f("ACCOUNT_CAPITAL",         20.0)
RISK_PCT_PER_TRADE      = _f("RISK_PCT_PER_TRADE",       0.02)
MAX_POSITION_PCT        = _f("MAX_POSITION_PCT",         1.00)
MIN_ORDER_NOTIONAL      = _f("MIN_ORDER_NOTIONAL",       10.0)
MIN_ACCOUNT_BALANCE     = _f("MIN_ACCOUNT_BALANCE",      12.0)
DAILY_DRAWDOWN_LIMIT    = _f("DAILY_DRAWDOWN_LIMIT",      0.10)
MAX_TRADES_PER_DAY      = _i("MAX_TRADES_PER_DAY",        3)
REENTRY_COOLDOWN_BARS   = _i("REENTRY_COOLDOWN_BARS",     3)
MAX_HOLD_BARS           = _i("MAX_HOLD_BARS",             48)
ENTRY_FILL_TIMEOUT_BARS = _i("ENTRY_FILL_TIMEOUT_BARS",   2)

# ── Fees & execution ───────────────────────────────────────────────────────────
MAKER_FEE_RATE          = _f("MAKER_FEE_RATE",            0.00075)
TAKER_FEE_RATE          = _f("TAKER_FEE_RATE",            0.00075)
ROUND_TRIP_FEE_RATE     = MAKER_FEE_RATE + TAKER_FEE_RATE
ENTRY_LIMIT_OFFSET_PCT  = _f("ENTRY_LIMIT_OFFSET_PCT",    0.0002)
STOP_LIMIT_OFFSET_PCT   = _f("STOP_LIMIT_OFFSET_PCT",     0.001)
SPREAD_GUARD_THRESHOLD_PCT = _f("SPREAD_GUARD_THRESHOLD_PCT", 0.0005)

# ── ATR ────────────────────────────────────────────────────────────────────────
ATR_PERIOD              = _i("ATR_PERIOD",                14)
ATR_MULTIPLIER_STOP     = _f("ATR_MULTIPLIER_STOP",        2.0)
ATR_MULTIPLIER_TARGET   = _f("ATR_MULTIPLIER_TARGET",      3.0)
ATR_SPIKE_MULTIPLIER    = _f("ATR_SPIKE_MULTIPLIER",       3.0)
ATR_BASELINE_PERIOD     = _i("ATR_BASELINE_PERIOD",        50)
REWARD_TO_RISK_RATIO    = _f("REWARD_TO_RISK_RATIO",       1.5)

# ── BTC momentum ───────────────────────────────────────────────────────────────
BTC_EMA_FAST_STD        = _i("BTC_EMA_FAST_STD",          50)
BTC_EMA_SLOW_STD        = _i("BTC_EMA_SLOW_STD",         200)
BTC_EMA_FAST_REGIME     = _i("BTC_EMA_FAST_REGIME",       20)
BTC_EMA_SLOW_REGIME     = _i("BTC_EMA_SLOW_REGIME",       50)
BTC_REGIME_MODE         = _s("BTC_REGIME_MODE",    "CONSOLIDATION_RECOVERY")
BTC_MIN_EMA_SEP_PCT     = _f("BTC_MIN_EMA_SEP_PCT",       0.005)
ADX_PERIOD              = _i("ADX_PERIOD",                14)
ADX_ENTRY_THRESHOLD     = _f("ADX_ENTRY_THRESHOLD",       25.0)
ADX_EXIT_THRESHOLD      = _f("ADX_EXIT_THRESHOLD",        20.0)
ADX_RISING_LOOKBACK     = _i("ADX_RISING_LOOKBACK",        3)
MACD_FAST               = _i("MACD_FAST",                 12)
MACD_SLOW               = _i("MACD_SLOW",                 26)
MACD_SIGNAL             = _i("MACD_SIGNAL",                9)
BTC_VOLUME_MA_PERIOD    = _i("BTC_VOLUME_MA_PERIOD",      20)
BTC_VOLUME_MULTIPLIER   = _f("BTC_VOLUME_MULTIPLIER",      1.0)

# ── ETH mean reversion ─────────────────────────────────────────────────────────
ETH_VOLUME_MA_PERIOD    = _i("ETH_VOLUME_MA_PERIOD",      20)
ETH_VOLUME_MULTIPLIER   = _f("ETH_VOLUME_MULTIPLIER",      1.0)
MR_RSI_PERIOD           = _i("MR_RSI_PERIOD",             14)
MR_RSI_OVERSOLD         = _f("MR_RSI_OVERSOLD",           35.0)
MR_RSI_OVERBOUGHT       = _f("MR_RSI_OVERBOUGHT",         65.0)
MR_BB_PERIOD            = _i("MR_BB_PERIOD",              20)
MR_BB_STD               = _f("MR_BB_STD",                  2.0)
MR_LOOKBACK_CANDLES     = _i("MR_LOOKBACK_CANDLES",       20)
SESSION_RANGE_LOWER     = _f("SESSION_RANGE_LOWER",      1500.0)
SESSION_RANGE_UPPER     = _f("SESSION_RANGE_UPPER",      5000.0)

# ── Backtest gates ─────────────────────────────────────────────────────────────
MIN_WIN_RATE            = _f("MIN_WIN_RATE",               0.52)
MIN_RR_RATIO            = _f("MIN_RR_RATIO",               1.5)
MIN_BACKTEST_TRADES     = _i("MIN_BACKTEST_TRADES",       100)
MAX_BACKTEST_DRAWDOWN   = _f("MAX_BACKTEST_DRAWDOWN",      0.35)
BACKTEST_DAYS           = _i("BACKTEST_DAYS",              90)

# ── Signal scoring (Divinity Engine Ω) ────────────────────────────────────────
# Minimum Ω tier to allow entry. TACTICAL=0 skips, OPERATIONAL=1 allows at 0.5×
MIN_OMEGA_TIER          = _i("MIN_OMEGA_TIER",          1)   # 0=TACTICAL,1=OPERATIONAL,2=STRATEGIC
# Regime gates: BTC only trades when momentum trust >= this value
BTC_REGIME_TRUST_MIN    = _f("BTC_REGIME_TRUST_MIN",    0.40)
# ETH only trades when momentum trust <= this value
ETH_REGIME_TRUST_MAX    = _f("ETH_REGIME_TRUST_MAX",    0.60)
# Min trades before intelligence amplifier activates
INTEL_MIN_TRADES        = _i("INTEL_MIN_TRADES",        10)
# Consolidation interval (trades between dream-engine runs)
INTEL_CONSOLIDATION_N   = _i("INTEL_CONSOLIDATION_N",  15)
