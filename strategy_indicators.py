"""
SHARED INDICATOR HELPERS
========================
Ported 1:1 from the three strategy backtests so the live detectors produce the
same indicator values their backtests did:

  - AxisPro_Backtest_15m_top50.py        (ema_series, atr_series, sma_of_series, pivots)
  - BacktestBreakout_6coins_2000d_RR3.py (ema_series, ADX/DI, ATR, volume MA)
  - ict_gap_v3_backtest.py               (no heavy indicators — gap math lives in the detector)

All functions operate on plain python lists and return same-length lists with
leading ``None`` until the indicator is warm. This matches the backtests, which
recompute over the full series each bar. The live engine keeps a rolling window
(CANDLE_LIMIT) and recomputes over it on every close — EMA/ATR are recursive and
converge, so values match the backtest to floating-point noise once warm.
"""

from typing import List, Optional


# ── EMA (seeded with a simple average of the first `period` values) ───────────
def ema_series(values: List[float], period: int) -> List[Optional[float]]:
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, n):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


# ── Wilder ATR ────────────────────────────────────────────────────────────────
def atr_series(highs: List[float], lows: List[float], closes: List[float],
               period: int) -> List[Optional[float]]:
    n = len(closes)
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    out: List[Optional[float]] = [None] * n
    if n > period:
        out[period] = sum(tr[1:period + 1]) / period
        for i in range(period + 1, n):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


# ── SMA over a series that may contain leading None values ────────────────────
def sma_of_series(series: List[Optional[float]], period: int) -> List[Optional[float]]:
    n = len(series)
    out: List[Optional[float]] = [None] * n
    for i in range(n):
        if i + 1 < period:
            continue
        window = series[i - period + 1:i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(window) / period
    return out


# ── Wilder ADX / +DI / -DI (returns last-bar adx, or full series) ─────────────
def adx_series(highs: List[float], lows: List[float], closes: List[float],
               period: int = 14) -> List[Optional[float]]:
    n = len(closes)
    tr_r = [0.0] * n
    dm_p = [0.0] * n
    dm_n = [0.0] * n
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        tr_r[i] = max(h - l, abs(h - pc), abs(l - pc))
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        if up > dn and up > 0:
            dm_p[i] = up
        if dn > up and dn > 0:
            dm_n[i] = dn

    s_tr = [0.0] * n
    s_dp = [0.0] * n
    s_dn = [0.0] * n
    p = period
    if n > p:
        s_tr[p] = sum(tr_r[1:p + 1])
        s_dp[p] = sum(dm_p[1:p + 1])
        s_dn[p] = sum(dm_n[1:p + 1])
        for i in range(p + 1, n):
            s_tr[i] = s_tr[i - 1] - s_tr[i - 1] / p + tr_r[i]
            s_dp[i] = s_dp[i - 1] - s_dp[i - 1] / p + dm_p[i]
            s_dn[i] = s_dn[i - 1] - s_dn[i - 1] / p + dm_n[i]

    dx_s: List[Optional[float]] = [None] * n
    for i in range(p, n):
        av = s_tr[i]
        if av == 0:
            continue
        dip = 100.0 * s_dp[i] / av
        din = 100.0 * s_dn[i] / av
        denom = dip + din
        dx_s[i] = 0.0 if denom == 0 else 100.0 * abs(dip - din) / denom

    first_dx = next((i for i in range(n) if dx_s[i] is not None), None)
    adx_s: List[Optional[float]] = [None] * n
    if first_dx is not None:
        se = first_dx + p
        if se <= n:
            sv = [dx_s[i] for i in range(first_dx, se) if dx_s[i] is not None]
            if len(sv) == p:
                adx_s[se - 1] = sum(sv) / p
                for i in range(se, n):
                    if dx_s[i] is not None and adx_s[i - 1] is not None:
                        adx_s[i] = (adx_s[i - 1] * (p - 1) + dx_s[i]) / p
    return adx_s


# ── Pivots (used by AxisPro break-of-structure) ───────────────────────────────
def pivot_high(highs: List[float], i: int, left: int, right: int) -> Optional[float]:
    if i - left < 0 or i + right >= len(highs):
        return None
    piv = highs[i]
    for j in range(i - left, i + right + 1):
        if j == i:
            continue
        if highs[j] >= piv:
            return None
    return piv


def pivot_low(lows: List[float], i: int, left: int, right: int) -> Optional[float]:
    if i - left < 0 or i + right >= len(lows):
        return None
    piv = lows[i]
    for j in range(i - left, i + right + 1):
        if j == i:
            continue
        if lows[j] <= piv:
            return None
    return piv
