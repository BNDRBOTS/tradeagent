"""
Technical indicators. Pure Python + math. Zero external dependencies.
All functions return lists of float, NaN for bars before warmup period.
"""
import math
from typing import List

def _nan(): return float("nan")

def ema(values: List[float], period: int) -> List[float]:
    n = len(values)
    if n == 0: return []
    result = [_nan()] * n
    if n < period: return result
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    for i in range(period, n):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result

def sma(values: List[float], period: int) -> List[float]:
    n = len(values)
    result = [_nan()] * n
    if period <= 0 or n < period: return result
    window_sum = sum(values[:period])
    result[period - 1] = window_sum / period
    for i in range(period, n):
        window_sum += values[i] - values[i - period]
        result[i] = window_sum / period
    return result

def atr(highs: List[float], lows: List[float], closes: List[float], period: int) -> List[float]:
    n = len(closes)
    if n < 2: return [_nan()] * n
    trs = [_nan()] * n
    for i in range(1, n):
        trs[i] = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
    result = [_nan()] * n
    if n < period + 1: return result
    # Wilder seed: simple average of first `period` true ranges
    valid_trs = [trs[i] for i in range(1, period + 1)]
    result[period] = sum(valid_trs) / period
    for i in range(period + 1, n):
        if math.isnan(result[i - 1]) or math.isnan(trs[i]): continue
        result[i] = (result[i - 1] * (period - 1) + trs[i]) / period
    return result

def adx(highs: List[float], lows: List[float], closes: List[float], period: int):
    """Returns (adx_list, plus_di_list, minus_di_list)."""
    n = len(closes)
    pdm = [0.0] * n; ndm = [0.0] * n; trs = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]; dn = lows[i - 1] - lows[i]
        pdm[i] = up if up > dn and up > 0 else 0.0
        ndm[i] = dn if dn > up and dn > 0 else 0.0
        trs[i] = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i] - closes[i - 1]))
    sptr = sma(trs, period)
    sppdm = sma(pdm, period)
    spndm = sma(ndm, period)
    pdi = [_nan()] * n; ndi = [_nan()] * n; dx = [_nan()] * n
    for i in range(period, n):
        if math.isnan(sptr[i]) or sptr[i] == 0: continue
        pdi[i] = 100 * sppdm[i] / sptr[i]
        ndi[i] = 100 * spndm[i] / sptr[i]
        s = pdi[i] + ndi[i]
        dx[i] = (100 * abs(pdi[i] - ndi[i]) / s) if s != 0 else 0.0
    # ADX = SMA of DX over `period` bars starting from index `period`
    dx_slice = dx[period:]
    adx_slice = sma(dx_slice, period)
    adx_full = [_nan()] * period + adx_slice
    adx_full = adx_full[:n] + [_nan()] * max(0, n - len(adx_full))
    return adx_full[:n], pdi[:n], ndi[:n]

def macd(closes: List[float], fast: int, slow: int, signal: int):
    """Returns (macd_line, signal_line, histogram). All length == len(closes)."""
    n = len(closes)
    ef = ema(closes, fast); es = ema(closes, slow)
    ml = [_nan()] * n
    for i in range(n):
        if not math.isnan(ef[i]) and not math.isnan(es[i]):
            ml[i] = ef[i] - es[i]
    # Find first non-NaN index for EMA of MACD line
    start = next((i for i, v in enumerate(ml) if not math.isnan(v)), n)
    sl = [_nan()] * n
    if n - start >= signal:
        sr = ema(ml[start:], signal)
        for i, v in enumerate(sr):
            sl[start + i] = v
    hist = [_nan()] * n
    for i in range(n):
        if not math.isnan(ml[i]) and not math.isnan(sl[i]):
            hist[i] = ml[i] - sl[i]
    return ml, sl, hist

def rsi(closes: List[float], period: int) -> List[float]:
    n = len(closes)
    result = [_nan()] * n
    if n < period + 1: return result
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, period + 1)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, period + 1)]
    ag = sum(gains) / period; al = sum(losses) / period
    result[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]; g = max(d, 0.0); l = max(-d, 0.0)
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
        result[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return result

def bollinger_bands(closes: List[float], period: int, std_mult: float):
    """Returns (upper, middle, lower)."""
    n = len(closes)
    mid = sma(closes, period)
    upper = [_nan()] * n; lower = [_nan()] * n
    for i in range(period - 1, n):
        w = closes[i - period + 1: i + 1]
        mean = mid[i]
        variance = sum((x - mean) ** 2 for x in w) / period
        std = math.sqrt(variance)
        upper[i] = mean + std_mult * std
        lower[i] = mean - std_mult * std
    return upper, mid, lower

def last_valid(values: List[float]) -> float:
    """Return last non-NaN value, or NaN if none."""
    for v in reversed(values):
        if not math.isnan(v): return v
    return _nan()
