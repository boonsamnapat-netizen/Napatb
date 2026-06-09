"""
VWAPMomentumAI -- FreqAI XGBoost Mean Reversion Strategy (15m)
Phase D: Upgraded from 5m to 15m timeframe

Why 15m:
  - 5m fees (0.10% round-trip) = 12% of TP=0.8% -- fee drag kills edge
  - 15m candles smoother: fewer false RSI/BB signals from microstructure noise
  - Can set TP=1.5%, SL=0.75% -> R:R=2:1, breakeven WR=37% post-fees (achievable)

Signal: Mean reversion -- buy oversold below session VWAP in ranging markets.
  Phase C.2 on 5m showed WR=31.8% (mean-reversion tendency confirmed but fee-limited).
  On 15m same signal should have higher WR (cleaner candles) and better fee ratio.

Entry conditions:
  - RSI(14) < 32 AND > 18 (oversold, not crashing)
  - BB %B < 0.15 (near or below lower Bollinger Band, 20-period 2sigma)
  - VWAP deviation < -0.3% (below 08:00 UTC session VWAP)
  - ADX(14) < 25 (ranging market -- mean reversion valid)

R:R = 2:1 (TP 1.5% / SL 0.75%)
  Fee-adjusted breakeven WR: ~37% (OKX 0.05% taker = 0.10% round-trip)
  15m mean reversion WR target: 40-50%
Time window: 08:00-20:00 UTC (all liquid sessions)
"""

import numpy as np
import pandas as pd
from freqtrade.strategy import IStrategy
import talib.abstract as ta
from pandas import DataFrame
from datetime import datetime


