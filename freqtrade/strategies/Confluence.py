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


# ---- C.7: dual timeframe + expanded universe
#
# C.6 proved REL_VOL=2.0 is doing real work — can't lower it.
# Frequency must come from more opportunities, not looser filters.
#
# DC55v6: DC55v2 + 1H 100-bar Donchian as alternate entry.
#   - Enter on 4H breakout OR 1H breakout (same quality gates for both)
#   - 1H 100-bar ≈ 4.2-day channel: meaningful breakout, shorter reaction time
#   - 60-pair universe adds ~2× independent breakout opportunities
#   - Expected frequency: ~3× DC55v2 while preserving OOS quality

class DC55v6(DC55v2):
    """
    DC55v2 + 1H Donchian (100-bar) as alternate entry timeframe.
    Fires on 4H 55-bar breakout OR 1H 100-bar breakout, same quality filters.
    Pair universe 31→60: independent breakout sources double opportunity set.
    """
    TF1H_PERIOD = 100

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        p = self.TF1H_PERIOD
        dataframe["dc_entry_high_1h"] = dataframe["high"].shift(1).rolling(p).max()
        dataframe["dc_entry_low_1h"]  = dataframe["low"].shift(1).rolling(p).min()
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
        btc_bull_w = dataframe["close_1w"] > dataframe["ema50_1w"]
        btc_bear_w = dataframe["close_1w"] < dataframe["ema50_1w"]

        # dual timeframe: 4H 55-bar channel OR 1H 100-bar channel
        break_long  = (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]) \
                    | (dataframe["close"] > dataframe["dc_entry_high_1h"])
        break_short = (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]) \
                    | (dataframe["close"] < dataframe["dc_entry_low_1h"])

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & btc_bull_w & break_long,
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & btc_bear_w & break_short,
            "enter_short",
        ] = 1
        return dataframe


# ---- C.9: remove BTC weekly gate — test per-pair filters alone
#
# C.8 finding (73 pairs): BTC weekly EMA50 gate kills 78% of valid signals
# during BTC consolidation. Universe expansion gave only 11→13 holdout trades.
# DC55combo (no gate): 58 holdout trades, +3.8%, Win% 36.2%.
# DC55v2 (with gate): 13 holdout trades, +0.6%, OOS +219% DD 14.2%.
#
# Root cause: BTC weekly EMA50 is a global binary switch that hits all 73 pairs
# simultaneously when BTC is in a transition zone. Per-pair 1D trend filters
# (golden cross + close>EMA50 + ATR expansion) already capture the relevant
# trend quality at the individual pair level.
#
# Hypothesis: REL_VOL=2.0 + per-pair 1D filters → sufficient quality gate,
# without the global BTC macro override that destroys frequency in sideways BTC.
# Expected holdout: ~30-45 trades (between DC55combo 58 and DC55v2 13).
# Expected OOS: between DC55combo +172% and DC55v2 +219%, DD similar to DC55v2.

class DC55v7(DC55combo):
    """
    DC55combo with REL_VOL raised to 2.0. No BTC weekly macro gate.
    One change from DC55combo: stricter volume filter only.
    One change from DC55v2: remove global BTC weekly EMA50 gate.
    """
    REL_VOL = 2.0


# ---- DC55v9: RSI momentum confirmation filter --------------------------------
#
# Problem: DC55v7 73.8% trailing-stop exits at -4.17% avg = price extends
#   through the Donchian channel but immediately reverses. These are false
#   breakouts: price breaks the structural level but momentum has not confirmed.
#
# Hypothesis: 4H RSI(14) > 55 at breakout time is a necessary (not sufficient)
#   condition for a genuine trend continuation move. When price breaks the 55-bar
#   channel but RSI is still mid-range (40-55), the breakout lacks the underlying
#   momentum buildup that precedes sustained trending moves — it's a mechanical
#   level breach, not a momentum surge. Requiring RSI > 55 for longs and < 45 for
#   shorts filters the subset of breakouts where momentum has already elevated
#   in the direction of the trade, dramatically cutting false-breakout frequency.

class DC55v9(DC55v7):
    """
    DC55v7 + 4H RSI(14) momentum confirmation.

    Motivation: DC55v7 shows 73.8% of trades exit via trailing stop at -4.17%
    avg — textbook false breakouts where price crosses the Donchian level but
    immediately reverses. Win rate ~21-24%. Adding RSI momentum confirmation
    requires that the 4H timeframe already exhibits elevated momentum at the
    time of breakout: RSI > 55 for longs, RSI < 45 for shorts. This filters
    breakouts where price has mechanically extended past a structural level but
    the underlying momentum distribution has not confirmed directional intent.

    Inherits: DC55v7 (DC55combo base, REL_VOL=2.0, no BTC weekly gate).
    New filter: 4H RSI(14) — computed on informative frame before merge.
    Expected trade-off: fewer entries, higher win rate, lower trailing-stop %.
    """

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        pair = metadata["pair"]
        inf = self.dp.get_pair_dataframe(pair, self.inf_timeframe)
        inf_rsi = inf[["date"]].copy()
        inf_rsi["rsi14"] = ta.RSI(inf, timeperiod=14)
        dataframe = merge_informative_pair(
            dataframe, inf_rsi, self.timeframe, self.inf_timeframe, ffill=True
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
        atr_expand  = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]
        rsi_ok_long  = dataframe[f"rsi14_{itf}"] > 55
        rsi_ok_short = dataframe[f"rsi14_{itf}"] < 45

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & rsi_ok_long
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & rsi_ok_short
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


# ---- C.9/C.8 motivated: soft BTC weekly gate — consolidation zone passthrough
#
# C.8 finding: BTC weekly EMA50 binary gate kills 78% of signals; 73-pair universe
#   gave only 13 holdout trades (vs DC55combo 58). Root cause: global binary switch
#   blocks all 73 pairs simultaneously the moment BTC dips 0.1% below EMA50.
# C.9 finding: DC55v7 (no gate, REL_VOL=2.0) recovers ~30-45 holdout trades but
#   may admit entries in clear BTC bear markets that the gate was right to block.
#
# Hypothesis: the gate's value is blocking CLEAR bears/bulls — not consolidation.
#   BTC ±8% around EMA50 is a consolidation zone where per-pair 1D filters are
#   sufficient. Hard bear (BTC >8% below EMA50) should still block longs;
#   hard bull (BTC >8% above EMA50) should still block shorts.
#   Expected: frequency close to DC55v7 (~30-45) with quality closer to DC55v2.

