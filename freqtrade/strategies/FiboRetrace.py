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
from datetime import datetime, timedelta
from itertools import takewhile
from typing import Optional


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

    # ---- Closed-loop adaptive parameters -----------------------------------
    # After each closed trade the system reviews recent history and adjusts:
    #   ≥ ADAPT_TIGHT consecutive losses → require stricter confirm (RSI>50, MACD>0)
    #   ≥ ADAPT_STOP  consecutive losses → skip ALL new entries until a win resets
    #   stake scales down 15% per consecutive loss (floor ADAPT_STAKE_FLOOR)
    ADAPTIVE           = False  # disabled by default; subclasses opt in
    ADAPT_LOOKBACK     = 15     # number of recent closed trades to review
    ADAPT_TIGHT        = 3      # consecutive losses before tighter filter
    ADAPT_STOP         = 6      # consecutive losses before pausing entirely
    ADAPT_STAKE_FLOOR  = 0.25   # min stake fraction (e.g. 0.25 = 25% of normal)

    # ---- ATR volatility filter (works in backtesting, no Trade API needed) -
    # High ATR = market panic/chaos = Fibonacci setups get stop-hunted
    # When ATR > ATR_MA * ATR_MULT → skip all entries (low_vol gate)
    ATR_FILTER  = False   # disabled by default
    ATR_MULT    = 1.5     # ATR threshold multiplier (e.g. 1.5 = 50% above average)

    # ---- Split TP (F.12): partial close at TP1, remainder at TP2 ----------
    # TP1 (FIB_EXT_PARTIAL) is easier to hit → higher effective WR → less DD
    # TP2 (FIB_EXT) is the original full target → keeps upside on big moves
    SPLIT_TP          = False  # disabled by default; True in split variants
    FIB_EXT_PARTIAL   = 1.0   # TP1 extension level (1.0 = swing range above SH)
    PARTIAL_CLOSE_PCT = 0.5   # fraction of position to close at TP1

    position_adjustment_enable = False  # must be True in split variants

    # ---- F.13: structural DD reduction --------------------------------------
    # BE_AFTER_TP1: once TP1 partial close fires, move SL on the remainder to
    # entry — the trade can no longer turn negative (locks in TP1 profit).
    # RISK_PCT: equal-risk position sizing. Stake is computed so a stop-out
    # loses exactly RISK_PCT of total equity, regardless of SL distance.
    # PAIRLOCK_LOSSES: after N consecutive stop-outs on a pair, lock that pair
    # for PAIRLOCK_DAYS days. PairLocks work in backtesting (unlike Trade API).
    BE_AFTER_TP1    = False
    RISK_PCT        = None    # e.g. 0.0075 = risk 0.75% of equity per trade
    PAIRLOCK_LOSSES = None    # e.g. 3 = lock pair after 3 consecutive losses
    PAIRLOCK_DAYS   = 3

    # Fibonacci ratios
    FIB_50   = 0.500
    FIB_618  = 0.618
    FIB_SL   = 0.786   # stoploss placement (tunable); 0.786 = classical invalidation
    FIB_EXT  = 1.618   # extension TP (same ratio as the golden ratio leg)

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        # Config overrides class-level position_adjustment_enable; restore for split variants.
        if self.SPLIT_TP:
            self.position_adjustment_enable = True
        self._loss_streak: dict = {}   # per-pair consecutive stop-out counter

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

    # ---- Closed-loop helpers -----------------------------------------------

    def _recent_stats(self, pair: Optional[str] = None):
        """
        Review the last ADAPT_LOOKBACK closed trades and return:
          (consec_losses, long_wr, short_wr, pair_consec)
        consec_losses  — consecutive losses across all pairs (most recent first)
        long_wr        — win rate of long  trades in the lookback window
        short_wr       — win rate of short trades in the lookback window
        pair_consec    — consecutive losses for `pair` specifically (0 if pair=None)
        """
        from freqtrade.persistence import Trade

        closed = sorted(
            Trade.get_trades_proxy(is_open=False),
            key=lambda t: t.close_date or datetime.min
        )[-self.ADAPT_LOOKBACK:]

        if not closed:
            return 0, 1.0, 1.0, 0

        # Global consecutive losses (all pairs, most recent first)
        consec = 0
        for t in reversed(closed):
            if t.profit_ratio < 0:
                consec += 1
            else:
                break

        # Win rates by direction
        longs  = [t for t in closed if not t.is_short]
        shorts = [t for t in closed if t.is_short]
        long_wr  = sum(1 for t in longs  if t.profit_ratio >= 0) / len(longs)  if longs  else 1.0
        short_wr = sum(1 for t in shorts if t.profit_ratio >= 0) / len(shorts) if shorts else 1.0

        # Per-pair consecutive losses
        pair_consec = 0
        if pair:
            pair_closed = [t for t in closed if t.pair == pair]
            for t in reversed(pair_closed):
                if t.profit_ratio < 0:
                    pair_consec += 1
                else:
                    break

        return consec, long_wr, short_wr, pair_consec

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float,
                            rate: float, time_in_force: str, entry_tag: Optional[str],
                            side: str, **kwargs) -> bool:
        """
        Closed-loop gate: review recent performance before each new entry.
        Returns False to veto the entry if the system is in a losing streak.
        """
        if not self.ADAPTIVE:
            return True

        consec, long_wr, short_wr, pair_consec = self._recent_stats(pair)

        # Hard stop: too many consecutive losses globally → wait for a win
        if consec >= self.ADAPT_STOP:
            return False

        # Directional pause: if this direction has been losing badly
        dir_wr = long_wr if side == "long" else short_wr
        if dir_wr < 0.05 and len([1]) > 3:   # <5% WR in this direction recently
            return False

        # Tight mode: require stronger RSI/MACD confirmation
        if consec >= self.ADAPT_TIGHT or pair_consec >= self.ADAPT_TIGHT:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is None or df.empty:
                return False

            last = df.iloc[-1]
            rsi      = last.get("rsi", 50)
            macdhist = last.get("macdhist", 0)

            if side == "long":
                # Require RSI above midline AND MACD histogram positive
                if rsi < 50 or macdhist <= 0:
                    return False
            else:  # short
                if rsi > 50 or macdhist >= 0:
                    return False

        return True

    def custom_stake_amount(self, pair: str, current_time: datetime,
                            current_rate: float, proposed_stake: float,
                            min_stake: Optional[float], max_stake: float,
                            leverage: float, entry_tag: Optional[str],
                            side: str, **kwargs) -> float:
        """
        F.13 equal-risk sizing: when RISK_PCT is set, size the stake so that a
        stop-out at the fib SL loses exactly RISK_PCT of total equity.
          stake = equity * RISK_PCT / sl_distance
        Tight SL → bigger stake; wide SL → smaller stake. Every loss costs the
        same fraction of the account, which caps streak damage mathematically.
        """
        stake = proposed_stake

        if self.RISK_PCT is not None:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is not None and not df.empty:
                sig = df[df["date"] < current_time]
                if not sig.empty:
                    s_col = "s_fib_stop" if side == "short" else "fib_stop"
                    stop  = sig.iloc[-1].get(s_col)
                    if stop is not None and not pd.isna(stop) and current_rate > 0:
                        sl_dist = abs(current_rate - float(stop)) / current_rate
                        if sl_dist > 0.001:
                            try:
                                equity = self.wallets.get_total_stake_amount()
                            except Exception:
                                equity = proposed_stake
                            stake = equity * self.RISK_PCT / sl_dist

        # Legacy adaptive scaling (off by default; Trade API is a no-op in backtests)
        if self.ADAPTIVE:
            consec, _, _, _ = self._recent_stats()
            if consec > 0:
                stake *= max(self.ADAPT_STAKE_FLOOR, 1.0 - consec * 0.15)

        if min_stake is not None:
            stake = max(stake, min_stake)
        return min(stake, max_stake)

    def confirm_trade_exit(self, pair: str, trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs) -> bool:
        """
        F.13 pair cooldown: track consecutive stop-outs per pair in instance
        state (works in backtesting, unlike Trade.get_trades_proxy). After
        PAIRLOCK_LOSSES consecutive losses, lock the pair for PAIRLOCK_DAYS.
        """
        if self.PAIRLOCK_LOSSES:
            try:
                profit = trade.calc_profit_ratio(rate)
            except Exception:
                profit = 0.0
            if profit < 0:
                streak = self._loss_streak.get(pair, 0) + 1
                self._loss_streak[pair] = streak
                if streak >= self.PAIRLOCK_LOSSES:
                    self.lock_pair(
                        pair,
                        until=current_time + timedelta(days=self.PAIRLOCK_DAYS),
                        reason="loss_streak_cooldown",
                    )
                    self._loss_streak[pair] = 0
            else:
                self._loss_streak[pair] = 0
        return True

    # ---- Split TP partial close -------------------------------------------

    def adjust_trade_position(self, trade, current_time: datetime,
                              current_rate: float, current_profit: float,
                              min_stake: Optional[float], max_stake: float,
                              current_entry_rate: float, current_exit_rate: float,
                              current_entry_profit: float, current_exit_profit: float,
                              **kwargs) -> Optional[float]:
        """
        Close PARTIAL_CLOSE_PCT of the position when price reaches TP1 (FIB_EXT_PARTIAL).
        The remaining position is exited by custom_exit at TP2 (FIB_EXT).
        Only active when SPLIT_TP = True.
        """
        if not self.SPLIT_TP:
            return None
        # A previous partial exit == TP1 already taken. nr_of_successful_exits
        # is real trade state — reliable in backtesting, unlike custom_data.
        if trade.nr_of_successful_exits > 0:
            return None
        if trade.get_custom_data("tp1_hit", default=False):
            return None

        df, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
        if df is None or df.empty:
            return None
        sig = df[df["date"] < trade.open_date_utc]
        if sig.empty:
            return None

        t1_col  = "s_fib_target1" if trade.is_short else "fib_target1"
        target1 = sig.iloc[-1].get(t1_col)
        if target1 is None or pd.isna(float(target1)):
            return None
        target1 = float(target1)

        hit = (trade.is_short     and current_rate <= target1) or \
              (not trade.is_short and current_rate >= target1)
        if not hit:
            return None

        trade.set_custom_data("tp1_hit", True)
        close_amount = trade.stake_amount * self.PARTIAL_CLOSE_PCT
        if min_stake and close_amount < min_stake:
            return None
        return -close_amount

    # ---- Indicators --------------------------------------------------------

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]

        # 1H momentum (for 'confirm' mode entry filter)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        _macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macdhist"] = _macd["macdhist"]

        # ATR volatility regime (for ATR_FILTER mode)
        dataframe["atr14"]    = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_ma50"] = dataframe["atr14"].rolling(50).mean()
        # low_vol = True when current ATR is within normal range (not panic/chaos)
        dataframe["low_vol"]  = (
            dataframe["atr14"] < dataframe["atr_ma50"] * self.ATR_MULT
        ).astype(int)

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
        dataframe["fib_target"]  = H_u + self.FIB_EXT         * rng_u  # TP2 (full)
        dataframe["fib_target1"] = H_u + self.FIB_EXT_PARTIAL * rng_u  # TP1 (partial)

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
        dataframe["s_fib_stop"]    = (L_d + self.FIB_SL * rng_d) * 1.001            # SL above 0.786
        dataframe["s_fib_target"]  = L_d - (self.FIB_EXT         - 1.0) * rng_d   # TP2 (full)
        dataframe["s_fib_target1"] = L_d - (self.FIB_EXT_PARTIAL - 1.0) * rng_d   # TP1 (partial)

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

        # ATR volatility gate: skip entries when market is in panic/chaos regime
        low_vol = (dataframe["low_vol"] == 1) if self.ATR_FILTER else pd.Series(
            True, index=dataframe.index
        )

        if self.ENTRY_MODE == "confirm":
            rsi_up   = dataframe["rsi"] > dataframe["rsi"].shift(1)
            hist_up  = dataframe["macdhist"] > dataframe["macdhist"].shift(1)
            rsi_down = dataframe["rsi"] < dataframe["rsi"].shift(1)
            hist_down= dataframe["macdhist"] < dataframe["macdhist"].shift(1)

            dataframe.loc[in_zone_long  & trend_up   & rsi_up   & hist_up   & low_vol, "enter_long"]  = 1
            dataframe.loc[in_zone_short & trend_down & rsi_down & hist_down & low_vol, "enter_short"] = 1
        else:
            dataframe.loc[in_zone_long  & trend_up   & low_vol, "enter_long"]  = 1
            dataframe.loc[in_zone_short & trend_down & low_vol, "enter_short"] = 1

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
                        current_rate: float, current_profit: float, **kwargs):
        # NOTE on convention: stoploss_from_absolute returns a POSITIVE
        # distance from current_rate — SMALLER value = TIGHTER stop.
        # F.14b root cause: max(be_sl, sl) always picked the WIDER fib stop,
        # so no breakeven variant (F.7, F.13, F.14) ever fired. Return the
        # breakeven distance directly; freqtrade core never widens an
        # existing stop, so no comparison is needed.
        target, stop = self._trade_levels(trade)
        if stop is None or current_rate <= 0:
            return None   # keep current stop

        # F.13: after TP1 partial close, promote stop to breakeven — the
        # remaining half can no longer turn the trade negative.
        if self.BE_AFTER_TP1 and self.SPLIT_TP and trade.nr_of_successful_exits > 0:
            be_price = trade.open_rate * (1.001 if trade.is_short else 0.999)
            return stoploss_from_absolute(be_price, current_rate, is_short=trade.is_short)

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
                return stoploss_from_absolute(be_price, current_rate, is_short=trade.is_short)

        return stoploss_from_absolute(stop, current_rate, is_short=trade.is_short)

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


