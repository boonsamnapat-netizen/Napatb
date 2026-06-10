"""
Fibonacci retracement setup scanner.
Replicates FiboRetrace.py logic but standalone (no Freqtrade dependency).
Uses ccxt for public OKX market data — no API keys required.

Universe: top N USDT pairs by 24H volume, stablecoins excluded.
"""

import numpy as np
import pandas as pd
import ccxt
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timezone


# --- Indicator helpers (avoid ta-lib dependency) --------------------------

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


# --- Fibonacci constants --------------------------------------------------

SWING_WINDOW = 18   # fractal half-width (same as FiboTrendConfirm)
FIB_50  = 0.500
FIB_618 = 0.618
FIB_786 = 0.786
FIB_EXT = 1.618


# --- Core pivot logic (identical to FiboRetrace._fractal_pivots) ----------

def _fractal_pivots(high: np.ndarray, low: np.ndarray, w: int):
    n = len(high)
    ph = np.zeros(n, dtype=bool)
    pl = np.zeros(n, dtype=bool)
    for i in range(w, n - w):
        seg_h = high[i - w:i + w + 1]
        seg_l = low[i - w:i + w + 1]
        if high[i] == seg_h.max() and (seg_h == high[i]).sum() == 1:
            ph[i] = True
        if low[i] == seg_l.min() and (seg_l == low[i]).sum() == 1:
            pl[i] = True
    return ph, pl


def _carry_confirmed(piv: np.ndarray, vals: np.ndarray, w: int):
    """Rolling carry of the last confirmed pivot value (no lookahead)."""
    n = len(vals)
    out_val = np.full(n, np.nan)
    out_idx = np.full(n, np.nan)
    last_val = np.nan
    last_idx = np.nan
    for i in range(n):
        j = i - w
        if j >= 0 and piv[j]:
            last_val = vals[j]
            last_idx = float(j)
        out_val[i] = last_val
        out_idx[i] = last_idx
    return out_val, out_idx


# --- Data fetching --------------------------------------------------------

