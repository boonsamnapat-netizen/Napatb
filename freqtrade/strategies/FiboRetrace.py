"""
FiboRetrace -- Rule-based Fibonacci Retracement Strategy (1H entry / 4H trend)
Phase E: Pivot away from ML. Automate manual S/R + Fibo + RSI/MACD trading.

Setup (long-only, spot):
  1. 4H trend filter: only long when 4H close > 4H EMA50 (trade with the trend)
  2. Detect last confirmed up-leg: swing low (L) -> swing high (H) on 1H
     (pivots confirmed `swing_window` bars later -- NO lookahead)
  3. Price retraces into the golden zone (0.5 - 0.618 of L->H)
  4. Entry:
       - 'touch'   = price enters golden zone (mechanical)
       - 'confirm' = golden zone + RSI turning up + MACD histogram rising
  5. TP = Fibonacci extension (H + 0.272 * range)  [custom_exit]
     SL = just below swing low L                    [custom_stoploss]

Swing modes:
  - 'recent' (swing_window=5)  : local 1H swing -- frequent, smaller legs
  - 'trend'  (swing_window=18) : larger leg aligned with the 4H trend -- rarer, bigger legs

Four variants backtested together via --strategy-list:
  FiboRecentConfirm, FiboRecentTouch, FiboTrendConfirm, FiboTrendTouch
"""

import numpy as np
import pandas as pd
from freqtrade.strategy import IStrategy, merge_informative_pair
import talib.abstract as ta
from pandas import DataFrame
from datetime import datetime