# ---- Phase F.11: ATR volatility filter (closed-loop via market structure) ---
# F.10 finding: confirm_trade_entry Trade API doesn't update in backtesting.
# Root cause of consecutive losses: high ATR periods (panic/whipsaw) → stop hunts.
# ATR filter computes directly from OHLCV — guaranteed to work in backtesting.
#
# When ATR(14) > ATR_MA(50) × ATR_MULT → market is chaotic → skip entries.
# This breaks the consecutive-loss cycle at the source, not the symptom.
#
# Variants:
#   FiboSC14w14       — control: no filter (F.9 baseline)
#   FiboSC14w14v15    — ATR_MULT=1.5 (skip when ATR > 150% of average)
#   FiboSC14w14v12    — ATR_MULT=1.2 (tighter: skip when ATR > 120% of average)
#   FiboSC14w14v20    — ATR_MULT=2.0 (looser: only skip extreme panic)

class FiboSC14w14(FiboRetrace):
    """Control: no ATR filter. F.9 baseline."""
    TREND_MODE   = "macro200"
    ENTRY_MODE   = "confirm"
    FIB_EXT      = 1.4
    SWING_WINDOW = 14
    FIB_SL       = 0.786
    ATR_FILTER   = False


class FiboSC14w14v15(FiboRetrace):
    """ATR filter: skip entries when ATR > 1.5× 50-bar average."""
    TREND_MODE   = "macro200"
    ENTRY_MODE   = "confirm"
    FIB_EXT      = 1.4
    SWING_WINDOW = 14
    FIB_SL       = 0.786
    ATR_FILTER   = True
    ATR_MULT     = 1.5


