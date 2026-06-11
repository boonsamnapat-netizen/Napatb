"""
Confluence -- three research-backed confluence systems, head-to-head (Phase C.1)

Built from agent research + 16 phases of empirical lessons:
  - one indicator per ROLE (trend / timing / location / participation),
    no collinear stacking (RSI~Stoch~MACD double-counting)
  - tight structural stops + R-multiple targets; split TP raises WR
  - equal-risk sizing 0.5%/trade keeps DD bounded (proven F.13-F.16)
  - NO breakeven promotion (proven harmful), NO 1h mean reversion (dead)
  - wide static universe + strict entries + max_open_trades = the "scan"
    (backtestable without survivorship bias; live uses VolumePairList)

A) HolyGrail     -- Raschke: 4H ADX trend + EMA20 pullback into Fib zone
                    + volume dry-up + 1H trigger-bar break.  WR~45-55% target
B) TripleScreen  -- Elder: 4H MACD tide + 1H Stoch wave + breakout trigger
C) DonchianBreak -- Turtle: 4H 20-bar channel break + 1D regime + rel volume
"""

import numpy as np
import pandas as pd
from freqtrade.strategy import IStrategy, merge_informative_pair, stoploss_from_absolute
import talib.abstract as ta
from pandas import DataFrame
from datetime import datetime
from typing import Optional


