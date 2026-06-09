"""
EMAXGBoost — FreqAI XGBoost Regressor wrapping EMA 5/13 Cross Strategy
Phase B: ML upgrade from rule-based scalper

Logic:
  - Feature engineering: EMA cross, volume, RSI, ATR, ADX, Bollinger, VWAP deviation, time
  - Label: 1.0 = price hits TP(0.5%) before SL(0.2%) in next 20 candles
           0.0 = price hits SL first or neither
  - Entry: EMA signal fires AND XGBoost predicted value > 0.6
  - TP/SL managed by ROI table and stoploss config
"""

import numpy as np
import pandas as pd
from freqtrade.strategy import IStrategy, merge_informative_pair
import talib.abstract as ta
from pandas import DataFrame
from datetime import datetime


class EMAXGBoost(IStrategy):
    INTERFACE_VERSION = 3

    # --- ROI / Stoploss ---
    minimal_roi = {"0": 0.005}          # TP 0.5%
    stoploss = -0.002                    # SL 0.2%
    trailing_stop = False
    timeframe = "5m"
    inf_timeframe = "15m"

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    startup_candle_count = 100

    # Trading window: 13:00–17:00 UTC only
    TRADE_HOUR_START = 13
    TRADE_HOUR_END = 17

    # FreqAI confidence threshold
    LONG_THRESHOLD  = 0.60
    SHORT_THRESHOLD = 0.60  # for future short support

    def feature_engineering_expand_all(
        self, dataframe: DataFrame, period: int,
        metadata: dict, **kwargs
    ) -> DataFrame:
        """Features that FreqAI automatically expands across timeframes and periods."""
        dataframe[f"%-ema_fast_{period}"] = ta.EMA(dataframe, timeperiod=period)
        dataframe[f"%-ema_slow_{period}"] = ta.EMA(dataframe, timeperiod=period * 2)
        dataframe[f"%-rsi_{period}"]      = ta.RSI(dataframe, timeperiod=period)
        dataframe[f"%-adx_{period}"]      = ta.ADX(dataframe, timeperiod=period)
        dataframe[f"%-atr_{period}"]      = ta.ATR(dataframe, timeperiod=period)
        dataframe[f"%-mfi_{period}"]      = ta.MFI(dataframe, timeperiod=period)
        return dataframe

    def feature_engineering_expand_basic(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        """Basic price and volume features."""
        # EMA cross signals (core strategy)
        dataframe["%-ema5"]  = ta.EMA(dataframe, timeperiod=5)
        dataframe["%-ema13"] = ta.EMA(dataframe, timeperiod=13)
        dataframe["%-ema50"] = ta.EMA(dataframe, timeperiod=50)

        # EMA cross spread (normalized)
        dataframe["%-ema_cross_spread"] = (
            (dataframe["%-ema5"] - dataframe["%-ema13"]) / dataframe["%-ema13"]
        )
        # EMA cross direction (1 = bullish, -1 = bearish)
        dataframe["%-ema_cross_dir"] = np.where(
            dataframe["%-ema5"] > dataframe["%-ema13"], 1, -1
        ).astype(float)
        # Cross event
        cross_up   = (dataframe["%-ema5"] > dataframe["%-ema13"]) & \
                     (dataframe["%-ema5"].shift(1) <= dataframe["%-ema13"].shift(1))
        cross_down = (dataframe["%-ema5"] < dataframe["%-ema13"]) & \
                     (dataframe["%-ema5"].shift(1) >= dataframe["%-ema13"].shift(1))
        dataframe["%-ema_cross_event"] = np.where(cross_up, 1, np.where(cross_down, -1, 0)).astype(float)

        # Trend (15m EMA50 relative to close)
        dataframe["%-trend_bias"] = np.where(
            dataframe["close"] > dataframe["%-ema50"], 1, -1
        ).astype(float)

        # Volume features
        dataframe["%-vol_ratio"] = dataframe["volume"] / (
            dataframe["volume"].rolling(20).mean().replace(0, np.nan)
        )
        dataframe["%-vol_ratio"] = dataframe["%-vol_ratio"].fillna(1.0)

        # Bollinger Band position
        _bb = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        upper = _bb["upperband"]
        lower = _bb["lowerband"]
        dataframe["%-bb_upper"] = upper
        dataframe["%-bb_lower"] = lower
        dataframe["%-bb_pos"] = (dataframe["close"] - lower) / (upper - lower + 1e-10)

        # VWAP deviation (daily reset)
        tp = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3
        dates = dataframe.index.date if hasattr(dataframe.index, 'date') else \
                pd.to_datetime(dataframe["date"]).dt.date
        dataframe["%-vwap_dev"] = 0.0
        for d in np.unique(dates):
            mask = dates == d
            cum_tpv = (tp[mask] * dataframe.loc[mask, "volume"]).cumsum()
            cum_v   = dataframe.loc[mask, "volume"].cumsum()
            vwap    = cum_tpv / cum_v.replace(0, np.nan)
            dataframe.loc[mask, "%-vwap_dev"] = (dataframe.loc[mask, "close"] - vwap) / vwap

        # ATR normalized
        dataframe["%-atr_norm"] = ta.ATR(dataframe, timeperiod=14) / dataframe["close"]

        # Price momentum
        dataframe["%-momentum_5"]  = dataframe["close"].pct_change(5)
        dataframe["%-momentum_15"] = dataframe["close"].pct_change(15)

        # Candle body / range
        dataframe["%-candle_body"] = abs(dataframe["close"] - dataframe["open"]) / \
                                      (dataframe["high"] - dataframe["low"] + 1e-10)

        # Upper wick ratio (wick hunting signal)
        body_top = dataframe[["open", "close"]].max(axis=1)
        dataframe["%-upper_wick"] = (dataframe["high"] - body_top) / \
                                     (dataframe["high"] - dataframe["low"] + 1e-10)

        # MACD histogram (momentum confirmation)
        _macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["%-macd_hist"] = _macd["macdhist"]

        # Rate of change (5 candles)
        dataframe["%-roc_5"] = dataframe["close"].pct_change(5)

        # CCI (overbought/oversold context)
        dataframe["%-cci_20"] = ta.CCI(dataframe, timeperiod=20)

        # OBV slope
        obv = ta.OBV(dataframe)
        dataframe["%-obv_slope"] = (obv - obv.shift(5)) / (obv.shift(5).abs() + 1e-10)

        # Bollinger bandwidth
        _bb2 = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        dataframe["%-bb_width"] = (_bb2["upperband"] - _bb2["lowerband"]) / (_bb2["middleband"] + 1e-10)

        return dataframe

    def feature_engineering_standard(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        """Time and market-specific features."""
        # Time features (important for session-aware trading)
        if "date" in dataframe.columns:
            dt = pd.to_datetime(dataframe["date"])
        else:
            dt = dataframe.index.to_series()
        dataframe["%-hour_sin"] = np.sin(2 * np.pi * dt.dt.hour / 24)
        dataframe["%-hour_cos"] = np.cos(2 * np.pi * dt.dt.hour / 24)
        dataframe["%-dow_sin"]  = np.sin(2 * np.pi * dt.dt.dayofweek / 7)
        dataframe["%-is_peak_session"] = (
            (dt.dt.hour >= self.TRADE_HOUR_START) &
            (dt.dt.hour < self.TRADE_HOUR_END)
        ).astype(float)

        # Pair identifier (for corr pairs)
        dataframe["%-pair_id"] = 1.0 if metadata.get("pair", "") == "BTC/USDT" else 2.0

        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        """
        Vectorized label: 1.0 = TP(+0.5%) hit before SL(-0.2%) in next 20 candles
                          0.0 = SL hit first or neither
        Regressor learns to predict probability-like score (0-1).
        """
        tp_pct    = 0.005
        sl_pct    = 0.002
        lookahead = 20

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
            labels = np.where(undecided & tp_hit & ~sl_hit, 1,  labels)
            labels = np.where(undecided & sl_hit & ~tp_hit, -1, labels)

        dataframe["&-target"] = (labels == 1).astype(float)
        return dataframe

    # ─── Indicator calculation (non-FreqAI) ───────────────────────────────
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self.freqai.start(dataframe, metadata, self)
        return dataframe

    # ─── Entry signals ─────────────────────────────────────────────────────
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        hour = pd.to_datetime(dataframe["date"]).dt.hour

        # EMA cross condition (original rule-based signal)
        ema_long = (
            (dataframe["%-ema5"] > dataframe["%-ema13"]) &
            (dataframe["%-ema5"].shift(1) <= dataframe["%-ema13"].shift(1)) &
            (dataframe["%-vol_ratio"] > 1.3) &
            (dataframe["%-trend_bias"] == 1)
        )

        # FreqAI confidence gate
        ai_confident = dataframe["&-target_mean"] > self.LONG_THRESHOLD

        # Trading window gate
        in_window = (hour >= self.TRADE_HOUR_START) & (hour < self.TRADE_HOUR_END)

        dataframe.loc[
            ema_long & ai_confident & in_window, "enter_long"
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Let ROI and stoploss handle exits; add AI-based early exit
        low_confidence = dataframe["&-target_mean"] < 0.4

        dataframe.loc[low_confidence, "exit_long"] = 1
        return dataframe

    def confirm_trade_entry(
        self, pair: str, order_type: str, amount: float, rate: float,
        time_in_force: str, current_time: datetime, entry_tag: str,
        side: str, **kwargs
    ) -> bool:
        # Extra guard: reject trades outside peak session
        hour = current_time.hour
        return self.TRADE_HOUR_START <= hour < self.TRADE_HOUR_END