class FiboSC14w14v12(FiboRetrace):
    """ATR filter: skip entries when ATR > 1.2× 50-bar average (tighter)."""
    TREND_MODE   = "macro200"
    ENTRY_MODE   = "confirm"
    FIB_EXT      = 1.4
    SWING_WINDOW = 14
    FIB_SL       = 0.786
    ATR_FILTER   = True
    ATR_MULT     = 1.2


class FiboSC14w14v20(FiboRetrace):
    """ATR filter: skip entries when ATR > 2.0× 50-bar average (looser)."""
    TREND_MODE   = "macro200"
    ENTRY_MODE   = "confirm"
    FIB_EXT      = 1.4
    SWING_WINDOW = 14
    FIB_SL       = 0.786
    ATR_FILTER   = True
    ATR_MULT     = 2.0


# ---- Phase F.12: Split TP (partial close at TP1=1.0×, remainder at TP2=1.4×) ---
# Root cause of high DD: low WR (~17%) means 5-6 consecutive losses are common.
# Split TP: close 50% at 1.0× extension (easier hit → higher partial WR ~35-40%).
# Remaining 50% still targets 1.4× — keeps the R:R upside on big moves.
# Expected effect: WR_effective rises → consecutive loss streaks shorten → DD falls.
#
# F.12 control: FiboSC14w14 (unchanged, for comparison)
#
# Variants:
#   FiboSC14w14sp   — split TP only (no ATR filter)
#   FiboSC14w14sp15 — split TP + ATR×1.5 filter (combine best improvements)