class FiboRetrace(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    inf_timeframe = "4h"

    # Exits are governed by custom_exit (Fibo extension) and
    # custom_stoploss (below swing low). ROI disabled, hard stop as safety net.
    minimal_roi = {"0": 100.0}
    stoploss = -0.15
    use_custom_stoploss = True
    trailing_stop = False

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    startup_candle_count = 900   # covers 4H EMA200 warmup
    can_short = False

    # --- Variant toggles (overridden by subclasses) ---
    SWING_WINDOW  = 18        # fractal half-width for pivot detection
    ENTRY_MODE    = "touch"   # 'confirm' | 'touch'
    USE_BREAKEVEN = False     # BE@1R hurt (cut winners); kept off
    BE_TRIGGER_R  = 1.0       # how many R of profit before SL -> breakeven
    TREND_MODE    = "base"    # 'base'|'slope'|'ema200'|'both' -- trend-strength filter
    USE_TRAIL     = False     # trail SL below running peak to ride trends
    TRAIL_PCT     = 0.08      # trail this far below peak high
    TRAIL_ACT     = 0.03      # activate trailing after +3% profit

    # Fibonacci ratios
    FIB_382  = 0.382
    FIB_50   = 0.500
    FIB_618  = 0.618
    FIB_STOP = 0.786       # SL just below this invalidation level
    FIB_EXT  = 0.272       # extension beyond H for TP

    # ---- Pivot detection (no lookahead) -----------------------------------

    @staticmethod
    def _fractal_pivots(high: np.ndarray, low: np.ndarray, w: int):
        """Strict fractal pivots: high[i] is the sole max in [i-w, i+w]."""
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

    @staticmethod
    def _carry_confirmed(piv: np.ndarray, vals: np.ndarray, w: int):
        """For each bar, the value/index of the last pivot CONFIRMED by that bar.
        A pivot centered at j is only knowable at bar j+w (no future leak)."""
        n = len(vals)
        out_val = np.full(n, np.nan)
        out_idx = np.full(n, np.nan)
        last_val = np.nan
        last_idx = np.nan
        for i in range(n):
            j = i - w
            if j >= 0 and piv[j]:
                last_val = vals[j]
                last_idx = j
            out_val[i] = last_val
            out_idx[i] = last_idx
        return out_val, out_idx

    # ---- Indicators --------------------------------------------------------

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 1H momentum indicators
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd"]     = macd["macd"]
        dataframe["macdsig"]  = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]

        # 4H trend filter via informative pair
        inf = self.dp.get_pair_dataframe(metadata["pair"], self.inf_timeframe)
        inf["ema50"]  = ta.EMA(inf, timeperiod=50)
        inf["ema200"] = ta.EMA(inf, timeperiod=200)
        inf["ema50_rising"] = (inf["ema50"] > inf["ema50"].shift(1)).astype(int)
        dataframe = merge_informative_pair(
            dataframe, inf, self.timeframe, self.inf_timeframe, ffill=True
        )
        itf = self.inf_timeframe
        close_col = f"close_{itf}"
        # Base trend + optional strength filters
        base_up   = dataframe[close_col] > dataframe[f"ema50_{itf}"]
        rising    = dataframe[f"ema50_rising_{itf}"] == 1
        above_200 = dataframe[close_col] > dataframe[f"ema200_{itf}"]
        if self.TREND_MODE == "slope":
            trend = base_up & rising
        elif self.TREND_MODE == "ema200":
            trend = base_up & above_200
        elif self.TREND_MODE == "both":
            trend = base_up & rising & above_200
        else:  # 'base'
            trend = base_up
        dataframe["trend_up"] = trend.astype(int)

        # Pivot-based swings (confirmed, no lookahead)
        w = self.SWING_WINDOW
        high = dataframe["high"].values
        low  = dataframe["low"].values
        ph, pl = self._fractal_pivots(high, low, w)
        sh_val, sh_idx = self._carry_confirmed(ph, high, w)
        sl_val, sl_idx = self._carry_confirmed(pl, low,  w)

        dataframe["sh_val"] = sh_val
        dataframe["sl_val"] = sl_val
        dataframe["sh_idx"] = sh_idx
        dataframe["sl_idx"] = sl_idx

        # Valid up-leg: most-recent swing high formed AFTER the swing low, H > L
        up_leg = (
            ~np.isnan(sh_idx) & ~np.isnan(sl_idx) &
            (sh_idx > sl_idx) & (sh_val > sl_val)
        )
        dataframe["up_leg"] = up_leg.astype(int)

        rng = np.where(up_leg, sh_val - sl_val, np.nan)
        H = np.where(up_leg, sh_val, np.nan)
        L = np.where(up_leg, sl_val, np.nan)

        dataframe["fib_382"] = H - self.FIB_382 * rng
        dataframe["fib_50"]  = H - self.FIB_50  * rng
        dataframe["fib_618"] = H - self.FIB_618 * rng
        dataframe["fib_target"] = H + self.FIB_EXT * rng    # extension TP
        # SL just below the 0.786 invalidation level (tight, classic Fibo stop)
        # -- not the full swing low, which made losses too wide (R:R ~1.4 -> 2.7).
        dataframe["fib_stop"] = (H - self.FIB_STOP * rng) * 0.999

        return dataframe

    # ---- Entry -------------------------------------------------------------

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        in_zone = (
            (dataframe["up_leg"] == 1) &
            (dataframe["low"] <= dataframe["fib_50"]) &     # dipped into golden zone
            (dataframe["close"] >= dataframe["fib_618"]) &  # still holding 0.618
            (dataframe["close"] <= dataframe["fib_382"])    # not above zone
        )
        trend_up = dataframe["trend_up"] == 1

        if self.ENTRY_MODE == "confirm":
            confirm = (
                (dataframe["rsi"] > dataframe["rsi"].shift(1)) &
                (dataframe["macdhist"] > dataframe["macdhist"].shift(1))
            )
            entry = in_zone & trend_up & confirm
        else:  # 'touch'
            entry = in_zone & trend_up

        dataframe.loc[entry, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    # ---- Level-based exits (locked at entry) -------------------------------

    def _trade_levels(self, trade):
        """Return (target, stop) locked at entry candle; cached on the trade."""
        try:
            t = trade.get_custom_data("fib_target")
            s = trade.get_custom_data("fib_stop")
            if t is not None and s is not None:
                return t, s
        except Exception:
            pass

        df, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
        if df is None or df.empty:
            return None, None
        sig = df[df["date"] < trade.open_date_utc]
        if sig.empty:
            return None, None
        row = sig.iloc[-1]
        t = row.get("fib_target")
        s = row.get("fib_stop")
        if t is None or s is None or pd.isna(t) or pd.isna(s):
            return None, None
        try:
            trade.set_custom_data("fib_target", float(t))
            trade.set_custom_data("fib_stop", float(s))
        except Exception:
            pass
        return float(t), float(s)

    def custom_stoploss(self, pair, trade, current_time, current_rate,
                        current_profit, **kwargs) -> float:
        _, stop = self._trade_levels(trade)
        if stop is None or current_rate <= 0:
            return -0.05  # fallback

        entry = trade.open_rate

        # Trailing: once +TRAIL_ACT in profit, trail the stop TRAIL_PCT below
        # the running peak high to ride extended trends (never below initial stop).
        if self.USE_TRAIL and entry and current_rate >= entry * (1 + self.TRAIL_ACT):
            try:
                df, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
                seg = df[df["date"] >= trade.open_date_utc]
                peak = float(seg["high"].max()) if not seg.empty else current_rate
            except Exception:
                peak = current_rate
            trail_stop = max(peak * (1 - self.TRAIL_PCT), stop)
            ratio = (trail_stop / current_rate) - 1.0
            return ratio if ratio < 0 else -0.0005

        # Break-even: once price has gained BE_TRIGGER_R * initial risk,
        # ratchet the stop up to entry (+fee buffer) so the trade can't turn red.
        if self.USE_BREAKEVEN and entry and entry > stop:
            risk = entry - stop
            if current_rate >= entry + self.BE_TRIGGER_R * risk:
                be = entry * 1.002  # cover ~2x fee
                ratio = (be / current_rate) - 1.0
                return ratio if ratio < 0 else -0.0005

        ratio = (stop / current_rate) - 1.0   # fixed-price stop, relative to current
        if ratio >= 0:
            return -0.001
        return max(ratio, -0.20)

    def custom_exit(self, pair, trade, current_time, current_rate,
                   current_profit, **kwargs):
        target, _ = self._trade_levels(trade)
        if target is not None and current_rate >= target:
            return "fib_target"
        return None


# ---- Peak sweep 1.8-2.4 (Phase E.8) ----------------------------------------
# E.7: profit kept rising 1.382->1.8 (+17.77%); E.6 had 2.618 losing (-20%).
# True peak is between 1.8 and 2.618 -- locate it. Same 21 huge-trend winners
# carry the result; farther TP books more per winner until trends fall short.

class FiboRecentTouch(FiboRetrace):
    FIB_EXT = 1.8     # E.7 best, re-confirm


class FiboTrendTouch(FiboRetrace):
    FIB_EXT = 2.0


class FiboTrendConfirm(FiboRetrace):
    FIB_EXT = 2.2


class FiboRecentConfirm(FiboRetrace):
    FIB_EXT = 2.4