class ConfluenceBase(IStrategy):
    INTERFACE_VERSION = 3

    timeframe     = "1h"
    inf_timeframe = "4h"

    minimal_roi         = {"0": 100.0}
    stoploss            = -0.15          # catastrophe net; custom stop is tighter
    use_custom_stoploss = True
    trailing_stop       = False

    process_only_new_candles   = True
    use_exit_signal            = True
    exit_profit_only           = False
    ignore_roi_if_entry_signal = False
    startup_candle_count       = 900
    can_short                  = True

    position_adjustment_enable = True    # split TP partial closes

    # ---- shared risk framework ----------------------------------------------
    SL_ATR   = 1.5      # stop = SL_ATR x ATR(14,1h) at entry
    TP1_R    = 1.5      # close TP1_PCT of position at this R-multiple (None=off)
    TP2_R    = 3.0      # close remainder here
    TP1_PCT  = 0.5
    RISK_PCT = 0.005    # equal-risk sizing: each stop-out = 0.5% of equity
    DC_ENTRY = 20       # Donchian entry channel (4H bars)
    DC_EXIT  = 10       # Donchian exit channel (4H bars)

    # ---- anti-lookahead fractal pivots (proven in FiboRetrace) ---------------

    @staticmethod
    def _fractal_pivots(high: np.ndarray, low: np.ndarray, w: int):
        n  = len(high)
        ph = np.zeros(n, dtype=bool)
        pl = np.zeros(n, dtype=bool)
        for i in range(w, n - w):
            seg_h = high[i - w:i + w + 1]
            seg_l = low [i - w:i + w + 1]
            if high[i] == seg_h.max() and (seg_h == high[i]).sum() == 1:
                ph[i] = True
            if low[i] == seg_l.min() and (seg_l == low[i]).sum() == 1:
                pl[i] = True
        return ph, pl

    @staticmethod
    def _carry_confirmed(piv: np.ndarray, vals: np.ndarray, w: int):
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

    # ---- shared indicator scaffold -------------------------------------------

    def _base_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]

        dataframe["atr14"]     = ta.ATR(dataframe, timeperiod=14)
        dataframe["vol_sma20"] = dataframe["volume"].rolling(20).mean()
        dataframe["rel_vol"]   = dataframe["volume"] / dataframe["vol_sma20"]

        inf = self.dp.get_pair_dataframe(pair, self.inf_timeframe)
        inf["ema20"]  = ta.EMA(inf, timeperiod=20)
        inf["ema50"]  = ta.EMA(inf, timeperiod=50)
        inf["atr14"]  = ta.ATR(inf, timeperiod=14)
        inf["adx"]    = ta.ADX(inf, timeperiod=14)
        inf["plus_di"]  = ta.PLUS_DI(inf, timeperiod=14)
        inf["minus_di"] = ta.MINUS_DI(inf, timeperiod=14)
        _m = ta.MACD(inf, fastperiod=12, slowperiod=26, signalperiod=9)
        inf["macdhist"] = _m["macdhist"]
        inf["hist_rising"]  = (inf["macdhist"] > inf["macdhist"].shift(1)).astype(int)
        inf["hist_falling"] = (inf["macdhist"] < inf["macdhist"].shift(1)).astype(int)
        # Donchian on COMPLETED bars only (shift(1) = no lookahead)
        inf["dc_entry_high"] = inf["high"].rolling(self.DC_ENTRY).max().shift(1)
        inf["dc_entry_low"]  = inf["low"].rolling(self.DC_ENTRY).min().shift(1)
        inf["dc_exit_high"]  = inf["high"].rolling(self.DC_EXIT).max().shift(1)
        inf["dc_exit_low"]   = inf["low"].rolling(self.DC_EXIT).min().shift(1)
        # ATR SMA for volatility-expansion regime gate (C.3)
        inf["atr14_sma20"]   = inf["atr14"].rolling(20).mean()
        dataframe = merge_informative_pair(
            dataframe, inf, self.timeframe, self.inf_timeframe, ffill=True
        )

        infd = self.dp.get_pair_dataframe(pair, "1d")
        infd["ema50"]  = ta.EMA(infd, timeperiod=50)
        infd["ema200"] = ta.EMA(infd, timeperiod=200)
        dataframe = merge_informative_pair(
            dataframe, infd, self.timeframe, "1d", ffill=True
        )
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    # ---- shared exits: structural ATR stop + R-multiple split TP --------------

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

    def _r_dist(self, trade) -> Optional[float]:
        atr = self._entry_atr(trade)
        if atr is None:
            return None
        return self.SL_ATR * atr   # 1R in price units

    def custom_stoploss(self, pair, trade, current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs):
        r = self._r_dist(trade)
        if r is None or current_rate <= 0:
            return None
        stop_price = trade.open_rate + r if trade.is_short else trade.open_rate - r
        return stoploss_from_absolute(stop_price, current_rate, is_short=trade.is_short)

    def adjust_trade_position(self, trade, current_time: datetime,
                              current_rate: float, current_profit: float,
                              min_stake: Optional[float], max_stake: float,
                              current_entry_rate: float, current_exit_rate: float,
                              current_entry_profit: float, current_exit_profit: float,
                              **kwargs) -> Optional[float]:
        if self.TP1_R is None or trade.nr_of_successful_exits > 0:
            return None
        r = self._r_dist(trade)
        if r is None:
            return None
        tp1 = (trade.open_rate - self.TP1_R * r) if trade.is_short \
              else (trade.open_rate + self.TP1_R * r)
        hit = (current_rate <= tp1) if trade.is_short else (current_rate >= tp1)
        if not hit:
            return None
        close_amount = trade.stake_amount * self.TP1_PCT
        if min_stake and close_amount < min_stake:
            return None
        return -close_amount

    def custom_exit(self, pair, trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        if self.TP2_R is None:
            return None
        r = self._r_dist(trade)
        if r is None:
            return None
        tp2 = (trade.open_rate - self.TP2_R * r) if trade.is_short \
              else (trade.open_rate + self.TP2_R * r)
        hit = (current_rate <= tp2) if trade.is_short else (current_rate >= tp2)
        return "tp2_target" if hit else None

    def custom_stake_amount(self, pair: str, current_time: datetime,
                            current_rate: float, proposed_stake: float,
                            min_stake: Optional[float], max_stake: float,
                            leverage: float, entry_tag: Optional[str],
                            side: str, **kwargs) -> float:
        if self.RISK_PCT is None:
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


# ============================================================================
# A) HolyGrail — Raschke: ADX trend + EMA20 pullback + Fib zone + volume dry-up
# ============================================================================

class HolyGrail(ConfluenceBase):
    """
    Roles: ADX(4H)=trend strength, EMA20(4H)+Fib 38.2-61.8=location,
    volume<SMA20=corrective pullback, 1H trigger-bar break=timing.
    Raschke stand-aside rule: price through EMA50(4H) = trend broken, no entry.
    """
    ADX_MIN      = 25
    SWING_WINDOW = 12     # 1h fractal half-width for the impulse leg
    FIB_MIN      = 0.382
    FIB_MAX      = 0.618

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self._base_indicators(dataframe, metadata)

        w  = self.SWING_WINDOW
        hi = dataframe["high"].values
        lo = dataframe["low"].values
        ph, pl = self._fractal_pivots(hi, lo, w)
        sh_val, sh_idx = self._carry_confirmed(ph, hi, w)
        sl_val, sl_idx = self._carry_confirmed(pl, lo, w)

        up_leg   = ~np.isnan(sh_idx) & ~np.isnan(sl_idx) & (sh_idx > sl_idx) & (sh_val > sl_val)
        down_leg = ~np.isnan(sh_idx) & ~np.isnan(sl_idx) & (sl_idx > sh_idx) & (sl_val < sh_val)
        rng = sh_val - sl_val
        with np.errstate(invalid="ignore", divide="ignore"):
            # long: depth of pullback from swing high; short: bounce from swing low
            dataframe["retr_long"]  = np.where(up_leg & (rng > 0), (sh_val - lo) / rng, np.nan)
            dataframe["retr_short"] = np.where(down_leg & (rng > 0), (hi - sl_val) / rng, np.nan)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf  = self.inf_timeframe
        live = dataframe["volume"] > 0
        adx_ok = dataframe[f"adx_{itf}"] > self.ADX_MIN

        long_di    = dataframe[f"plus_di_{itf}"] > dataframe[f"minus_di_{itf}"]
        touch_long = (dataframe["low"] <= dataframe[f"ema20_{itf}"]) & \
                     (dataframe["close"] >= dataframe[f"ema20_{itf}"] * 0.99)
        fib_long   = dataframe["retr_long"].between(self.FIB_MIN, self.FIB_MAX)
        vol_dry    = dataframe["volume"] < dataframe["vol_sma20"]
        trigger_l  = dataframe["close"] > dataframe["high"].shift(1)
        intact_l   = dataframe["close"] > dataframe[f"ema50_{itf}"]

        dataframe.loc[
            live & adx_ok & long_di & touch_long & fib_long & vol_dry & trigger_l & intact_l,
            "enter_long",
        ] = 1

        short_di    = dataframe[f"minus_di_{itf}"] > dataframe[f"plus_di_{itf}"]
        touch_short = (dataframe["high"] >= dataframe[f"ema20_{itf}"]) & \
                      (dataframe["close"] <= dataframe[f"ema20_{itf}"] * 1.01)
        fib_short   = dataframe["retr_short"].between(self.FIB_MIN, self.FIB_MAX)
        trigger_s   = dataframe["close"] < dataframe["low"].shift(1)
        intact_s    = dataframe["close"] < dataframe[f"ema50_{itf}"]

        dataframe.loc[
            live & adx_ok & short_di & touch_short & fib_short & vol_dry & trigger_s & intact_s,
            "enter_short",
        ] = 1
        return dataframe


# ============================================================================
# B) TripleScreen — Elder: 4H MACD tide + 1H Stoch wave + breakout trigger
# ============================================================================

class TripleScreen(ConfluenceBase):
    """
    Roles: MACD hist(4H)=tide, Stoch(1H) reset+recross=wave,
    break of prior 1H extreme=third-screen entry trigger.
    Chop mitigation: ADX(4H) > 20 (the standard fix for screen-one whipsaw).
    """
    ADX_MIN    = 20
    STO_LOW    = 30
    STO_HIGH   = 70
    CROSS_LOOK = 3     # trigger valid within N bars of the stoch recross

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self._base_indicators(dataframe, metadata)
        sto = ta.STOCH(dataframe, fastk_period=14, slowk_period=3, slowk_matype=0,
                       slowd_period=3, slowd_matype=0)
        dataframe["sto_k"] = sto["slowk"]

        k = dataframe["sto_k"]
        cross_up   = (k > self.STO_LOW) & (k.shift(1) <= self.STO_LOW)
        cross_down = (k < self.STO_HIGH) & (k.shift(1) >= self.STO_HIGH)
        dataframe["sto_up_recent"]   = cross_up.rolling(self.CROSS_LOOK).max().fillna(0)
        dataframe["sto_down_recent"] = cross_down.rolling(self.CROSS_LOOK).max().fillna(0)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf  = self.inf_timeframe
        live = dataframe["volume"] > 0
        adx_ok = dataframe[f"adx_{itf}"] > self.ADX_MIN

        tide_up   = (dataframe[f"hist_rising_{itf}"] == 1) & \
                    (dataframe["close"] > dataframe[f"ema50_{itf}"])
        tide_down = (dataframe[f"hist_falling_{itf}"] == 1) & \
                    (dataframe["close"] < dataframe[f"ema50_{itf}"])

        dataframe.loc[
            live & adx_ok & tide_up
            & (dataframe["sto_up_recent"] == 1)
            & (dataframe["close"] > dataframe["high"].shift(1)),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & adx_ok & tide_down
            & (dataframe["sto_down_recent"] == 1)
            & (dataframe["close"] < dataframe["low"].shift(1)),
            "enter_short",
        ] = 1
        return dataframe


# ============================================================================
# C) DonchianBreak — Turtle: 4H channel break + 1D regime + relative volume
# ============================================================================

class DonchianBreak(ConfluenceBase):
    """
    Roles: Donchian 20(4H)=lookahead-free S/R break, 1D EMA50/200=regime,
    rel volume >= 1.5x = participation (the documented false-breakout filter).
    Exit: opposite 10-bar channel (no fixed TP — let the fat tail run),
    2xATR stop. The fat-tail catcher / diversifier of the trio.
    """
    SL_ATR  = 2.0
    TP1_R   = None     # no split TP — channel exit rides the trend
    TP2_R   = None
    REL_VOL = 1.5
    ADX_MIN = None     # optional chop filter: require ADX(4H) above this

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return self._base_indicators(dataframe, metadata)

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf  = self.inf_timeframe
        live = dataframe["volume"] > 0
        vol_ok  = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d = dataframe["ema50_1d"] > dataframe["ema200_1d"]
        bear_1d = dataframe["ema50_1d"] < dataframe["ema200_1d"]
        adx_ok  = (dataframe[f"adx_{itf}"] > self.ADX_MIN) if self.ADX_MIN \
                  else pd.Series(True, index=dataframe.index)

        dataframe.loc[
            live & vol_ok & bull_1d & adx_ok
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & adx_ok
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        dataframe.loc[dataframe["close"] < dataframe[f"dc_exit_low_{itf}"],  "exit_long"]  = 1
        dataframe.loc[dataframe["close"] > dataframe[f"dc_exit_high_{itf}"], "exit_short"] = 1
        return dataframe


# ---- C.2: Donchian refinement — kill the chop bleed ---------------------------
# C.1: DonchianBreak is the alpha engine (IS +61%, OOS bear +149%) but bleeds
# in chop (holdout -15%, the documented Turtle failure mode). Standard
# mitigations under test: ADX trend-strength gate and the slower Turtle
# System-2 channel (55-entry / 20-exit) that ignores minor range breaks.

class DCadx22(DonchianBreak):
    """20/10 channel + ADX(4H) > 22 chop gate."""
    ADX_MIN = 22


class DC55(DonchianBreak):
    """Turtle System 2: 55-bar entry / 20-bar exit, no ADX gate."""
    DC_ENTRY = 55
    DC_EXIT  = 20


class DC55adx(DonchianBreak):
    """55/20 channel + ADX(4H) > 22 — both mitigations combined."""
    DC_ENTRY = 55
    DC_EXIT  = 20
    ADX_MIN  = 22


# ---- C.3: Regime filters — fix holdout chop bleed on DC55 --------------------
# All C.2 variants bled 11-15% in Holdout (Mar-Jun 2025 choppy/corrective).
# Root cause: entries during broken trending regime (price below EMA50_1d even
# though golden cross still held) and during low-volatility consolidation chop.
# C.3 tests two regime gates layered onto the best C.2 variant (DC55):
#   EMA  — coin 1D close > EMA50_1d  (price above medium-term trend)
#   ATR  — 4H ATR > SMA20(ATR,4H)    (volatility expansion = real breakout)
#   combo— both gates together        (most conservative)

class DC55ema(DC55):
    """
    DC55 + require 1D close > EMA50(1D) at entry.
    Blocks entries during corrective pullbacks where golden cross still active
    but price has already broken below its medium-term MA.
    """
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live    = dataframe["volume"] > 0
        vol_ok  = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d = (dataframe["ema50_1d"] > dataframe["ema200_1d"]) \
                & (dataframe["close_1d"]  > dataframe["ema50_1d"])
        bear_1d = (dataframe["ema50_1d"] < dataframe["ema200_1d"]) \
                & (dataframe["close_1d"]  < dataframe["ema50_1d"])
        dataframe.loc[
            live & vol_ok & bull_1d
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


class DC55atr(DC55):
    """
    DC55 + ATR(4H) > SMA20(ATR,4H) at entry.
    Requires volatility expansion before entering: breakouts on expanding ATR
    are genuine momentum moves; breakouts on contracting ATR are choppy fakes.
    """
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live       = dataframe["volume"] > 0
        vol_ok     = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d    = dataframe["ema50_1d"] > dataframe["ema200_1d"]
        bear_1d    = dataframe["ema50_1d"] < dataframe["ema200_1d"]
        atr_expand = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]
        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


class DC55combo(DC55):
    """
    DC55 + 1D close > EMA50(1D) AND ATR expansion — both regime gates together.
    Most conservative: fewer trades but highest probability of trending environment.
    """
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live       = dataframe["volume"] > 0
        vol_ok     = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d    = (dataframe["ema50_1d"] > dataframe["ema200_1d"]) \
                   & (dataframe["close_1d"]  > dataframe["ema50_1d"])
        bear_1d    = (dataframe["ema50_1d"] < dataframe["ema200_1d"]) \
                   & (dataframe["close_1d"]  < dataframe["ema50_1d"])
        atr_expand = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]
        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


# ---- C.4 v2: BTC weekly macro regime + tighter volume gate ------------------
# Analysis of DC55combo weakness:
#   1. All 31 pairs are BTC-correlated — when BTC breaks out, all altcoins
#      signal together. max_open_trades=4 fills randomly, not by quality.
#   2. Crypto longs during BTC macro bear still bleed despite daily EMA filter.
#
# Fix:
#   A) vol ≥ 2.0× average (was 1.5×) — select only top-quality breakouts,
#      naturally filters correlation noise (only strongest signals fire)
#   B) BTC 1W close > EMA50(1W) required for longs — pure macro regime gate.
#      A breakout during BTC weekly downtrend is far more likely to be a trap.

class DC55v2(DC55combo):
    """
    DC55combo + BTC weekly macro gate + tighter volume quality filter.
    Enters only when: (1) BTC weekly trend confirmed, (2) volume is a genuine
    2× surge above average — filters correlation-driven fake breakouts.
    """
    REL_VOL = 2.0   # was 1.5 — top-quality breakouts only

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self._base_indicators(dataframe, metadata)
        # BTC 1W macro regime — always BTC regardless of which pair we're on
        btc_w = self.dp.get_pair_dataframe("BTC/USDT:USDT", "1w")
        btc_w["ema50"] = ta.EMA(btc_w, timeperiod=50)
        dataframe = merge_informative_pair(
            dataframe, btc_w, self.timeframe, "1w", ffill=True
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live       = dataframe["volume"] > 0
        vol_ok     = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d    = (dataframe["ema50_1d"] > dataframe["ema200_1d"]) \
                   & (dataframe["close_1d"]  > dataframe["ema50_1d"])
        bear_1d    = (dataframe["ema50_1d"] < dataframe["ema200_1d"]) \
                   & (dataframe["close_1d"]  < dataframe["ema50_1d"])
        atr_expand = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]
        # BTC weekly macro regime gate
        btc_bull_w = dataframe["close_1w"] > dataframe["ema50_1w"]
        btc_bear_w = dataframe["close_1w"] < dataframe["ema50_1w"]

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & btc_bull_w
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & btc_bear_w
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


# ---- C.5 experiments (v2): fixed slope bug + weekly ATR expansion filter
#
# DC55v3 fix: C.5 run showed DC55v3 == DC55v2 — bug was shift(4) on ffilled
#   hourly column = 4 hours not 4 weeks. Fix: compute slope on btc_w BEFORE
#   merge_informative_pair so diff(4) works on actual weekly candles.
#
# DC55v4 redesign: neutral-zone approach backfired — 2025-Q2 neutral BTC
#   produced WORSE trades (+8 entries, win% 21% vs 36%). Neutral zone IS bad.
#   New idea: weekly ATR expansion gate — enter only when BTC weekly volatility
#   is expanding (ATR14 > SMA20 on weekly). Filters choppy sideways regardless
#   of direction; keeps the binary level filter from DC55v2.

class DC55v3(DC55v2):
    """
    DC55v2 with slope-based BTC weekly filter — fixed: slope computed on
    weekly candles before merge, not on ffilled hourly column.
    Blocks longs only when BTC EMA50(1W) is actively falling AND price below.
    """

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self._base_indicators(dataframe, metadata)
        btc_w = self.dp.get_pair_dataframe("BTC/USDT:USDT", "1w")
        btc_w["ema50"] = ta.EMA(btc_w, timeperiod=50)
        # slope computed on weekly data — diff(4) = change over 4 real weeks
        btc_w["ema50_slope"] = btc_w["ema50"].diff(4) > 0
        dataframe = merge_informative_pair(
            dataframe, btc_w, self.timeframe, "1w", ffill=True
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live       = dataframe["volume"] > 0
        vol_ok     = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d    = (dataframe["ema50_1d"] > dataframe["ema200_1d"]) \
                   & (dataframe["close_1d"]  > dataframe["ema50_1d"])
        bear_1d    = (dataframe["ema50_1d"] < dataframe["ema200_1d"]) \
                   & (dataframe["close_1d"]  < dataframe["ema50_1d"])
        atr_expand = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]

        ema50_rising = dataframe["ema50_slope_1w"].astype(bool)
        btc_bull_w   = (dataframe["close_1w"] > dataframe["ema50_1w"]) | ema50_rising
        btc_bear_w   = (dataframe["close_1w"] < dataframe["ema50_1w"]) & ~ema50_rising

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & btc_bull_w
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & btc_bear_w
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


class DC55v4(DC55v2):
    """
    DC55v2 + weekly ATR expansion gate.
    C.5 proved neutral-zone trades are losers; binary level filter is correct.
    This variant keeps the binary weekly level gate and adds a second condition:
    BTC weekly ATR(14) > SMA20(ATR14) — volatility must be expanding.
    Filters choppy sideways weeks regardless of BTC level.
    """

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self._base_indicators(dataframe, metadata)
        btc_w = self.dp.get_pair_dataframe("BTC/USDT:USDT", "1w")
        btc_w["ema50"] = ta.EMA(btc_w, timeperiod=50)
        btc_w["atr14"] = ta.ATR(btc_w, timeperiod=14)
        btc_w["atr14_sma20"] = btc_w["atr14"].rolling(20).mean()
        dataframe = merge_informative_pair(
            dataframe, btc_w, self.timeframe, "1w", ffill=True
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live       = dataframe["volume"] > 0
        vol_ok     = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d    = (dataframe["ema50_1d"] > dataframe["ema200_1d"]) \
                   & (dataframe["close_1d"]  > dataframe["ema50_1d"])
        bear_1d    = (dataframe["ema50_1d"] < dataframe["ema200_1d"]) \
                   & (dataframe["close_1d"]  < dataframe["ema50_1d"])
        atr_expand = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]

        # binary weekly level gate (same as DC55v2)
        btc_bull_w = dataframe["close_1w"] > dataframe["ema50_1w"]
        btc_bear_w = dataframe["close_1w"] < dataframe["ema50_1w"]
        # extra: BTC weekly volatility must be expanding (not choppy sideways)
        btc_atr_w  = dataframe["atr14_1w"] > dataframe["atr14_sma20_1w"]

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & btc_bull_w & btc_atr_w
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & btc_bear_w & btc_atr_w
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


# ---- C.6: frequency tuning — test whether REL_VOL=2.0 is over-cutting
#
# DC55v2 OOS: +130%, PF 1.59 — weekly filter is the primary quality gate.
# DC55v2 holdout: only 11 trades (78% slot-idle) — too slow for live use.
#
# Hypothesis: the BTC weekly level filter does the heavy quality lifting.
# REL_VOL=2.0 cuts a further 34% of trades with marginal quality gain.
# Restoring REL_VOL=1.5 + keeping weekly filter may recover frequency
# while preserving the OOS quality advantage over DC55combo.

class DC55v5(DC55v2):
    """
    DC55v2 with REL_VOL restored to 1.5 (DC55combo default).
    Tests whether the BTC weekly macro gate alone is sufficient for quality
    and the 2× volume filter over-cuts valid signals.
    Target: ~20+ trades/month holdout, OOS PF still > 1.4.
    """
    REL_VOL = 1.5