class FiboSC14w14sp(FiboRetrace):
    """F.12: Split TP — 50% closed at 1.0× extension, remaining 50% at 1.4×."""
    TREND_MODE   = "macro200"
    ENTRY_MODE   = "confirm"
    FIB_EXT      = 1.4
    FIB_EXT_PARTIAL   = 1.0
    SWING_WINDOW = 14
    FIB_SL       = 0.786
    ATR_FILTER   = False
    SPLIT_TP     = True
    PARTIAL_CLOSE_PCT = 0.5
    position_adjustment_enable = True


class FiboSC14w14sp15(FiboRetrace):
    """F.12: Split TP + ATR×1.5 filter (combines F.11v15 + F.12 split TP)."""
    TREND_MODE   = "macro200"
    ENTRY_MODE   = "confirm"
    FIB_EXT      = 1.4
    FIB_EXT_PARTIAL   = 1.0
    SWING_WINDOW = 14
    FIB_SL       = 0.786
    ATR_FILTER   = True
    ATR_MULT     = 1.5
    SPLIT_TP     = True
    PARTIAL_CLOSE_PCT = 0.5
    position_adjustment_enable = True


# ---- Phase F.13: structural DD reduction (target DD < 12%) -------------------
# F.12 result: split TP raised WR (19→25% OOS) and cut OOS DD to 28%, but DD is
# still dominated by (a) post-TP1 give-backs, (b) stake size >> risk budget,
# (c) 16-loss streaks. Three fixes, layered on the best F.12 variant (sp15):
#
#   1. BE_AFTER_TP1  — once TP1 fires, SL moves to entry: trade can't turn red.
#   2. RISK_PCT      — equal-risk sizing: every stop-out costs the same % of
#                      equity. 16-loss streak × 0.75% ≈ 12% DD by construction.
#   3. PAIRLOCK      — 3 consecutive stop-outs on a pair → 3-day cooldown.
#
# Variants:
#   FiboSC14w14sp15 — F.12 best (control)
#   FiboF13be       — sp15 + breakeven after TP1
#   FiboF13r75      — F13be + equal-risk sizing 0.75%/trade
#   FiboF13full     — F13r75 + pairlock(3 losses → 3 days)

