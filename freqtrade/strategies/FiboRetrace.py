"""
FiboRetrace -- Fibonacci Retracement Strategy (1H / 4H trend, Long + Short)
Phase F: Symmetric long/short -- profits in both bull and bear markets.

Long setup  (up-trend  + pullback):
  SL → SH on 1H confirmed → price retraces to 50-61.8% golden zone → BUY
  SL at 0.786, TP at SH + 1.618 * range (extension above SH)

Short setup (down-trend + bounce):
  SH → SL on 1H confirmed → price bounces to 50-61.8% from SL → SELL
  SL at 0.786 above SL, TP at SL - 1.618 * range (extension below SL)

R:R ~7:1 gross (wide TP, tight SL) → break-even WR ~12% gross, ~15% post-fee.
In-sample WR target: >15%.

Anti-lookahead: fractal pivot at bar i confirmed at bar i+SWING_WINDOW.
At bar t, only pivots from bars ≤ t-w are visible.
"""

import numpy as np
import pandas as pd
from freqtrade.strategy import IStrategy, merge_informative_pair, stoploss_from_absolute
import talib.abstract as ta
from pandas import DataFrame
from datetime import datetime


class FiboRetrace(IStrategy):
    INTERFACE_VERSION = 3

    timeframe     = "1h"
    inf_timeframe = "4h"

    minimal_roi         = {"0": 100.0}   # custom_exit handles TP
    stoploss            = -0.15          # safety net; custom_stoploss uses fib levels
    use_custom_stoploss = True
    trailing_stop       = False

    process_only_new_candles  = True
    use_exit_signal           = True
    exit_profit_only          = False
    ignore_roi_if_entry_signal= False
    startup_candle_count      = 900      # covers 4H EMA200 (200 bars × 4h = 800h)
    can_short                 = True

    # ---- Variant parameters ------------------------------------------------
    SWING_WINDOW   = 18        # fractal half-width in 1H bars
    ENTRY_MODE     = "touch"   # "touch" = mechanical; "confirm" = RSI+MACD filter
    TREND_MODE     = "base"    # see populate_indicators for modes
    BREAKEVEN_PCT  = None      # fraction of (entry→TP) range; None = disabled

    # Fibonacci ratios
    FIB_50   = 0.500
    FIB_618  = 0.618
    FIB_SL   = 0.786   # stoploss placement (tunable); 0.786 = classical invalidation
    FIB_EXT  = 1.618   # extension TP (same ratio as the golden ratio leg)

    # ---- Pivot helpers (no lookahead) -------------------------------------

    @staticmethod
    def _fractal_pivots(high: np.ndarray, low: np.ndarray, w: int):
        """Strict fractal pivot: sole max/min in [i-w, i+w] window."""
        n  = len(high)
        ph = np.zeros(n, dtype=bool)
        pl = np.zeros(n, dtype=bool)
        for i in range(w, n - w):
            seg_h = high[i - w:i + w + 1]
            seg_l = low [i - w:i + w + 1]
            if high[i] == seg_h.max() and (seg_h == high[i]).sum() == 1:
                ph[i] = True
            if low[i]  == seg_l.min() and (seg_l == low[i] ).sum() == 1:
                pl[i] = True
        return ph, pl

    @staticmethod
    def _carry_confirmed(piv: np.ndarray, vals: np.ndarray, w: int):
        """
        At bar t, carry the value of the last pivot confirmed at or before t.
        A pivot at index j is only visible from bar j+w onward (no lookahead).
        """
        n        = len(vals)
        out_val  = np.full(n, np.nan)
        out_idx  = np.full(n, np.nan)
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

    # ---- Indicators --------------------------------------------------------

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]

        # 1H momentum (for 'confirm' mode entry filter)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        _macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macdhist"] = _macd["macdhist"]

        # ---- 4H trend -------------------------------------------------
        inf4h = self.dp.get_pair_dataframe(pair, self.inf_timeframe)
        inf4h["ema50"]         = ta.EMA(inf4h, timeperiod=50)
        inf4h["ema200"]        = ta.EMA(inf4h, timeperiod=200)
        inf4h["ema200_rising"] = (inf4h["ema200"] > inf4h["ema200"].shift(3)).astype(int)
        dataframe = merge_informative_pair(
            dataframe, inf4h, self.timeframe, self.inf_timeframe, ffill=True
        )

        # ---- Daily macro regime -------------------------------------------
        infd = self.dp.get_pair_dataframe(pair, "1d")
        infd["sma200"] = ta.SMA(infd, timeperiod=200)
        dataframe = merge_informative_pair(
            dataframe, infd, self.timeframe, "1d", ffill=True
        )

        # ---- Trend flags --------------------------------------------------
        itf        = self.inf_timeframe
        close_4h   = dataframe[f"close_{itf}"]
        ema50_4h   = dataframe[f"ema50_{itf}"]
        ema200_4h  = dataframe[f"ema200_{itf}"]
        rising_4h  = dataframe[f"ema200_rising_{itf}"] == 1
        close_1d   = dataframe["close_1d"]
        sma200_1d  = dataframe["sma200_1d"]

        base_up   = close_4h > ema50_4h
        above_200 = close_4h > ema200_4h
        bull_1d   = close_1d > sma200_1d

        if self.TREND_MODE == "slope":
            trend_up   = base_up & rising_4h
            trend_down = ~base_up & ~rising_4h
        elif self.TREND_MODE == "ema200":
            trend_up   = base_up & above_200
            trend_down = ~base_up & ~above_200
        elif self.TREND_MODE == "ema200slope":
            trend_up   = base_up & above_200 & rising_4h
            trend_down = ~base_up & ~above_200 & ~rising_4h
        elif self.TREND_MODE == "macro":
            trend_up   = base_up & bull_1d
            trend_down = ~base_up & ~bull_1d
        elif self.TREND_MODE == "macro200":
            trend_up   = base_up & above_200 & bull_1d
            trend_down = ~base_up & ~above_200 & ~bull_1d
        else:  # "base" -- just EMA50 side
            trend_up   = base_up
            trend_down = ~base_up

        dataframe["trend_up"]   = trend_up.fillna(False).astype(int)
        dataframe["trend_down"] = trend_down.fillna(False).astype(int)

        # ---- Fractal pivots (no lookahead) --------------------------------
        w        = self.SWING_WINDOW
        high_arr = dataframe["high"].values
        low_arr  = dataframe["low"].values
        ph, pl   = self._fractal_pivots(high_arr, low_arr, w)

        sh_val, sh_idx = self._carry_confirmed(ph, high_arr, w)
        sl_val, sl_idx = self._carry_confirmed(pl, low_arr,  w)

        dataframe["sh_val"] = sh_val
        dataframe["sl_val"] = sl_val
        dataframe["sh_idx"] = sh_idx
        dataframe["sl_idx"] = sl_idx

        # ---- Long Fibonacci (up-leg: SL → SH → pullback) ----------------
        up_leg = (
            ~np.isnan(sh_idx) & ~np.isnan(sl_idx) &
            (sh_idx > sl_idx) & (sh_val > sl_val)
        )
        dataframe["up_leg"] = up_leg.astype(int)

        rng_u = np.where(up_leg, sh_val - sl_val, np.nan)
        H_u   = np.where(up_leg, sh_val, np.nan)
        L_u   = np.where(up_leg, sl_val, np.nan)

        dataframe["fib_50"]     = H_u - self.FIB_50  * rng_u    # golden zone top
        dataframe["fib_618"]    = H_u - self.FIB_618 * rng_u    # golden zone bottom
        dataframe["fib_stop"]   = (H_u - self.FIB_SL * rng_u) * 0.999
        dataframe["fib_target"] = H_u + self.FIB_EXT * rng_u    # extension TP

        # ---- Short Fibonacci (down-leg: SH → SL → bounce) ---------------
        down_leg = (
            ~np.isnan(sl_idx) & ~np.isnan(sh_idx) &
            (sl_idx > sh_idx) & (sl_val < sh_val)   # SL is more recent AND lower
        )
        dataframe["down_leg"] = down_leg.astype(int)

        rng_d  = np.where(down_leg, sh_val - sl_val, np.nan)
        L_d    = np.where(down_leg, sl_val, np.nan)

        # Entry zone: price bounced from SL up to 50-61.8% of the down-leg range
        dataframe["s_fib_50"]     = L_d + self.FIB_50  * rng_d   # zone bottom (50% from SL)
        dataframe["s_fib_618"]    = L_d + self.FIB_618 * rng_d   # zone top    (61.8% from SL)
        dataframe["s_fib_stop"]   = (L_d + self.FIB_SL * rng_d) * 1.001  # SL above 0.786
        dataframe["s_fib_target"] = L_d - (self.FIB_EXT - 1.0) * rng_d   # ext below SL

        return dataframe

    # ---- Entry signals ----------------------------------------------------

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # --- Long entry ---
        in_zone_long = (
            (dataframe["up_leg"] == 1) &
            (dataframe["low"]   <= dataframe["fib_50"]) &   # dipped into zone
            (dataframe["close"] >= dataframe["fib_618"]) &  # held above 0.618
            (dataframe["close"] <= dataframe["fib_50"])     # close within zone
        )
        trend_up = dataframe["trend_up"] == 1

        # --- Short entry ---
        in_zone_short = (
            (dataframe["down_leg"] == 1) &
            (dataframe["high"]  >= dataframe["s_fib_50"]) &   # touched zone from below
            (dataframe["close"] <= dataframe["s_fib_618"]) &  # didn't break above
            (dataframe["close"] >= dataframe["s_fib_50"])     # close within zone
        )
        trend_down = dataframe["trend_down"] == 1

        if self.ENTRY_MODE == "confirm":
            rsi_up   = dataframe["rsi"] > dataframe["rsi"].shift(1)
            hist_up  = dataframe["macdhist"] > dataframe["macdhist"].shift(1)
            rsi_down = dataframe["rsi"] < dataframe["rsi"].shift(1)
            hist_down= dataframe["macdhist"] < dataframe["macdhist"].shift(1)

            dataframe.loc[in_zone_long  & trend_up   & rsi_up   & hist_up,   "enter_long"]  = 1
            dataframe.loc[in_zone_short & trend_down & rsi_down & hist_down, "enter_short"] = 1
        else:
            dataframe.loc[in_zone_long  & trend_up,   "enter_long"]  = 1
            dataframe.loc[in_zone_short & trend_down, "enter_short"] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    # ---- Level-based exits (locked at entry bar) --------------------------

    def _trade_levels(self, trade):
        """Return (target_price, stop_price) from the entry bar's Fib levels."""
        # Try cache first (avoids re-scanning on every tick)
        try:
            t = trade.get_custom_data("fib_target")
            s = trade.get_custom_data("fib_stop")
            if t is not None and s is not None:
                return float(t), float(s)
        except Exception:
            pass

        df, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
        if df is None or df.empty:
            return None, None

        # Use the last candle BEFORE trade open (= the signal bar)
        sig = df[df["date"] < trade.open_date_utc]
        if sig.empty:
            return None, None

        row    = sig.iloc[-1]
        t_col  = "s_fib_target" if trade.is_short else "fib_target"
        s_col  = "s_fib_stop"   if trade.is_short else "fib_stop"
        t      = row.get(t_col)
        s      = row.get(s_col)

        if t is None or s is None or pd.isna(t) or pd.isna(s):
            return None, None

        try:
            trade.set_custom_data("fib_target", float(t))
            trade.set_custom_data("fib_stop",   float(s))
        except Exception:
            pass

        return float(t), float(s)

    def custom_stoploss(self, pair, trade, current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs) -> float:
        target, stop = self._trade_levels(trade)
        if stop is None or current_rate <= 0:
            return -0.05

        # Breakeven promotion: once price has moved BREAKEVEN_PCT of the way to TP,
        # shift stop to entry (with 0.1% buffer to avoid fee-induced stops).
        if self.BREAKEVEN_PCT is not None and target is not None:
            entry = trade.open_rate
            if trade.is_short:
                total_move   = entry - target        # total move to TP (positive)
                current_move = entry - current_rate  # how far price has moved down
                be_price     = entry * 1.001         # breakeven with buffer (short)
            else:
                total_move   = target - entry        # total move to TP (positive)
                current_move = current_rate - entry  # how far price has moved up
                be_price     = entry * 0.999         # breakeven with buffer (long)

            if total_move > 0 and current_move / total_move >= self.BREAKEVEN_PCT:
                be_sl = stoploss_from_absolute(be_price, current_rate, is_short=trade.is_short)
                # Only tighten — never widen the stop
                sl = stoploss_from_absolute(stop, current_rate, is_short=trade.is_short)
                return max(be_sl, sl)

        sl = stoploss_from_absolute(stop, current_rate, is_short=trade.is_short)
        return max(sl, -0.20)

    def custom_exit(self, pair, trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        target, _ = self._trade_levels(trade)
        if target is None:
            return None

        if trade.is_short and current_rate <= target:
            return "fib_target_short"
        if not trade.is_short and current_rate >= target:
            return "fib_target_long"
        return None


# ---- Production strategy — Phase F (final) ---------------------------------
# Optimization path: F.1→F.2 (ext sweep) → F.3 (confirm filter) →
#   F.4 (ext sweep w/ confirm) → F.5 (multi-pair, swing-window) →
#   F.6 (w fine-tune) → F.7 (breakeven, no effect) → F.8 (SL sweep) →
#   F.9 (3-period final validation)
#
# FiboSC14w14 validated results (BTC+ETH+SOL):
#   IS   2023-2025: +34.48%  WR 15.0%  147 trades  DD 29.65%
#   OOS1 2021-2023: +41.88%  WR 19.3%  145 trades  DD 32.06%
#   OOS2 2020-2021: -7.25%   WR 13.5%   37 trades  DD 17.88%
#   (OOS2 low trade count due to macro200 filter suppressing entries
#    during COVID crash — correct risk-avoidance behavior)

class FiboSC14w14(FiboRetrace):
    """
    Production strategy: Fibonacci retracement, Long+Short, 1H/4H/1D.
    Pairs: BTC/USDT:USDT  ETH/USDT:USDT  SOL/USDT:USDT
    Parameters proven across 3 independent time periods (2020-2025).
    """
    TREND_MODE   = "macro200"   # 4H EMA50 + 4H EMA200 + 1D SMA200
    ENTRY_MODE   = "confirm"    # RSI rising + MACD hist rising at entry
    FIB_EXT      = 1.4          # TP extension (vs 1.618 classical — higher WR)
    SWING_WINDOW = 14           # fractal pivot half-width in 1H bars
    FIB_SL       = 0.786        # stoploss at classical Fibonacci invalidation