def _fetch_ohlcv(exchange, pair: str, timeframe: str, limit: int) -> pd.DataFrame:
    raw = exchange.fetch_ohlcv(pair, timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.set_index("date").drop(columns=["timestamp"])


# --- Setup detection ------------------------------------------------------

@dataclass
class FiboSetup:
    pair: str
    timeframe: str
    candle_time: datetime
    close: float
    entry_low: float    # fib_50 level
    entry_high: float   # fib_618 level (less retrace = stronger)
    sl: float           # fib_786
    tp: float           # fib_1618 extension
    swing_low: float
    swing_high: float
    retrace_pct: float  # how deep in zone (0=at fib_50, 1=at fib_618)
    trend_score: int    # 0-3: how many trend filters pass
    in_zone: bool       # low touched zone AND close still above fib_618

    @property
    def risk_pct(self) -> float:
        return (self.sl - self.close) / self.close * 100

    @property
    def reward_pct(self) -> float:
        return (self.tp - self.close) / self.close * 100

    @property
    def rr_ratio(self) -> float:
        if self.risk_pct == 0:
            return 0
        return abs(self.reward_pct / self.risk_pct)


def scan_pair(exchange, pair: str) -> Optional[FiboSetup]:
    """
    Check the latest closed 1H candle for a Fibonacci golden-zone entry setup.
    Returns a FiboSetup if a signal exists on the most recent closed bar, else None.
    """
    # Fetch enough history: 1000 1H bars (covers EMA200 warmup + swing window)
    df1h = _fetch_ohlcv(exchange, pair, "1h", limit=1000)
    df4h = _fetch_ohlcv(exchange, pair, "4h", limit=300)
    df1d = _fetch_ohlcv(exchange, pair, "1d", limit=300)

    if len(df1h) < 300 or len(df4h) < 210 or len(df1d) < 210:
        return None  # insufficient history

    # ---- 4H trend indicators -------------------------------------------
    df4h["ema50"]  = _ema(df4h["close"], 50)
    df4h["ema200"] = _ema(df4h["close"], 200)
    df4h["ema200_rising"] = (df4h["ema200"] > df4h["ema200"].shift(5)).astype(int)

    # ---- Daily SMA200 --------------------------------------------------
    df1d["sma200"] = _sma(df1d["close"], 200)

    # ---- Fractal pivots on 1H -----------------------------------------
    w = SWING_WINDOW
    ph_arr, pl_arr = _fractal_pivots(
        df1h["high"].values,
        df1h["low"].values,
        w
    )
    sh_val, sh_idx = _carry_confirmed(ph_arr, df1h["high"].values, w)
    sl_val, sl_idx = _carry_confirmed(pl_arr, df1h["low"].values, w)

    df1h["sh_val"] = sh_val
    df1h["sh_idx"] = sh_idx
    df1h["sl_val"] = sl_val
    df1h["sl_idx"] = sl_idx

    # ---- Fibonacci levels ----------------------------------------------
    fib_valid = (
        (~np.isnan(sh_val)) & (~np.isnan(sl_val)) &
        (sh_idx > sl_idx) &
        (sh_val > sl_val)
    )
    rng = np.where(fib_valid, sh_val - sl_val, np.nan)

    df1h["fib_50"]     = sh_val - FIB_50  * rng
    df1h["fib_618"]    = sh_val - FIB_618 * rng
    df1h["fib_stop"]   = sh_val - FIB_786 * rng
    df1h["fib_target"] = sl_val + FIB_EXT  * rng

    # ---- Check LAST CLOSED candle (index -1 is the latest closed bar) --
    last = df1h.iloc[-1]
    close = last["close"]
    low   = last["low"]

    fib_50     = last.get("fib_50")
    fib_618    = last.get("fib_618")
    fib_stop   = last.get("fib_stop")
    fib_target = last.get("fib_target")

    if any(pd.isna(v) or v <= 0 for v in [fib_50, fib_618, fib_stop, fib_target]):
        return None

    in_zone = (low <= fib_50) and (close >= fib_618)
    if not in_zone:
        return None

    # ---- Trend score (how many filters agree) --------------------------
    # Get 4H and 1D values via ffill alignment
    last_4h = df4h.iloc[-1]
    last_1d = df1d.iloc[-1]

    trend_score = 0
    if close > last_4h.get("ema50",  0): trend_score += 1  # above 4H EMA50
    if close > last_4h.get("ema200", 0): trend_score += 1  # above 4H EMA200
    if last_4h.get("ema200_rising", 0):   trend_score += 1  # 4H EMA200 slope up
    if close > last_1d.get("sma200", 0): trend_score += 1  # above daily SMA200

    # Require AT LEAST 2 trend filters for a valid signal
    if trend_score < 2:
        return None

    retrace_pct = (fib_50 - close) / (fib_50 - fib_618) if (fib_50 - fib_618) > 0 else 0.5

    return FiboSetup(
        pair=pair,
        timeframe="1h",
        candle_time=df1h.index[-1].to_pydatetime(),
        close=close,
        entry_low=fib_618,
        entry_high=fib_50,
        sl=fib_stop,
        tp=fib_target,
        swing_low=last["sl_val"],
        swing_high=last["sh_val"],
        retrace_pct=retrace_pct,
        trend_score=trend_score,
        in_zone=True,
    )


# Stablecoins and pegged tokens to exclude from the universe
_STABLES = {
    "USDC", "BUSD", "DAI", "TUSD", "USDE", "FDUSD", "USDP", "GUSD",
    "FRAX", "LUSD", "SUSD", "CRVUSD", "PYUSD", "EURS", "EURT",
}


def get_top_pairs(exchange, n: int = 20) -> list[str]:
    """
    Return top N USDT spot pairs on OKX ranked by 24H quote volume.
    Stablecoins are excluded. No API key required.
    """
    tickers = exchange.fetch_tickers()

    rows = []
    for symbol, t in tickers.items():
        # Keep USDT spot pairs only (no futures like BTC/USDT:USDT)
        if not symbol.endswith("/USDT") or ":" in symbol:
            continue
        base = symbol.split("/")[0]
        if base in _STABLES:
            continue
        quote_vol = t.get("quoteVolume") or 0
        if quote_vol > 0:
            rows.append((symbol, quote_vol))

    rows.sort(key=lambda x: x[1], reverse=True)
    top = [r[0] for r in rows[:n]]
    print(f"[scanner] Universe (top {n} by 24H vol): {top}")
    return top


def scan_all(pairs: list[str] | None = None, top_n: int = 20) -> list[FiboSetup]:
    """
    Scan for Fibonacci setups.
    If pairs is None, dynamically fetch the top_n pairs by 24H volume.
    Results are sorted by trend_score descending (strongest trend first).
    """
    exchange = ccxt.okx({"enableRateLimit": True})

    if pairs is None:
        pairs = get_top_pairs(exchange, n=top_n)

    setups = []
    for pair in pairs:
        try:
            setup = scan_pair(exchange, pair)
            if setup:
                setups.append(setup)
                print(f"[scanner] ✅ {pair}: trend={setup.trend_score}/4  R:R={setup.rr_ratio:.1f}")
            else:
                print(f"[scanner]    {pair}: no setup")
        except Exception as e:
            print(f"[scanner] ❌ {pair} error: {e}")

    # Best setups first (highest trend score, then best R:R)
    setups.sort(key=lambda s: (s.trend_score, s.rr_ratio), reverse=True)
    return setups