class FiboF13be(FiboSC14w14sp15):
    """F.13a: split TP + ATR + breakeven-after-TP1."""
    BE_AFTER_TP1 = True


class FiboF13r75(FiboF13be):
    """F.13b: + equal-risk position sizing, 0.75% of equity per trade."""
    RISK_PCT = 0.0075


class FiboF13full(FiboF13r75):
    """F.13c: + pair cooldown after 3 consecutive stop-outs."""
    PAIRLOCK_LOSSES = 3
    PAIRLOCK_DAYS   = 3


# ---- Phase F.14: risk curve with working breakeven ---------------------------
# F.13 findings:
#   - Equal-risk sizing works exactly as designed (DD 28→16% at 0.75%/trade).
#   - BE_AFTER_TP1 never fired: custom_data writes from adjust_trade_position
#     were not visible in custom_stoploss during backtesting. Fixed by checking
#     trade.nr_of_successful_exits instead (real trade state).
#   - Pairlock dropped: locking after streaks skips the rare big winners that
#     fund the system (IS profit collapsed from 19.9% to 1.3%).
#
# F.14 sweeps RISK_PCT with the fixed BE to find max profit at DD < 12%:
#   FiboSC14w14sp15 — no risk sizing, no BE (F.12 baseline)
#   FiboF13be       — BE only (isolates the BE fix effect)
#   FiboF13r75      — BE + 0.75%/trade
#   FiboF14r60      — BE + 0.60%/trade (expected DD ≈ 11-13%)
#   FiboF14r100     — BE + 1.00%/trade (profit recovery probe)

class FiboF14r60(FiboF13be):
    """F.14: breakeven-after-TP1 + equal-risk 0.60% per trade."""
    RISK_PCT = 0.006


class FiboF14r100(FiboF13be):
    """F.14: breakeven-after-TP1 + equal-risk 1.00% per trade."""
    RISK_PCT = 0.010


# ---- Phase F.15: diversification + risk curve (no breakeven) -----------------
# F.14b verdict: BE-after-TP1 fires correctly but costs more than it saves —
# the big TP2 runners often dip below entry before continuing (IS profit
# 28.4→18.5%). BE permanently dropped.
#
# Profit lever that doesn't raise DD: more opportunities. Whitelist grows to
# 6 pairs (BTC/ETH/SOL/XRP/DOGE/ADA), max_open_trades 2→4. Equal-risk sizing
# keeps every stop-out at a fixed % of equity, so DD stays bounded while
# trade count (and compounding) roughly doubles.
#
# Variants (all split TP + ATR×1.5, NO breakeven):
#   FiboSC14w14sp15 — unlimited stake (control)
#   FiboF15r50      — 0.50%/trade
#   FiboF15r60      — 0.60%/trade
#   FiboF15r75      — 0.75%/trade

class FiboF15r50(FiboSC14w14sp15):
    """F.15: equal-risk 0.50% per trade, no breakeven."""
    RISK_PCT = 0.005


class FiboF15r60(FiboSC14w14sp15):
    """F.15: equal-risk 0.60% per trade, no breakeven."""
    RISK_PCT = 0.006


class FiboF15r75(FiboSC14w14sp15):
    """F.15: equal-risk 0.75% per trade, no breakeven."""
    RISK_PCT = 0.0075
