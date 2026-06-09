"""
Pure-Python indicator library. No external deps. No look-ahead.
All functions return lists of the same length as input, with float('nan')
for positions where insufficient history exists.
"""
import math
from typing import List, Tuple


def last_valid(series: List[float]) -> float:
    for v in reversed(series):
        if not math.isnan(v):
            return v
    return float("nan")


def sma(values: List[float], period: int) -> List[float]:
    n = len(values)
    out = [float("nan")] * n
    for i in range(period - 1, n):
        window = values[i - period + 1: i + 1]
        if any(math.isnan(v) for v in window):
            continue
        out[i] = sum(window) / period
    return out


def ema(values: List[float], period: int) -> List[float]:
    n = len(values)
    out = [float("nan")] * n
    k = 2.0 / (period + 1)
    seed_start = None
    for i in range(n):
        if math.isnan(values[i]):
            continue
        if seed_start is None:
            if i + period <= n:
                window = values[i: i + period]
                if not any(math.isnan(v) for v in window):
                    out[i + period - 1] = sum(window) / period
                    seed_start = i + period - 1
        else:
            out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(closes: List[float], period: int = 14) -> List[float]:
    n = len(closes)
    out = [float("nan")] * n
    if n < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, n):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    if len(gains) < period:
        return out
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    if avg_l == 0:
        out[period] = 100.0
    else:
        rs = avg_g / avg_l
        out[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        if avg_l == 0:
            out[i + 1] = 100.0
        else:
            rs = avg_g / avg_l
            out[i + 1] = 100.0 - 100.0 / (1.0 + rs)
    return out


def bollinger_bands(closes: List[float], period: int = 20,
                    std_dev: float = 2.0) -> Tuple[List[float], List[float], List[float]]:
    n = len(closes)
    upper = [float("nan")] * n
    middle = [float("nan")] * n
    lower = [float("nan")] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1: i + 1]
        if any(math.isnan(v) for v in window):
            continue
        m = sum(window) / period
        variance = sum((v - m) ** 2 for v in window) / period
        sd = math.sqrt(variance)
        middle[i] = m
        upper[i] = m + std_dev * sd
        lower[i] = m - std_dev * sd
    return upper, middle, lower


def _true_range(highs: List[float], lows: List[float], closes: List[float]) -> List[float]:
    n = len(closes)
    tr = [float("nan")] * n
    for i in range(1, n):
        if any(math.isnan(v) for v in [highs[i], lows[i], closes[i - 1]]):
            continue
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    if not math.isnan(highs[0]) and not math.isnan(lows[0]):
        tr[0] = highs[0] - lows[0]
    return tr


def atr(highs: List[float], lows: List[float], closes: List[float],
        period: int = 14) -> List[float]:
    tr = _true_range(highs, lows, closes)
    n = len(tr)
    out = [float("nan")] * n
    valids = [(i, v) for i, v in enumerate(tr) if not math.isnan(v)]
    if len(valids) < period:
        return out
    start_idx = valids[period - 1][0]
    seed = sum(v for _, v in valids[:period]) / period
    out[start_idx] = seed
    k = 1.0 / period
    for i in range(start_idx + 1, n):
        if math.isnan(tr[i]):
            out[i] = out[i - 1]
        else:
            prev = out[i - 1]
            out[i] = tr[i] * k + prev * (1 - k) if not math.isnan(prev) else tr[i]
    return out


def adx(highs: List[float], lows: List[float], closes: List[float],
        period: int = 14) -> Tuple[List[float], List[float], List[float]]:
    n = len(closes)
    adx_out = [float("nan")] * n
    plus_di  = [float("nan")] * n
    minus_di = [float("nan")] * n
    if n < period * 2 + 1:
        return adx_out, plus_di, minus_di

    tr_list, pdm_list, ndm_list = [], [], []
    for i in range(1, n):
        h, l, ph, pl, pc = highs[i], lows[i], highs[i-1], lows[i-1], closes[i-1]
        if any(math.isnan(v) for v in [h, l, ph, pl, pc]):
            tr_list.append(float("nan"))
            pdm_list.append(float("nan"))
            ndm_list.append(float("nan"))
            continue
        tr_val  = max(h - l, abs(h - pc), abs(l - pc))
        up_move = h - ph
        dn_move = pl - l
        pdm = up_move if up_move > dn_move and up_move > 0 else 0.0
        ndm = dn_move if dn_move > up_move and dn_move > 0 else 0.0
        tr_list.append(tr_val)
        pdm_list.append(pdm)
        ndm_list.append(ndm)

    valid = [(i, t, p, d) for i, (t, p, d) in enumerate(zip(tr_list, pdm_list, ndm_list))
             if not any(math.isnan(v) for v in [t, p, d])]
    if len(valid) < period * 2:
        return adx_out, plus_di, minus_di

    k = 1.0 / period
    atr_s = sum(t for _, t, _, _ in valid[:period]) / period
    pdm_s = sum(p for _, _, p, _ in valid[:period]) / period
    ndm_s = sum(d for _, _, _, d in valid[:period]) / period

    dx_series = []
    for idx in range(period, len(valid)):
        _, t, p, d = valid[idx]
        atr_s = t * k + atr_s * (1 - k)
        pdm_s = p * k + pdm_s * (1 - k)
        ndm_s = d * k + ndm_s * (1 - k)
        if atr_s == 0:
            dx_series.append(0.0)
            continue
        pdi = 100.0 * pdm_s / atr_s
        ndi = 100.0 * ndm_s / atr_s
        denom = pdi + ndi
        dx = 100.0 * abs(pdi - ndi) / denom if denom != 0 else 0.0
        real_i = valid[idx][0] + 1  # offset back to closes index
        plus_di[real_i]  = pdi
        minus_di[real_i] = ndi
        dx_series.append(dx)

    if len(dx_series) >= period:
        adx_seed = sum(dx_series[:period]) / period
        base_i = valid[period * 2 - 1][0] + 1
        if base_i < n:
            adx_out[base_i] = adx_seed
        for j in range(period, len(dx_series)):
            real_i = valid[period + j][0] + 1 if (period + j) < len(valid) else n - 1
            prev_adx = adx_out[real_i - 1]
            if not math.isnan(prev_adx):
                adx_out[real_i] = dx_series[j] * k + prev_adx * (1 - k)
    return adx_out, plus_di, minus_di


def macd(closes: List[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> Tuple[List[float], List[float], List[float]]:
    n = len(closes)
    nan = float("nan")
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    ml = [nan] * n
    for i in range(n):
        if not math.isnan(fast_ema[i]) and not math.isnan(slow_ema[i]):
            ml[i] = fast_ema[i] - slow_ema[i]
    start = next((i for i, v in enumerate(ml) if not math.isnan(v)), None)
    if start is None or (n - start) < signal:
        return ml, [nan] * n, [nan] * n
    sl_part = ema(ml[start:], signal)
    sl = [nan] * start + sl_part
    hist = [ml[i] - sl[i] if not (math.isnan(ml[i]) or math.isnan(sl[i])) else nan for i in range(n)]
    return ml, sl, hist