class VWAPMomentumAI(IStrategy):
    INTERFACE_VERSION = 3

    # R:R = 2:1 -> breakeven WR = 33.3% gross, ~37% post-fees
    minimal_roi = {"0": 0.015}   # TP 1.5%
    stoploss = -0.0075            # SL 0.75% (~1x 15m ATR, clears noise floor)
    trailing_stop = False
    timeframe = "15m"

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    startup_candle_count = 200

    # All liquid sessions: London open through US close
    TRADE_HOUR_START = 8
    TRADE_HOUR_END = 20

    # AI gate: set to base rate (0.30) to test if model has ANY discrimination.
    # Should pass ~50% of rule-based signals. If WR > 30.6% -> model adds value.
    # If WR ~= 30.6% -> model cannot discriminate, AI gate is useless.
    LONG_THRESHOLD = 0.30

    # ---- Session VWAP Helper -----------------------------------------------

    def _session_vwap_dev(self, dataframe: DataFrame) -> pd.Series:
        """Session VWAP deviation anchored at 08:00 UTC each day."""
        tp  = ((dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3).values
        vol = dataframe["volume"].values
        cls = dataframe["close"].values

        if "date" in dataframe.columns:
            dt = pd.to_datetime(dataframe["date"])
        else:
            dt = pd.to_datetime(dataframe.index)

        session_id = (dt - pd.Timedelta(hours=8)).dt.date.values
        dev = np.zeros(len(dataframe), dtype=float)

        for sid in np.unique(session_id):
            mask = session_id == sid
            cum_tpv = np.cumsum(tp[mask] * vol[mask])
            cum_v   = np.cumsum(vol[mask])
            cum_v   = np.where(cum_v == 0, np.nan, cum_v)
            vwap    = cum_tpv / cum_v
            dev[mask] = (cls[mask] - vwap) / np.where(vwap == 0, np.nan, vwap)

        return pd.Series(np.nan_to_num(dev, nan=0.0), index=dataframe.index)

    # ---- FreqAI Feature Engineering ----------------------------------------

    def feature_engineering_expand_all(
        self, dataframe: DataFrame, period: int,
        metadata: dict, **kwargs
    ) -> DataFrame:
        """Auto-expanded across timeframes (15m, 1h) and periods by FreqAI."""
        dataframe[f"%-rsi_{period}"] = ta.RSI(dataframe, timeperiod=period)
        dataframe[f"%-adx_{period}"] = ta.ADX(dataframe, timeperiod=period)
        dataframe[f"%-atr_{period}"] = ta.ATR(dataframe, timeperiod=period)
        dataframe[f"%-mfi_{period}"] = ta.MFI(dataframe, timeperiod=period)
        return dataframe

    def feature_engineering_expand_basic(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        # Momentum context
        dataframe["%-momentum_4c"]  = dataframe["close"].pct_change(4)   # 1h on 15m
        dataframe["%-momentum_12c"] = dataframe["close"].pct_change(12)  # 3h on 15m
        dataframe["%-momentum_1c"]  = dataframe["close"].pct_change(1)

        # Volume
        vol_sma20 = dataframe["volume"].rolling(20).mean().replace(0, np.nan)
        dataframe["%-vol_ratio_20"] = (dataframe["volume"] / vol_sma20).fillna(1.0)

        # Session VWAP deviation
        dataframe["%-vwap_dev"] = self._session_vwap_dev(dataframe)

        # RSI
        dataframe["%-rsi_7"]  = ta.RSI(dataframe, timeperiod=7)
        dataframe["%-rsi_14"] = ta.RSI(dataframe, timeperiod=14)

        # Bollinger Band %B and width
        _bb = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        upper  = _bb["upperband"]
        middle = _bb["middleband"]
        lower  = _bb["lowerband"]
        dataframe["%-bb_pct_b"] = (dataframe["close"] - lower) / (upper - lower + 1e-10)
        dataframe["%-bb_width"] = (upper - lower) / (middle + 1e-10)

        # ATR normalized
        dataframe["%-atr_norm"] = ta.ATR(dataframe, timeperiod=14) / (dataframe["close"] + 1e-10)

        # ADX
        dataframe["%-adx_14"] = ta.ADX(dataframe, timeperiod=14)

        # OBV rate of change
        obv = ta.OBV(dataframe)
        dataframe["%-obv_roc_5"] = (obv - obv.shift(5)) / (obv.shift(5).abs() + 1e-10)

        # MFI
        dataframe["%-mfi_14"] = ta.MFI(dataframe, timeperiod=14)

        # Candle features
        body = abs(dataframe["close"] - dataframe["open"])
        rng  = dataframe["high"] - dataframe["low"] + 1e-10
        dataframe["%-candle_body"]   = body / rng
        dataframe["%-close_gt_open"] = np.where(
            dataframe["close"] > dataframe["open"], 1.0, -1.0
        )

        # MACD histogram
        _macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["%-macd_hist"] = _macd["macdhist"]

        # CCI
        dataframe["%-cci_20"] = ta.CCI(dataframe, timeperiod=20)

        # EMA bias
        dataframe["%-ema_50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["%-ema_bias"] = (
            (dataframe["close"] - dataframe["%-ema_50"]) / (dataframe["%-ema_50"] + 1e-10)
        )

        return dataframe

    def feature_engineering_standard(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        if "date" in dataframe.columns:
            dt = pd.to_datetime(dataframe["date"])
        else:
            dt = pd.to_datetime(dataframe.index)

        dataframe["%-hour_sin"] = np.sin(2 * np.pi * dt.dt.hour / 24)
        dataframe["%-hour_cos"] = np.cos(2 * np.pi * dt.dt.hour / 24)
        dataframe["%-dow_sin"]  = np.sin(2 * np.pi * dt.dt.dayofweek / 7)
        dataframe["%-is_peak"]  = (
            (dt.dt.hour >= self.TRADE_HOUR_START) &
            (dt.dt.hour < self.TRADE_HOUR_END)
        ).astype(float)

        dataframe["%-pair_id"] = 1.0 if metadata.get("pair", "") == "BTC/USDT" else 2.0

        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        """
        Label: 1.0 = TP (+1.5%) hit before SL (-0.75%) in next 8 candles (2h on 15m)
               0.0 = SL hit first or neither
        """
        tp_pct    = 0.015    # 1.5%, matches minimal_roi
        sl_pct    = 0.0075   # 0.75%, matches stoploss
        lookahead = 8        # 2 hours at 15m resolution

        close  = dataframe["close"].values
        high   = dataframe["high"].values
        low    = dataframe["low"].values
        labels = np.zeros(len(dataframe), dtype=float)

        for offset in range(1, lookahead + 1):
            fh = np.roll(high, -offset).astype(float)
            fl = np.roll(low,  -offset).astype(float)
            fh[-offset:] = np.nan
            fl[-offset:] = np.nan

            tp_hit = fh >= close * (1 + tp_pct)
            sl_hit = fl <= close * (1 - sl_pct)

            undecided = labels == 0
            labels = np.where(undecided & tp_hit & ~sl_hit,  1, labels)
            labels = np.where(undecided & sl_hit & ~tp_hit, -1, labels)

        dataframe["&-target"] = (labels == 1).astype(float)
        return dataframe

    # ---- Indicators (non-FreqAI) -------------------------------------------

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self.freqai.start(dataframe, metadata, self)

        # Recompute after FreqAI strips %- columns
        dataframe["vwap_dev"] = self._session_vwap_dev(dataframe)

        vol_sma20 = dataframe["volume"].rolling(20).mean().replace(0, np.nan)
        dataframe["vol_ratio"] = (dataframe["volume"] / vol_sma20).fillna(1.0)

        dataframe["adx_14"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi_14"] = ta.RSI(dataframe, timeperiod=14)

        _bb = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        upper = _bb["upperband"]
        lower = _bb["lowerband"]
        dataframe["bb_pct_b"] = (dataframe["close"] - lower) / (upper - lower + 1e-10)

        return dataframe

    # ---- Entry Signals -----------------------------------------------------

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        hour = pd.to_datetime(dataframe["date"]).dt.hour
        in_window = (hour >= self.TRADE_HOUR_START) & (hour < self.TRADE_HOUR_END)

        if "&-target_mean" in dataframe.columns:
            ai_gate = dataframe["&-target_mean"] > self.LONG_THRESHOLD
        else:
            ai_gate = pd.Series(True, index=dataframe.index)

        oversold   = (dataframe["rsi_14"] < 32) & (dataframe["rsi_14"] > 18)
        bb_low     = dataframe["bb_pct_b"] < 0.15
        below_vwap = dataframe["vwap_dev"] < -0.003
        ranging    = dataframe["adx_14"] < 25

        entry = oversold & bb_low & below_vwap & ranging & in_window & ai_gate
        dataframe.loc[entry, "enter_long"] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def confirm_trade_entry(
        self, pair: str, order_type: str, amount: float, rate: float,
        time_in_force: str, current_time: datetime, entry_tag: str,
        side: str, **kwargs
    ) -> bool:
        hour = current_time.hour
        return self.TRADE_HOUR_START <= hour < self.TRADE_HOUR_END
