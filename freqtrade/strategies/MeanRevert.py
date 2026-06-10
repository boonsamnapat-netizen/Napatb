"""
MeanRevert -- Bollinger + RSI(2) mean reversion (1H, Long + Short)
Phase MR: rebuilt from scratch after the Fibo post-mortem (F.1-F.15).

Philosophy: many small, frequent wins instead of rare big runners.
  Long : 4H uptrend   + 1H close below lower BB + RSI(2) deeply oversold
         → buy the dip, exit at the BB midline
  Short: 4H downtrend + 1H close above upper BB + RSI(2) overbought
         → sell the rip, exit at the BB midline

Target profile: WR 55-70%, avg win ~1%, shallow equity dips (DD < 12%).
Regime filter is essential: mean reversion AGAINST the macro trend is how
accounts die in crypto (lesson from the 2021-2022 bear).

Risk: ATR-based stop fixed at entry (positive-distance convention —
stoploss_from_absolute returns a positive number, smaller = tighter).
Time stop kills stale trades so capital recycles into fresh signals.
"""

import pandas as pd
from freqtrade.strategy import IStrategy, merge_informative_pair, stoploss_from_absolute
import talib.abstract as ta
from pandas import DataFrame
from datetime import datetime, timedelta, timezone
from typing import Optional