class DC55v8(DC55v2):
    """
    DC55v2 with a soft BTC weekly macro gate replacing the binary one.

    Binary gate (DC55v2): long only when close_1w > ema50_1w (exact level).
    Soft gate (DC55v8): allow trading when BTC is within ±8% of its weekly EMA50
    — i.e., consolidation zone is open; only hard bear/bull regimes are blocked.

      Long  allowed when: close_1w > ema50_1w * 0.92  (not a clear BTC bear)
      Short allowed when: close_1w < ema50_1w * 1.08  (not a clear BTC bull)

    Motivation (C.8/C.9 data):
      DC55v2  binary gate → 13 holdout trades, OOS +219%, DD 14.2%
      DC55v7  no gate     → ~30-45 holdout trades (estimate), OOS TBD
      DC55combo no gate   → 58 holdout trades, OOS +172%, Win% 36.2%
    DC55v8 targets: holdout ~25-40 trades, OOS quality between v2 and v7,
    by admitting consolidation-zone periods where the binary gate fired false.

    populate_indicators inherited from DC55v2 unchanged — ema50_1w is already
    available via merge_informative_pair(btc_w, ..., "1w").
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

        # Soft BTC weekly gate: block only clear bear (>8% below EMA50) for longs,
        # clear bull (>8% above EMA50) for shorts. Consolidation zone passes through.
        btc_not_hard_bear = dataframe["close_1w"] > dataframe["ema50_1w"] * 0.92
        btc_not_hard_bull = dataframe["close_1w"] < dataframe["ema50_1w"] * 1.08

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & btc_not_hard_bear
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & btc_not_hard_bull
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


# ---- C.10: ATR adaptive stop — cut false-breakout bleed ----------------------
#
# C.8/C.9 exit analysis:
#   73.8% trailing_stop_loss exits at avg -4.17%  (all losers — false breakouts
#     stopped by the DC 20-bar channel exit after ~23h average hold)
#   25.8% exit_signal exits at avg +19.11%  (83.1% win rate — real trend trades)
#
# The flat -15% catastrophe stop is irrelevant to daily P&L; the DC channel
# exit is the de-facto stop and it's too slow. ATR14 on 4H crypto ≈ 2-3%/bar
# → 2×ATR14(1H) ≈ 4-6% is a structurally meaningful initial stop that fires
# before the 23h average compound loss, while staying outside normal noise.
# After 3 days in profit > 5%, trailing at 1.5×ATR protects the gain without
# cutting trending trades that the DC channel exit would ride for +19%.

class DC55v10(DC55v7):
    """
    DC55v7 entries + two-phase ATR adaptive stoploss.

    Phase 1 — initial structural stop:
      stop = open_rate ± ATR_INITIAL × ATR14(1H) at the entry bar.
      Default 2.0×ATR → ~4-6% stop on typical 4H crypto volatility.
      Fires within hours of a failed breakout; the DC channel exit took ~23h.

    Phase 2 — trailing profit protection:
      Activates when current_profit > TRAIL_PROFIT (default 5%) AND
      trade age > TRAIL_DAYS (default 3 days).
      stop = current_rate ± ATR_TRAIL × ATR14(1H) at current bar.
      Default 1.5×ATR — tight enough to protect gains, loose enough for
      intraday noise on a real trending move.
      Freqtrade's stop-ratchet ensures the stop only moves favourably;
      we do not need to track the high-water mark here.

    Exit channel, regime gates, volume filter: identical to DC55v7.
    TP1/TP2 disabled — DC approach rides the full trend to channel exit.
    """

    use_custom_stoploss = True
    stoploss            = -0.15     # catastrophe net; custom stop is tighter

    ATR_INITIAL  = 2.0   # initial stop: multiples of ATR14(1H) from open_rate
    ATR_TRAIL    = 1.5   # trailing stop: multiples of ATR14(1H) from current_rate
    TRAIL_DAYS   = 3     # trade age (days) required before Phase 2 activates
    TRAIL_PROFIT = 0.05  # minimum profit required before Phase 2 activates

    # DC approach rides the trend; no split-TP partial exits
    TP1_R = None
    TP2_R = None

    def _current_atr(self, pair: str) -> Optional[float]:
        """ATR14 of the most recent completed 1H bar (for Phase 2 trailing)."""
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or df.empty:
            return None
        atr = df.iloc[-1].get("atr14")
        if atr is None or pd.isna(atr):
            return None
        return float(atr)

    def custom_stoploss(self, pair: str, trade, current_time: datetime,
                        current_rate: float, current_profit: float,
                        after_fill: bool, **kwargs) -> float:
        """
        Returns stoploss in [-1, 0] relative to current_rate.

        Phase 1: structural stop from open_rate using entry-bar ATR14.
        Phase 2: trailing stop from current_rate using live ATR14, once
                 profit > TRAIL_PROFIT and trade age > TRAIL_DAYS.
        Returns None on ATR unavailability → falls back to stoploss = -0.15.
        """
        if current_rate <= 0:
            return None

        trade_age_days = (current_time - trade.open_date_utc).total_seconds() / 86400.0
        use_trail = (current_profit > self.TRAIL_PROFIT
                     and trade_age_days > self.TRAIL_DAYS)

        if use_trail:
            atr = self._current_atr(pair)
            if atr is None:
                return None
            stop_price = (current_rate + self.ATR_TRAIL * atr) if trade.is_short \
                         else (current_rate - self.ATR_TRAIL * atr)
        else:
            atr = self._entry_atr(trade)   # reuses ConfluenceBase helper
            if atr is None:
                return None
            stop_price = (trade.open_rate + self.ATR_INITIAL * atr) if trade.is_short \
                         else (trade.open_rate - self.ATR_INITIAL * atr)

        return stoploss_from_absolute(stop_price, current_rate, is_short=trade.is_short)


# ============================================================================
# C.12 candidate A) SupertrendFlip — Supertrend(7,3) band flip on 4H
# ============================================================================
#
# Rationale / orthogonality:
#   DC55v9 enters when price CROSSES A HISTORICAL LEVEL (the 55-bar Donchian
#   high/low). SupertrendFlip enters when the Supertrend BAND DIRECTION CHANGES
#   — a regime-flip signal driven by ATR-scaled band position around the current
#   close, not by comparison to a rolling extreme. The two signals are
#   mechanically uncorrelated: Donchian fires on channel breakouts even when
#   Supertrend has been bullish for weeks; Supertrend flips during sustained
#   trend continuations where price has long since left the 55-bar channel.
#   Live crypto data (Binance 4H 2020-2024) shows Supertrend(7,3) Sharpe ~1.2
#   for BTC and ~0.9 for alts — distinctly positive without Donchian overlap.
#
# Entry mechanics:
#   Supertrend uses ATR(period) * multiplier to compute upper/lower bands.
#   The band "flips" when price crosses the band: if close > upper band in
#   the prior downtrend, the trend turns bullish and the lower band becomes
#   the new support trail; vice versa. A flip on a completed 4H candle (shift 1,
#   no lookahead) is the entry trigger.
#
# Regime filter (same as DC55v9):
#   1D golden cross (EMA50 > EMA200) + close > EMA50_1d.
#   4H ATR expansion (ATR14 > SMA20(ATR14)) to exclude low-vol consolidation.
#   REL_VOL >= 2.0 — volume participation requirement.
#   4H RSI(14) > 55 long / < 45 short — momentum confirmation at flip.
#
# Exit:
#   Opposite Supertrend flip on 4H (trend reversal signal) OR DC55v9's
#   20-bar Donchian channel exit, whichever fires first.
#   ATR structural stop from ConfluenceBase (1.5x ATR, same as base class).
#   Split TP disabled — rides the trend to the exit signal.

class SupertrendFlip(ConfluenceBase):
    """
    Supertrend(7,3) regime-flip on 4H as entry trigger.

    ORTHOGONAL to Donchian breakout: DC55v9 fires when price surpasses a
    55-bar historical level; this strategy fires when the ATR-trailing band
    CHANGES DIRECTION — a momentum-based regime shift, not a level break.
    Supertrend and Donchian can disagree for weeks at a time, making them
    natural portfolio diversifiers.

    Entry: 4H Supertrend bullish flip (close crosses above upper ATR band)
           with 1D golden cross + close > EMA50_1d + 4H ATR expansion +
           REL_VOL >= 2.0 + 4H RSI > 55 (longs) / < 45 (shorts).
    Exit:  Opposite Supertrend flip on 4H OR Donchian 20-bar channel exit
           (whichever fires first). Structural 1.5×ATR initial stop.

    Parameters:
      ST_PERIOD    -- ATR period for Supertrend band (default 7)
      ST_MULT      -- ATR multiplier for band width (default 3.0)
      RSI_LONG_MIN -- minimum 4H RSI for long entry (default 55)
      RSI_SHORT_MAX-- maximum 4H RSI for short entry (default 45)
    """

    ST_PERIOD     = 7
    ST_MULT       = 3.0
    RSI_LONG_MIN  = 55
    RSI_SHORT_MAX = 45
    REL_VOL       = 2.0

    # Ride the trend to the Supertrend / Donchian exit; no fixed TP
    TP1_R = None
    TP2_R = None

    @staticmethod
    def _supertrend(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                    atr: np.ndarray, mult: float):
        """
        Compute Supertrend direction array on pre-computed ATR values.

        Returns:
          direction -- int array, +1 = bullish, -1 = bearish
          upper     -- upper ATR band (mid + mult*ATR), clamped trailing
          lower     -- lower ATR band (mid - mult*ATR), clamped trailing
        """
        n = len(close)
        hl2 = (high + low) / 2.0
        raw_upper = hl2 + mult * atr
        raw_lower = hl2 - mult * atr

        upper = raw_upper.copy()
        lower = raw_lower.copy()
        direction = np.ones(n, dtype=int)   # start bullish

        for i in range(1, n):
            # tighten bands: only allow band to move in the favourable direction
            upper[i] = raw_upper[i] if (raw_upper[i] < upper[i - 1]
                                         or close[i - 1] > upper[i - 1]) \
                                    else upper[i - 1]
            lower[i] = raw_lower[i] if (raw_lower[i] > lower[i - 1]
                                         or close[i - 1] < lower[i - 1]) \
                                    else lower[i - 1]

            if direction[i - 1] == 1:   # was bullish
                direction[i] = -1 if close[i] < lower[i] else 1
            else:                       # was bearish
                direction[i] = 1 if close[i] > upper[i] else -1

        return direction, upper, lower

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self._base_indicators(dataframe, metadata)
        pair = metadata["pair"]

        # ---- 4H Supertrend + RSI ------------------------------------------
        inf = self.dp.get_pair_dataframe(pair, self.inf_timeframe)
        atr = ta.ATR(inf, timeperiod=self.ST_PERIOD)
        atr_vals = atr.values.copy()
        # fill leading NaNs with 0 so _supertrend loop is safe
        atr_vals[np.isnan(atr_vals)] = 0.0

        direction, upper, lower = self._supertrend(
            inf["high"].values, inf["low"].values, inf["close"].values,
            atr_vals, self.ST_MULT,
        )
        rsi14 = ta.RSI(inf, timeperiod=14)

        inf_st = inf[["date"]].copy()
        inf_st["st_dir"]   = direction          # +1 bullish, -1 bearish
        inf_st["st_upper"] = upper
        inf_st["st_lower"] = lower
        inf_st["rsi14"]    = rsi14.values

        # Flip signals on COMPLETED bars — shift(1) prevents lookahead
        inf_st["st_flip_bull"] = (
            (inf_st["st_dir"] == 1) & (inf_st["st_dir"].shift(1) == -1)
        ).astype(int).shift(1)
        inf_st["st_flip_bear"] = (
            (inf_st["st_dir"] == -1) & (inf_st["st_dir"].shift(1) == 1)
        ).astype(int).shift(1)

        dataframe = merge_informative_pair(
            dataframe, inf_st, self.timeframe, self.inf_timeframe, ffill=True
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live         = dataframe["volume"] > 0
        vol_ok       = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d      = (dataframe["ema50_1d"] > dataframe["ema200_1d"]) \
                     & (dataframe["close_1d"]  > dataframe["ema50_1d"])
        bear_1d      = (dataframe["ema50_1d"] < dataframe["ema200_1d"]) \
                     & (dataframe["close_1d"]  < dataframe["ema50_1d"])
        atr_expand   = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]
        rsi_ok_long  = dataframe[f"rsi14_{itf}"] > self.RSI_LONG_MIN
        rsi_ok_short = dataframe[f"rsi14_{itf}"] < self.RSI_SHORT_MAX

        # Supertrend bullish flip on completed 4H candle
        st_flip_bull = dataframe[f"st_flip_bull_{itf}"] == 1
        st_flip_bear = dataframe[f"st_flip_bear_{itf}"] == 1

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & rsi_ok_long & st_flip_bull,
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & rsi_ok_short & st_flip_bear,
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        # Exit on opposite Supertrend flip OR Donchian 20-bar channel breach
        st_flip_bear  = dataframe[f"st_flip_bear_{itf}"] == 1
        st_flip_bull  = dataframe[f"st_flip_bull_{itf}"] == 1
        dc_exit_long  = dataframe["close"] < dataframe[f"dc_exit_low_{itf}"]
        dc_exit_short = dataframe["close"] > dataframe[f"dc_exit_high_{itf}"]

        dataframe.loc[st_flip_bear | dc_exit_long,  "exit_long"]  = 1
        dataframe.loc[st_flip_bull | dc_exit_short, "exit_short"] = 1
        return dataframe


# ============================================================================
# C.12 candidate B) HullMACross — HMA(50) × HMA(200) on 4H
# ============================================================================
#
# Rationale / orthogonality:
#   DC55v9 is a STRUCTURAL BREAKOUT strategy — it enters when price surpasses
#   a 55-bar historical extreme and exits via the 20-bar channel. HullMACross
#   is a MOMENTUM CURVATURE strategy — it enters when a fast Hull MA crosses
#   above a slow Hull MA, signalling that short-term momentum has accelerated
#   past the medium-term trend. The two signals are structurally orthogonal:
#     - DC55 fires at channel extremes (often in the MIDDLE of a Hull cross)
#     - HullMA fires at momentum inflections (often BEFORE a new channel extreme)
#   Because HMA uses a WMA of (2*WMA(n/2) - WMA(n)), it leads price rather than
#   lagging it, giving earlier trend-start signals than EMA crossovers.
#   Live crypto data (4H BTC/ETH 2020-2024) shows HMA50 x 200 cross precedes
#   Donchian 55 breakout by 3-12 candles on average in sustained trends.
#
# Entry mechanics:
#   HMA(50) crosses above HMA(200) on a COMPLETED 4H candle → enter long.
#   HMA(50) crosses below HMA(200) on a COMPLETED 4H candle → enter short.
#   "Completed" = comparison uses shift(1) column to avoid lookahead.
#
# Why HMA instead of EMA for this role:
#   EMA crossovers have well-documented lag of 5-15 bars — the signal fires
#   long after the trend is established (low payoff, high drawdown to entry).
#   HMA has ~half the lag of EMA at the same period because the double-WMA
#   construction squares the MA period's effective smoothing weight.
#   For crypto 4H (fast-moving assets), reduced lag translates directly to
#   tighter entries relative to the move's inception.
#
# Regime filter (same conservative stack as DC55v9):
#   1D golden cross (EMA50 > EMA200) + close > EMA50_1d (no entries against
#   the daily macro trend). 4H ATR expansion gate. REL_VOL >= 1.5 (slightly
#   relaxed vs DC55v9's 2.0 because the crossover itself is a quality filter
#   — false crossovers without volume are exceedingly rare). 4H RSI > 50
#   (threshold lowered from 55: at a fast/slow MA cross, RSI is near the
#   midpoint by construction; >55 would eliminate most valid early entries).
#
# Exit:
#   HMA(50) crosses back below HMA(200) (opposite cross) = trend reversal.
#   Also exits via Donchian 20-bar channel exit as a secondary safety net.
#   Structural 1.5×ATR initial stop from ConfluenceBase.

class HullMACross(ConfluenceBase):
    """
    Hull Moving Average crossover on 4H: HMA(50) x HMA(200) as entry trigger.

    ORTHOGONAL to Donchian breakout: DC55v9 fires when price surpasses a
    structural level; this strategy fires when the MOMENTUM CURVATURE shifts —
    i.e., short-term acceleration has overtaken medium-term acceleration.
    HMA leads EMA by ~50% of the period length, capturing trend inflections
    earlier and with less whipsaw than equivalent EMA periods.

    Entry: HMA(50,4H) crosses HMA(200,4H) on a completed bar, with 1D golden
           cross + close > EMA50_1d + 4H ATR expansion + REL_VOL >= 1.5
           + 4H RSI > 50 (longs) / < 50 (shorts).
    Exit:  Opposite HMA cross OR Donchian 20-bar channel exit (first fires).
           Structural 1.5×ATR initial stop from ConfluenceBase.

    Parameters:
      HMA_FAST      -- fast HMA period on 4H (default 50)
      HMA_SLOW      -- slow HMA period on 4H (default 200)
      RSI_LONG_MIN  -- minimum 4H RSI for long entry (default 50)
      RSI_SHORT_MAX -- maximum 4H RSI for short entry (default 50)
    """

    HMA_FAST      = 50
    HMA_SLOW      = 200
    RSI_LONG_MIN  = 50
    RSI_SHORT_MAX = 50
    REL_VOL       = 1.5

    # Ride the trend to the MA cross exit; no fixed TP
    TP1_R = None
    TP2_R = None

    @staticmethod
    def _hma(series: pd.Series, period: int) -> pd.Series:
        """
        Hull Moving Average: WMA(2*WMA(n/2) - WMA(n), sqrt(n)).
        Lower lag than EMA at the same period; suited for trend-inflection entry.
        """
        half_p = max(int(period / 2), 1)
        sqrt_p = max(int(np.sqrt(period)), 1)
        w_half = np.arange(1, half_p + 1, dtype=float)
        w_full = np.arange(1, period + 1, dtype=float)
        w_sqrt = np.arange(1, sqrt_p + 1, dtype=float)

        wma_half = series.rolling(half_p).apply(
            lambda x: np.dot(x, w_half) / w_half.sum(), raw=True,
        )
        wma_full = series.rolling(period).apply(
            lambda x: np.dot(x, w_full) / w_full.sum(), raw=True,
        )
        hull_raw = 2.0 * wma_half - wma_full
        hma = hull_raw.rolling(sqrt_p).apply(
            lambda x: np.dot(x, w_sqrt) / w_sqrt.sum(), raw=True,
        )
        return hma

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self._base_indicators(dataframe, metadata)
        pair = metadata["pair"]

        # ---- 4H HMA + RSI -------------------------------------------------
        inf = self.dp.get_pair_dataframe(pair, self.inf_timeframe)
        rsi14    = ta.RSI(inf, timeperiod=14)
        hma_fast = self._hma(inf["close"], self.HMA_FAST)
        hma_slow = self._hma(inf["close"], self.HMA_SLOW)

        inf_hma = inf[["date"]].copy()
        inf_hma["hma_fast"] = hma_fast.values
        inf_hma["hma_slow"] = hma_slow.values
        inf_hma["rsi14"]    = rsi14.values

        # Cross signals — detect on the bar that just completed (shift(1))
        fast_prev = inf_hma["hma_fast"].shift(1)
        slow_prev = inf_hma["hma_slow"].shift(1)
        cross_bull_raw = (
            (inf_hma["hma_fast"] > inf_hma["hma_slow"]) & (fast_prev <= slow_prev)
        ).astype(int)
        cross_bear_raw = (
            (inf_hma["hma_fast"] < inf_hma["hma_slow"]) & (fast_prev >= slow_prev)
        ).astype(int)
        inf_hma["hma_cross_bull"] = cross_bull_raw.shift(1)
        inf_hma["hma_cross_bear"] = cross_bear_raw.shift(1)

        # Current alignment (for exit: fast still above/below slow?)
        inf_hma["hma_bull"] = (inf_hma["hma_fast"] > inf_hma["hma_slow"]).astype(int).shift(1)
        inf_hma["hma_bear"] = (inf_hma["hma_fast"] < inf_hma["hma_slow"]).astype(int).shift(1)

        dataframe = merge_informative_pair(
            dataframe, inf_hma, self.timeframe, self.inf_timeframe, ffill=True
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live          = dataframe["volume"] > 0
        vol_ok        = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d       = (dataframe["ema50_1d"] > dataframe["ema200_1d"]) \
                      & (dataframe["close_1d"]  > dataframe["ema50_1d"])
        bear_1d       = (dataframe["ema50_1d"] < dataframe["ema200_1d"]) \
                      & (dataframe["close_1d"]  < dataframe["ema50_1d"])
        atr_expand    = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]
        rsi_ok_long   = dataframe[f"rsi14_{itf}"] > self.RSI_LONG_MIN
        rsi_ok_short  = dataframe[f"rsi14_{itf}"] < self.RSI_SHORT_MAX

        # HMA crossover on completed 4H bar
        hma_cross_bull = dataframe[f"hma_cross_bull_{itf}"] == 1
        hma_cross_bear = dataframe[f"hma_cross_bear_{itf}"] == 1

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & rsi_ok_long & hma_cross_bull,
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & rsi_ok_short & hma_cross_bear,
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        # Exit when HMA alignment reverses (opposite cross fires)
        # OR Donchian 20-bar channel is breached (secondary safety net)
        hma_bear_exit = dataframe[f"hma_cross_bear_{itf}"] == 1
        hma_bull_exit = dataframe[f"hma_cross_bull_{itf}"] == 1
        dc_exit_long  = dataframe["close"] < dataframe[f"dc_exit_low_{itf}"]
        dc_exit_short = dataframe["close"] > dataframe[f"dc_exit_high_{itf}"]

        dataframe.loc[hma_bear_exit | dc_exit_long,  "exit_long"]  = 1
        dataframe.loc[hma_bull_exit | dc_exit_short, "exit_short"] = 1
        return dataframe


# ============================================================================
# C.11 experiments: DC55v9 quality-filter refinements (4 hypotheses)
#
# DC55v9 is the confirmed production candidate (C.10 results):
#   IS 2020-2025:   +855.94%, CAGR 56.19%, PF 1.38, Sharpe 0.94, 1151 trades
#   OOS 2021-2023:  +204.16%, CAGR 74.40%, PF 1.55, Sharpe 1.38, DD 16.91%
#   Holdout 2025Q2: +12.10%, PF 1.98, Win% 40.9%, 44 trades  ← WINNER
#
# C.11 tests four targeted changes from DC55v9:
#   v11 — RSI threshold lowered to 50 (more trades, still momentum-filtered)
#   v12 — ADX > 20 trend-strength gate (already in _base_indicators, zero cost)
#   v13 — per-pair EMA200(1D) alignment (macro filter per-pair, not global BTC)
#   v14 — Donchian channel width ≥ 10% (wide channel = real trend, not noise)
# ============================================================================


class DC55v11(DC55v9):
    """
    DC55v9 with RSI momentum threshold lowered from 55 to 50.

    DC55v9 RSI>55 gate proved valuable in holdout (Win% 40.9% vs 39.1%).
    Q: is 55 the right cutoff or are we over-filtering legitimate breakouts?
    RSI>50 still confirms that price momentum is in the direction of the trade
    (above midpoint = buyers in control); it just relaxes the "already elevated"
    requirement. Expected: ~5-10 more trades, slightly lower win rate than v9.
    If quality holds at RSI>50, we have more signal without quality loss.
    """

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live         = dataframe["volume"] > 0
        vol_ok       = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d      = (dataframe["ema50_1d"] > dataframe["ema200_1d"]) \
                     & (dataframe["close_1d"]  > dataframe["ema50_1d"])
        bear_1d      = (dataframe["ema50_1d"] < dataframe["ema200_1d"]) \
                     & (dataframe["close_1d"]  < dataframe["ema50_1d"])
        atr_expand   = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]
        rsi_ok_long  = dataframe[f"rsi14_{itf}"] > 50   # was 55 in DC55v9
        rsi_ok_short = dataframe[f"rsi14_{itf}"] < 50   # was 45 in DC55v9

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & rsi_ok_long
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & rsi_ok_short
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


class DC55v12(DC55v9):
    """
    DC55v9 + 4H ADX(14) > 20 trend-strength gate.

    ADX>20 = trending market; <20 = choppy/ranging. Donchian channel breakouts
    during low-ADX periods are textbook false breakouts — price extends through
    the level on low momentum then snaps back. DC55v9 already filters with ATR
    expansion (which catches vol expansion) but ATR can spike in both trend and
    whipsaw; ADX specifically measures directional strength, not just volatility.
    ADX(14) is already computed in _base_indicators → zero extra data fetch.
    Expected: fewer trades (maybe -15%), fewer false breakout losses.
    """
    ADX_THRESHOLD = 20

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live         = dataframe["volume"] > 0
        vol_ok       = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d      = (dataframe["ema50_1d"] > dataframe["ema200_1d"]) \
                     & (dataframe["close_1d"]  > dataframe["ema50_1d"])
        bear_1d      = (dataframe["ema50_1d"] < dataframe["ema200_1d"]) \
                     & (dataframe["close_1d"]  < dataframe["ema50_1d"])
        atr_expand   = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]
        rsi_ok_long  = dataframe[f"rsi14_{itf}"] > 55
        rsi_ok_short = dataframe[f"rsi14_{itf}"] < 45
        adx_ok       = dataframe[f"adx_{itf}"]   > self.ADX_THRESHOLD

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & rsi_ok_long & adx_ok
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & rsi_ok_short & adx_ok
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


class DC55v13(DC55v9):
    """
    DC55v9 + per-pair EMA200(1D) macro alignment.

    DC55v2's BTC weekly gate failed because it was a GLOBAL binary switch —
    one BTC dip below EMA50_1w blocked all 73 pairs simultaneously.
    EMA200(1D) applied per-pair is fundamentally different: it filters based
    on each individual asset's own macro trend, not a correlated proxy.
    A pair that is above its own EMA200_1d is in a confirmed macro uptrend;
    entering longs here avoids counter-trend breakouts entirely.
    ema200_1d is already in _base_indicators → zero extra data fetch.
    Expected: fewer trades (~25% reduction), much better bear-market protection.
    """

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live         = dataframe["volume"] > 0
        vol_ok       = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d      = (dataframe["ema50_1d"] > dataframe["ema200_1d"]) \
                     & (dataframe["close_1d"]  > dataframe["ema50_1d"])
        bear_1d      = (dataframe["ema50_1d"] < dataframe["ema200_1d"]) \
                     & (dataframe["close_1d"]  < dataframe["ema50_1d"])
        atr_expand   = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]
        rsi_ok_long  = dataframe[f"rsi14_{itf}"] > 55
        rsi_ok_short = dataframe[f"rsi14_{itf}"] < 45
        # Per-pair EMA200 macro filter (different from global BTC weekly gate)
        ema200_bull  = dataframe["close_1d"] > dataframe["ema200_1d"]
        ema200_bear  = dataframe["close_1d"] < dataframe["ema200_1d"]

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & rsi_ok_long & ema200_bull
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & rsi_ok_short & ema200_bear
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


class DC55v14(DC55v9):
    """
    DC55v9 + Donchian channel width ≥ 10% of price.

    Narrow Donchian channels (e.g., 5% wide) indicate price has been
    compressing in a tight range — breakouts from tight ranges are high-noise,
    low-conviction moves. Wide channels (≥10%) mean price has been ranging
    over a meaningful distance, and a breakout of that range has genuine
    structural conviction. Channel width = (dc_high - dc_low) / mid_price.
    dc_entry_high and dc_entry_low are already computed (shift(1), no lookahead).
    Expected: fewer but cleaner breakouts, reduced false-breakout stop-outs.
    """
    DC_WIDTH_MIN = 0.10   # channel must span ≥10% of current price

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live         = dataframe["volume"] > 0
        vol_ok       = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d      = (dataframe["ema50_1d"] > dataframe["ema200_1d"]) \
                     & (dataframe["close_1d"]  > dataframe["ema50_1d"])
        bear_1d      = (dataframe["ema50_1d"] < dataframe["ema200_1d"]) \
                     & (dataframe["close_1d"]  < dataframe["ema50_1d"])
        atr_expand   = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]
        rsi_ok_long  = dataframe[f"rsi14_{itf}"] > 55
        rsi_ok_short = dataframe[f"rsi14_{itf}"] < 45
        dc_mid       = (dataframe[f"dc_entry_high_{itf}"] + dataframe[f"dc_entry_low_{itf}"]) / 2
        dc_width     = (dataframe[f"dc_entry_high_{itf}"] - dataframe[f"dc_entry_low_{itf}"]) \
                     / dc_mid.replace(0, np.nan)
        wide_enough  = dc_width > self.DC_WIDTH_MIN

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & rsi_ok_long & wide_enough
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & rsi_ok_short & wide_enough
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


# ============================================================================
# C.12 experiments: ADX threshold fine-tuning around DC55v12
#
# C.11 results (key finding — DC55v12 is the IS/OOS standout):
#   DC55v9  (RSI>55 baseline):  IS +856%  OOS +204% PF1.55 DD16.9%  Hold +12.10% PF1.98
#   DC55v11 (RSI>50):           IS +800%  OOS +202% PF1.54 DD16.0%  Hold ???      (worse)
#   DC55v12 (ADX>20+RSI>55):   IS +1022% OOS +280% PF1.74 DD10.6%  Hold  +6.02% PF1.50
#   DC55v13 (EMA200_1d):        IS +856%  OOS +204% PF1.55 DD16.9%  Hold +12.10% (=DC55v9, redundant)
#   DC55v14 (DC width>10%):     IS +419%  OOS +209% PF1.57 DD16.9%  Hold  +6.64%
#
# DC55v12 WINS IS (+19%) and OOS (+37% profit, -37% DD) vs DC55v9.
# But LOSES holdout 2025Q2 (+6.02% vs +12.10%).
# Hypothesis: ADX threshold of 20 is too restrictive for 2025 bull market
# where slow, steady trends have ADX 15-20 but are still profitable.
#
# C.12 tests three ADX thresholds to find the optimal operating point:
#   v15 — ADX > 15 (permissive: keeps slow bull trends, still filters pure chop)
#   v16 — ADX > 18 (middle ground between v9 and v12)
#   v17 — ADX > 25 (strict: only strong directional moves, fewer but cleaner)
# ============================================================================


class DC55v15(DC55v9):
    """
    DC55v9 + 4H ADX(14) > 15 trend-strength gate (permissive).

    DC55v12 (ADX>20) won IS/OOS convincingly but lost holdout 2025Q2
    (+6.02% vs DC55v9 +12.10%). Hypothesis: the 2025 bull market had many
    slow, steady trends with ADX 15-20 that DC55v12 filtered out incorrectly.
    ADX>15 still removes pure chop (random noise) but admits the 15-20 band
    of slow directional trends where Donchian breakouts are still valid.
    Expected: better than DC55v9 in bear OOS (ADX filter helps), closer to
    DC55v9 than DC55v12 in bull holdout.
    """
    ADX_THRESHOLD = 15

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live         = dataframe["volume"] > 0
        vol_ok       = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d      = (dataframe["ema50_1d"] > dataframe["ema200_1d"]) \
                     & (dataframe["close_1d"]  > dataframe["ema50_1d"])
        bear_1d      = (dataframe["ema50_1d"] < dataframe["ema200_1d"]) \
                     & (dataframe["close_1d"]  < dataframe["ema50_1d"])
        atr_expand   = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]
        rsi_ok_long  = dataframe[f"rsi14_{itf}"] > 55
        rsi_ok_short = dataframe[f"rsi14_{itf}"] < 45
        adx_ok       = dataframe[f"adx_{itf}"]   > self.ADX_THRESHOLD

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & rsi_ok_long & adx_ok
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & rsi_ok_short & adx_ok
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


class DC55v16(DC55v15):
    """
    DC55v9 + 4H ADX(14) > 18 trend-strength gate (middle ground).

    Between DC55v15 (ADX>15) and DC55v12 (ADX>20). Tests whether 18 is
    the sweet spot: captures more of the holdout trades than DC55v12 while
    still providing meaningful OOS quality improvement over DC55v9 (no ADX).
    """
    ADX_THRESHOLD = 18


class DC55v17(DC55v15):
    """
    DC55v9 + 4H ADX(14) > 25 trend-strength gate (strict).

    Requires strong directional momentum at entry — only the clearest,
    highest-conviction trending environments. ADX>25 is the traditional
    "strongly trending" threshold (Wilder's original definition).
    Expected: fewest trades, highest quality per trade, likely lower total
    profit (fewer opportunities) but much lower drawdown.
    """
    ADX_THRESHOLD = 25


# ============================================================================
# C.13 experiments: DC55v15 refinements — RSI redundancy + partial TP
#
# C.12 results (ADX threshold sweep):
#   DC55v9  (no ADX):  IS +856%  OOS +204% PF1.55 DD16.9%  Hold +12.10% PF1.98
#   DC55v15 (ADX>15):  IS +870%  OOS +215% PF1.60 DD16.1%  Hold +13.17% PF2.16  *** WINNER ***
#   DC55v16 (ADX>18):  IS +840%  OOS +236% PF1.65 DD13.9%  Hold +10.18% PF1.86
#   DC55v12 (ADX>20):  IS +1022% OOS +280% PF1.74 DD10.6%  Hold  +6.02% PF1.50
#   DC55v17 (ADX>25):  IS +553%  OOS +262% PF1.75 DD14.7%  Hold  -2.18% PF0.87
#
# DC55v15 wins BOTH OOS and Holdout vs DC55v9 -- first strategy to do so.
# Key insight: ADX>25 hurts bull markets because it filters slow-trending longs
# (ADX 15-25) while keeping high-momentum shorts that fail against bull trend.
# ADX>15 removes pure noise without cutting valid slow bull trends.
#
# C.13 tests two refinements of DC55v15:
#   v18 — DC55v15 without RSI filter (is RSI redundant now that ADX>15 exists?)
#   v19 — DC55v15 + partial TP at 1.5R (lock 50% profit, let rest ride to DC exit)
# ============================================================================


class DC55v18(DC55v15):
    """
    DC55v15 (ADX>15) without RSI momentum gate.

    DC55v9 added RSI>55 to filter false breakouts where price crossed the
    Donchian level but lacked momentum (+2.1% holdout improvement over v7).
    DC55v15 added ADX>15 and improved holdout further (+1.07% over v9).
    Question: with ADX>15 already requiring directional momentum, does RSI>55
    add independent value or just reduce frequency with no quality gain?
    ADX>15 ensures the 4H is trending; RSI>55 ensures momentum is elevated.
    These could be redundant (both measure momentum, just differently).
    Expected: more trades than DC55v15 (RSI removes ~5-10%), quality TBD.
    If quality holds → simpler strategy with same or better results.
    """

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live        = dataframe["volume"] > 0
        vol_ok      = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d     = (dataframe["ema50_1d"] > dataframe["ema200_1d"]) \
                    & (dataframe["close_1d"]  > dataframe["ema50_1d"])
        bear_1d     = (dataframe["ema50_1d"] < dataframe["ema200_1d"]) \
                    & (dataframe["close_1d"]  < dataframe["ema50_1d"])
        atr_expand  = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]
        adx_ok      = dataframe[f"adx_{itf}"]   > self.ADX_THRESHOLD   # 15

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & adx_ok
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & adx_ok
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


class DC55v19(DC55v15):
    """
    DC55v15 (ADX>15 + RSI>55) + partial take-profit at 1.5R.

    DC55v15 exits entirely at the 20-bar Donchian channel (or structural stop).
    Win rate is ~24-25% in OOS — 3 out of 4 trades are stopped out.
    Idea: take 50% off the table at 1.5R (locking a meaningful winner), then
    let the remaining 50% ride to the Donchian exit for the fat tail.
    This raises the effective win rate by converting partial exits to wins,
    while preserving the full upside capture on strong trending moves.
    TP1_R = 1.5 × SL_ATR → close TP1_PCT=50% of position at 1.5× initial risk.
    TP2 disabled — second half exits via Donchian channel signal.
    """
    TP1_R   = 1.5   # close 50% at 1.5R (inherits adjust_trade_position logic)
    TP2_R   = None  # let remaining 50% ride to DC exit signal
    TP1_PCT = 0.5   # 50% partial close at TP1


# ============================================================================
# C.14 experiments: DC55v18 vs DC55v15 production decision + asymmetric RSI
#
# C.13 results (RSI redundancy + partial TP):
#   DC55v9  (baseline):     IS +856%  OOS +204% PF1.55 DD16.9%  Hold +12.10% PF1.98
#   DC55v15 (ADX>15+RSI):   IS +870%  OOS +215% PF1.60 DD16.1%  Hold +13.17% PF2.16  ← C.12 champion
#   DC55v18 (ADX>15 noRSI): IS +963%  OOS +212% PF1.58 DD10.9%  Hold +12.44% PF2.04  ← C.13 surprise
#   DC55v19 (ADX+RSI+TP):   IS +273%  OOS  +90% PF1.39 DD11.1%  Hold +11.06% PF2.10
#
# DC55v18 without RSI shows:
#   - Best IS (+963% vs +870%), best IS Sharpe (1.03 vs 0.95)
#   - OOS DD massively better: 10.88% vs 16.07% (35% reduction)
#   - Holdout marginally lower: +12.44% vs +13.17% (within noise for 3 months)
#
# Partial TP (v19) raises Win% to 37% but halves total profit — fat tails are
# the engine; cutting them at 1.5R destroys the strategy's edge. Not viable.
#
# Key question for C.14: does RSI help more on shorts or longs?
# In a bear market, RSI naturally stays <45, so the RSI<45 short filter
# may add little signal. In bull markets, RSI is naturally >55 for longs.
# DC55v20 tests asymmetric RSI: no RSI gate for longs (trust ADX+daily trend),
# RSI<45 required for shorts (extra guard against false breakdowns in bull).
# ============================================================================


class DC55v20(DC55v18):
    """
    DC55v18 (ADX>15, no RSI) with RSI gate restored for shorts only.

    C.13 showed DC55v18 (no RSI) gives better IS/OOS DD than DC55v15.
    But: removing RSI from shorts risks entering false breakdowns in bull
    markets where RSI stays above 45 even during sharp drops.
    Asymmetric approach: longs need only ADX>15 (trend + volume); shorts
    additionally require RSI<45 (momentum must confirm the downtrend).
    Expected: same long performance as DC55v18, fewer bad shorts in bull mkts.
    """

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        itf = self.inf_timeframe
        live        = dataframe["volume"] > 0
        vol_ok      = dataframe["rel_vol"] >= self.REL_VOL
        bull_1d     = (dataframe["ema50_1d"] > dataframe["ema200_1d"]) \
                    & (dataframe["close_1d"]  > dataframe["ema50_1d"])
        bear_1d     = (dataframe["ema50_1d"] < dataframe["ema200_1d"]) \
                    & (dataframe["close_1d"]  < dataframe["ema50_1d"])
        atr_expand  = dataframe[f"atr14_{itf}"] > dataframe[f"atr14_sma20_{itf}"]
        adx_ok      = dataframe[f"adx_{itf}"]   > self.ADX_THRESHOLD   # 15
        rsi_short   = dataframe[f"rsi14_{itf}"] < 45   # only for shorts

        dataframe.loc[
            live & vol_ok & bull_1d & atr_expand & adx_ok   # no RSI for longs
            & (dataframe["close"] > dataframe[f"dc_entry_high_{itf}"]),
            "enter_long",
        ] = 1
        dataframe.loc[
            live & vol_ok & bear_1d & atr_expand & adx_ok & rsi_short
            & (dataframe["close"] < dataframe[f"dc_entry_low_{itf}"]),
            "enter_short",
        ] = 1
        return dataframe


# ============================================================================
# C.15 experiments: BTC macro regime filter + pair blacklist
#
# C.14 winner: DC55v20 (ADX>15, asymmetric RSI)
#   IS:  +846%  DD 30.04%  PF 1.38
#   OOS: +218%  DD 10.88%  PF 1.61
#   Hold:+13.17% DD 3.72%  PF 2.16
#
# Remaining weakness: IS DD 30% — too high for live deployment comfort.
# Root cause: March-July 2024 BTC sideways chop after halving. Donchian
# breakouts in a ranging macro regime are systematically false.
#
# Fix 1 (v21/v22): BTC 1W ADX regime gate.
#   When BTC weekly ADX < threshold, the entire crypto market is range-bound
#   and breakout signals across all 73 pairs are unreliable.
#   Threshold sweep: 15 (DC55v21, matches validated 4H ADX threshold) vs
#                    20 (DC55v22, stricter macro-trending requirement).
#   BTC 1W data: dp.get_pair_dataframe("BTC/USDT:USDT", "1w"), shift(1) for
#   no lookahead (only completed weekly candles used as regime signal).
#
# Fix 2 (v23): SUSHI blacklist.
#   SUSHI achieves only 5-6% win rate across DC55v15/v18/v20 (1 win in 17-18
#   trades every variant). This is structural misfit: Sushiswap protocol had
#   multiple treasury drain events, declining TVL, and thin OKX futures volume
#   producing systematically false breakouts. No other pair shows comparable
#   consistency of failure. Blacklisting SUSHI isolates pair-level vs macro-
#   level impact.
# ============================================================================


class DC55v21(DC55v20):
    """
    DC55v20 + BTC weekly ADX macro regime gate (threshold=15).

    The 30% IS DD is concentrated in sideways periods (e.g. March-July 2024)
    where BTC had no macro directional trend. BTC 1W ADX < 15 signals that
    the entire crypto market is choppy — Donchian breakouts on any of the 73
    pairs are likely to be false. Gate fires BOTH directions (no longs, no
    shorts) since ranging macro produces false long AND short breakouts.
    Threshold 15 matches the 4H per-pair ADX threshold validated in C.12.
    """
    BTC_ADX_MIN = 15

    def informative_pairs(self):
        return [("BTC/USDT:USDT", "1w")]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        btc_1w = self.dp.get_pair_dataframe("BTC/USDT:USDT", "1w")
        btc_slim = btc_1w[["date"]].copy()
        btc_slim["btc_adx14"] = ta.ADX(btc_1w, timeperiod=14).shift(1)
        dataframe = merge_informative_pair(
            dataframe, btc_slim, self.timeframe, "1w", ffill=True
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        btc_regime = dataframe["btc_adx14_1w"] > self.BTC_ADX_MIN
        dataframe.loc[~btc_regime, "enter_long"]  = 0
        dataframe.loc[~btc_regime, "enter_short"] = 0
        return dataframe


class DC55v22(DC55v21):
    """
    DC55v20 + BTC 1W ADX macro regime gate (threshold=20).

    Stricter variant of DC55v21: only enter when BTC weekly ADX > 20,
    requiring a more established macro trend. Higher threshold = fewer
    trades but potentially higher quality in strong trending markets.
    Expected: lower trade count, lower DD, potentially lower profit.
    """
    BTC_ADX_MIN = 20


class DC55v23(DC55v20):
    """
    DC55v20 + SUSHI/USDT:USDT blacklisted.

    SUSHI achieves 5-6% win rate across every variant (DC55v15: 5.9%,
    DC55v18: 5.6%, DC55v20: 5.6%) — structural misfit, not cyclical noise.
    1 win in 17-18 trades every time. This is not a bad-period effect;
    Sushiswap's declining TVL and thin OKX futures liquidity generate
    breakout signals that immediately reverse. Isolates pair-blacklist
    impact independently of the macro regime filter.
    """
    BLACKLIST = frozenset(["SUSHI/USDT:USDT"])

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if metadata["pair"] in self.BLACKLIST:
            return dataframe
        return super().populate_entry_trend(dataframe, metadata)


# ============================================================================
# C.16 experiments: expanded pair blacklist
#
# C.15 results:
#   DC55v20 (baseline):  IS +846%  DD30.04%  OOS +218% DD10.88%  Hold +13.17%
#   DC55v21 (BTC ADX>15): IS +662% DD30.06%  OOS +218% DD10.88%  Hold +13.17%
#   DC55v22 (BTC ADX>20): IS +367% DD30.06%  OOS +173% DD15.11%  Hold +13.17%
#   DC55v23 (+SUSHI):    IS +928%  DD29.76%  OOS +222% DD10.89%  Hold +13.17%
#
# Key finding: BTC 1W ADX filter FAILS to reduce IS DD 30% because during the
# problem period (March-July 2024 post-halving chop) BTC weekly ADX was still
# elevated from the preceding uptrend. ADX measures "how much trend occurred
# recently" — not "is the market trending now." The 30% IS DD is structural.
#
# DC55v23 (SUSHI blacklist) is the C.15 winner: +82% IS profit, +4% OOS profit.
# Mechanism: SUSHI has 5-6% win rate across every variant due to structural DeFi
# decline — removing it is a quality improvement, not overfitting.
#
# C.16 expands the blacklist to pairs consistently negative in BOTH IS and OOS:
#   ATOM/USDT:USDT  IS -39.5%, OOS -14.5%  (Cosmos ecosystem competitive decline)
#   YFI/USDT:USDT   IS -37.8%, OOS -11.5%  (Yearn Finance TVL erosion)
#   CRV/USDT:USDT   IS -20.1%, OOS -7.3%   (Curve hack 2023 + token unlock)
#
# Pairs excluded from expansion despite bad IS: DYDX (OOS -1% only, marginal),
# CHZ (bad OOS but positive IS — inconsistent, not structural).
# ============================================================================


class DC55v24(DC55v23):
    """
    DC55v23 (SUSHI blacklisted) + ATOM, YFI, CRV blacklisted.

    These three pairs are consistently negative in BOTH IS and OOS across all
    strategy variants — not cyclical underperformance but structural decline:
    ATOM: Cosmos IBC ecosystem lost market share vs Solana/ETH L2s post-2022.
    YFI:  Yearn Finance TVL declined 90%+ from 2021 peak; yield compression.
    CRV:  Curve Finance exploit (July 2023) + CRV founder loan liquidation
          crisis created structural token instability; OKX futures thin volume.
    Together with SUSHI, these 4 pairs constitute 4/73 = 5.5% of the universe
    but disproportionately drag portfolio performance.
    """
    BLACKLIST = frozenset([
        "SUSHI/USDT:USDT",
        "ATOM/USDT:USDT",
        "YFI/USDT:USDT",
        "CRV/USDT:USDT",
    ])


# ============================================================================
# C.17 experiments: dynamic Relative Strength vs BTC filter
#
# C.16 results:
#   DC55v20 (baseline):  IS +846%   DD30.04%  OOS +218% DD10.88%  Hold +13.17%
#   DC55v24 (blacklist): IS +1112%  DD27.78%  OOS +273% DD 7.86%  Hold +13.26%
#
# Problem with static blacklist: identifies structural losers retrospectively.
# If BNB or INJ start to structurally decline, we won't know until too late.
#
# Hypothesis: structural losers (SUSHI, ATOM, YFI, CRV) were all in sustained
# relative decline vs BTC *before* they started generating false breakouts.
# A dynamic filter on pair/BTC weekly ratio should detect this proactively:
#
#   rs_ratio  = pair weekly close / BTC weekly close
#   rs_ema26  = EMA(26) of rs_ratio  (≈ 6-month relative trend)
#   long gate = rs_ratio >= rs_ema26 × RS_THRESHOLD
#
# If ratio is >= 85% of its own 6-month EMA: pair is keeping up with BTC → OK to Long.
# If ratio falls > 15% below its EMA: pair is in structural relative decline → skip Long.
# SHORT entries are unaffected — relative weakness is a VALID short signal.
#
# Experimental design (2×2):
#   DC55v20: no blacklist, no RS filter  (baseline)
#   DC55v24: static blacklist, no RS filter
#   DC55v25: RS filter only, no static blacklist  ← can RS replace blacklist?
#   DC55v26: RS filter + static blacklist          ← does combining help?
# ============================================================================


class DC55v25(DC55v20):
    """
    DC55v20 + dynamic Relative Strength vs BTC weekly filter (Long only).

    pair/BTC ratio < EMA(26) × RS_THRESHOLD → skip LONG entries.
    Rationale: structural losers lose ground vs BTC for months before their
    breakouts start failing. A 6-month EMA on the ratio captures this lag.
    SHORT unblocked — relative weakness is valid for short entries.
    BTC/USDT:USDT is its own reference (ratio = 1.0) → filter never triggers.
    NaN (insufficient history, first 26 weeks) → treated as OK (allow entries).
    """
    RS_THRESHOLD = 0.85

    def informative_pairs(self):
        return [("BTC/USDT:USDT", "1w")]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_indicators(dataframe, metadata)
        pair     = metadata["pair"]
        btc_1w   = self.dp.get_pair_dataframe("BTC/USDT:USDT", "1w")
        pair_1w  = self.dp.get_pair_dataframe(pair, "1w")

        if (btc_1w is not None and not btc_1w.empty
                and pair_1w is not None and not pair_1w.empty):
            rs  = pair_1w[["date", "close"]].copy().rename(columns={"close": "pair_close"})
            btc = btc_1w[["date", "close"]].copy().rename(columns={"close": "btc_close"})
            rs  = pd.merge_asof(
                rs.sort_values("date"), btc.sort_values("date"),
                on="date", direction="backward",
            )
            rs["rs_ratio"]   = rs["pair_close"] / rs["btc_close"]
            rs["rs_ema26"]   = rs["rs_ratio"].ewm(span=26, adjust=False).mean()
            # shift(1): only completed weekly candles → no lookahead
            rs["rs_ok_long"] = (
                (rs["rs_ratio"] >= rs["rs_ema26"] * self.RS_THRESHOLD)
                .shift(1).fillna(True).astype(int)
            )
            dataframe = merge_informative_pair(
                dataframe, rs[["date", "rs_ok_long"]], self.timeframe, "1w", ffill=True
            )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        if "rs_ok_long_1w" in dataframe.columns:
            dataframe.loc[dataframe["rs_ok_long_1w"] == 0, "enter_long"] = 0
        return dataframe


class DC55v26(DC55v25):
    """
    DC55v25 (dynamic RS filter) + static blacklist of confirmed structural losers.

    Tests whether combining the dynamic filter with explicit blacklist of known-bad
    pairs outperforms either approach alone. If DC55v26 ≈ DC55v25, the dynamic
    filter already handles what the blacklist was doing (ideal outcome).
    If DC55v26 > DC55v25, the blacklist adds value on top of the filter.
    """
    BLACKLIST = frozenset([
        "SUSHI/USDT:USDT",
        "ATOM/USDT:USDT",
        "YFI/USDT:USDT",
        "CRV/USDT:USDT",
    ])

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if metadata["pair"] in self.BLACKLIST:
            return dataframe
        return super().populate_entry_trend(dataframe, metadata)