class MeanRevert(IStrategy):
    INTERFACE_VERSION = 3

    timeframe     = "1h"
    inf_timeframe = "4h"

    minimal_roi         = {"0": 100.0}    # exits handled by signal/stop/time
    stoploss            = -0.15           # safety net; custom_stoploss is tighter
    use_custom_stoploss = True
    trailing_stop       = False

    process_only_new_candles   = True
    use_exit_signal            = True
    exit_profit_only           = False
    ignore_roi_if_entry_signal = False
    startup_candle_count       = 900      # covers 4H EMA200
    can_short                  = True

    # ---- Variant parameters -------------------------------------------------
    BB_WIN       = 20      # Bollinger window (1H bars)
    BB_STD       = 2.0     # Bollinger width
    RSI_LEN      = 2       # short RSI = classic mean-reversion trigger
    RSI_OS       = 10      # oversold threshold (long entry)
    RSI_OB       = 90      # overbought threshold (short entry)
    TREND_FILTER = True    # require 4H EMA200 alignment
    SL_ATR       = 2.5     # stop distance in ATR(14) multiples, fixed at entry
    MAX_HOLD_H   = 48      # time stop: exit if mean not reached in N hours
    EXIT_MODE    = "mid"   # "mid" = BB midline; "rsi" = RSI back through 50

    # Equal-risk sizing (proven in F.13/F.14); None = unlimited stake
    RISK_PCT = None

    # ---- Indicators ----------------------------------------------------------

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]

        # Bollinger bands on close
        mid = ta.SMA(dataframe, timeperiod=self.BB_WIN)
        std = dataframe["close"].rolling(self.BB_WIN).std()
        dataframe["bb_mid"]   = mid
        dataframe["bb_upper"] = mid + self.BB_STD * std
        dataframe["bb_lower"] = mid - self.BB_STD * std

        dataframe["rsi_fast"] = ta.RSI(dataframe, timeperiod=self.RSI_LEN)
        dataframe["atr14"]    = ta.ATR(dataframe, timeperiod=14)

        # 4H macro regime
        inf4h = self.dp.get_pair_dataframe(pair, self.inf_timeframe)
        inf4h["ema200"] = ta.EMA(inf4h, timeperiod=200)
        dataframe = merge_informative_pair(
            dataframe, inf4h, self.timeframe, self.inf_timeframe, ffill=True
        )

        itf = self.inf_timeframe
        up   = dataframe[f"close_{itf}"] > dataframe[f"ema200_{itf}"]
        down = ~up
        if not self.TREND_FILTER:
            up = down = pd.Series(True, index=dataframe.index)
        dataframe["trend_up"]   = up.fillna(False).astype(int)
        dataframe["trend_down"] = down.fillna(False).astype(int)

        return dataframe

    # ---- Entries --------------------------------------------------------------

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        live = dataframe["volume"] > 0

        dataframe.loc[
            live
            & (dataframe["trend_up"] == 1)
            & (dataframe["close"] < dataframe["bb_lower"])
            & (dataframe["rsi_fast"] < self.RSI_OS),
            "enter_long",
        ] = 1

        dataframe.loc[
            live
            & (dataframe["trend_down"] == 1)
            & (dataframe["close"] > dataframe["bb_upper"])
            & (dataframe["rsi_fast"] > self.RSI_OB),
            "enter_short",
        ] = 1

        return dataframe

    # ---- Exits ----------------------------------------------------------------

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if self.EXIT_MODE == "rsi":
            dataframe.loc[dataframe["rsi_fast"] > 50, "exit_long"]  = 1
            dataframe.loc[dataframe["rsi_fast"] < 50, "exit_short"] = 1
        else:  # "mid"
            dataframe.loc[dataframe["close"] > dataframe["bb_mid"], "exit_long"]  = 1
            dataframe.loc[dataframe["close"] < dataframe["bb_mid"], "exit_short"] = 1
        return dataframe

    def custom_exit(self, pair, trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        # Time stop: mean reversion that hasn't reverted is a broken thesis.
        open_utc = trade.open_date_utc
        now = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
        if (now - open_utc) >= timedelta(hours=self.MAX_HOLD_H):
            return "time_stop"
        return None

    # ---- Stop: ATR distance locked at the entry bar ----------------------------

    def _entry_atr(self, trade) -> Optional[float]:
        df, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
        if df is None or df.empty:
            return None
        sig = df[df["date"] < trade.open_date_utc]
        if sig.empty:
            return None
        atr = sig.iloc[-1].get("atr14")
        if atr is None or pd.isna(atr):
            return None
        return float(atr)

    def custom_stoploss(self, pair, trade, current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs):
        # SL_ATR=None → no per-trade stop; only the static config stoploss
        # (-0.15 catastrophe net) applies. MR.1 showed ATR stops sell the
        # exact bottom: stop-outs -396% vs +374% from trades left to revert.
        if self.SL_ATR is None:
            return None
        atr = self._entry_atr(trade)
        if atr is None or current_rate <= 0:
            return None
        if trade.is_short:
            stop_price = trade.open_rate + self.SL_ATR * atr
        else:
            stop_price = trade.open_rate - self.SL_ATR * atr
        return stoploss_from_absolute(stop_price, current_rate, is_short=trade.is_short)

    # ---- Equal-risk sizing (carried over from F.13) -----------------------------

    def custom_stake_amount(self, pair: str, current_time: datetime,
                            current_rate: float, proposed_stake: float,
                            min_stake: Optional[float], max_stake: float,
                            leverage: float, entry_tag: Optional[str],
                            side: str, **kwargs) -> float:
        if self.RISK_PCT is None or self.SL_ATR is None:
            return proposed_stake

        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or df.empty:
            return proposed_stake
        sig = df[df["date"] < current_time]
        if sig.empty:
            return proposed_stake
        atr = sig.iloc[-1].get("atr14")
        if atr is None or pd.isna(atr) or current_rate <= 0:
            return proposed_stake

        sl_dist = (self.SL_ATR * float(atr)) / current_rate
        if sl_dist <= 0.001:
            return proposed_stake
        try:
            equity = self.wallets.get_total_stake_amount()
        except Exception:
            equity = proposed_stake
        stake = equity * self.RISK_PCT / sl_dist

        if min_stake is not None:
            stake = max(stake, min_stake)
        return min(stake, max_stake)


# ---- MR.1 variants -----------------------------------------------------------
# MRv1     — baseline: trend filter, RSI(2)<10/>90, exit at midline, 2.5×ATR stop
# MRnf     — no trend filter (measures how much the regime filter is worth)
# MRr4     — RSI(4) with looser 25/75 thresholds (more signals, weaker trigger)
# MRfast   — tighter risk: 2.0×ATR stop, 24h time stop (faster capital recycling)

class MRv1(MeanRevert):
    """MR.1 baseline."""
    pass


class MRnf(MeanRevert):
    """No 4H regime filter — control for the filter's value."""
    TREND_FILTER = False


class MRr4(MeanRevert):
    """RSI(4) 25/75 — more frequent, less extreme entries."""
    RSI_LEN = 4
    RSI_OS  = 25
    RSI_OB  = 75


class MRfast(MeanRevert):
    """Tighter stop (2.0×ATR) and 24h time stop."""
    SL_ATR     = 2.0
    MAX_HOLD_H = 24


# ---- MR.2: drop the per-trade stop (Connors rule: stops hurt MR) -------------
# MR.1 autopsy: trades allowed to revert won +374% at 91% WR; the 2.5×ATR
# stop sold the exact bottom 703 times for -396%. Risk control moves to the
# time stop + the static -15% catastrophe net + (later) position sizing.
#
#   MR2ns   — no stop, 48h time stop
#   MR2ns72 — no stop, 72h time stop (more room to revert)
#   MR2w5   — wide 5×ATR stop (middle ground control)
#   MR2s5   — no stop + stricter entries RSI(2) <5 / >95 (deeper stretch only)

class MR2ns(MeanRevert):
    """No per-trade stop; 48h time stop."""
    SL_ATR = None


class MR2ns72(MeanRevert):
    """No per-trade stop; 72h time stop."""
    SL_ATR     = None
    MAX_HOLD_H = 72


class MR2w5(MeanRevert):
    """Very wide 5×ATR stop; 48h time stop."""
    SL_ATR = 5.0


class MR2s5(MeanRevert):
    """No stop + deeper stretch required: RSI(2) < 5 / > 95."""
    SL_ATR = None
    RSI_OS = 5
    RSI_OB = 95
