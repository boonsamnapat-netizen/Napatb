"""
VWAPMomentumAI — FreqAI XGBoost Intraday Momentum Strategy
Phase C: Evidence-based upgrade from EMA cross

Signal basis: 30-minute intraday momentum (Shen, Urquhart, Wang 2022 Financial Review,
  doi:10.1111/fire.12290) validated on real BTC exchange data, Sharpe 1.15.
  + Session VWAP directional filter (08:00 UTC anchor)
  + Volume spike confirmation (1.3× 20-period average)
  + ADX regime gate (ADX > 20, trending markets only — filters choppy ranges)

R:R = 3:1 (TP 1.5% / SL 0.5%)
  Fee-adjusted breakeven WR: ~28% (OKX 0.05% taker = 0.10% round-trip)
Time window: 13:00–17:00 UTC (NYSE/LSE overlap, peak volume per Springer 2024 research)

Why EMA cross was abandoned (Phase B lessons):
  - EMA 5/13 cross on real 5m OKX data: WR=28.9% < 33.3% breakeven at R:R=2:1
  - Fee-adjusted breakeven at R:R=2:1 is actually 40% WR — far above actual 28.9%
  - Root cause: EMA cross signal fires during choppy / mean-reverting conditions too
"""

import numpy as np
import pandas as pd
from freqtrade.strategy import IStrategy
import talib.abstract as ta
from pandas import DataFrame
from datetime import datetime


class VWAPMomentumAI(IStrategy):
    INTERFACE_VERSION = 3

    # R:R = 3:1 → breakeven WR = 25% gross, ~28% net (0.10% OKX round-trip fees)
    minimal_roi = {"0": 0.015}  # TP 1.5%
    stoploss = -0.005            # SL 0.5% — 2× 5m ATR, clears noise floor
    trailing_stop = False
    timeframe = "5m"

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    startup_candle_count = 200

    # 13:00–17:00 UTC: NYSE open + LSE afternoon, highest liquidity and directional moves
    TRADE_HOUR_START = 13
    TRADE_HOUR_END = 17

    # FreqAI regressor predicts P(TP before SL); XGBoost base rate ~0.28-0.32
    # 0.40 = above-average confidence only
    LONG_THRESHOLD = 0.40

    # ─── Session VWAP Helper ─────────────────────────────────────────────────

    def _session_vwap_dev(self, dataframe: DataFrame) -> pd.Series:
        """Session VWAP deviation anchored at 08:00 UTC each day."""
        tp  = ((dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3).values
        vol = dataframe["volume"].values
        cls = dataframe["close"].values

        if "date" in dataframe.columns:
            dt = pd.to_datetime(dataframe["date"])
        else:
            dt = pd.to_datetime(dataframe.index)

        # Each session starts at 08:00 UTC; shift by 8h so date boundary = session boundary
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

    # ─── FreqAI Feature Engineering ─────────────────────────────────────────

    def feature_engineering_expand_all(
        self, dataframe: DataFrame, period: int,
        metadata: dict, **kwargs
    ) -> DataFrame:
        """Auto-expanded across timeframes and periods by FreqAI."""
        dataframe[f"%-rsi_{period}"] = ta.RSI(dataframe, timeperiod=period)
        dataframe[f"%-adx_{period}"] = ta.ADX(dataframe, timeperiod=period)
        dataframe[f"%-atr_{period}"] = ta.ATR(dataframe, timeperiod=period)
        dataframe[f"%-mfi_{period}"] = ta.MFI(dataframe, timeperiod=period)
        return dataframe

    def feature_engineering_expand_basic(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        """Computed once per timeframe (not per period)."""
        # 30-min / 15-min / 5-min momentum (core signal)
        dataframe["%-momentum_30m"] = dataframe["close"].pct_change(6)
        dataframe["%-momentum_15m"] = dataframe["close"].pct_change(3)
        dataframe["%-momentum_5m"]  = dataframe["close"].pct_change(1)

        # Volume ratios
        vol_sma20 = dataframe["volume"].rolling(20).mean().replace(0, np.nan)
        dataframe["%-vol_ratio_20"] = (dataframe["volume"] / vol_sma20).fillna(1.0)

        vol_30m     = dataframe["volume"].rolling(6).sum()
        vol_day_avg = (dataframe["volume"].rolling(288).mean() * 6).replace(0, np.nan)
        dataframe["%-vol_30m_ratio"] = (vol_30m / vol_day_avg).fillna(1.0)

        # Session VWAP deviation (08:00 UTC anchor)
        dataframe["%-vwap_dev"] = self._session_vwap_dev(dataframe)

        # RSI — fast (7) and standard (14) for different responsiveness
        dataframe["%-rsi_7"]  = ta.RSI(dataframe, timeperiod=7)
        dataframe["%-rsi_14"] = ta.RSI(dataframe, timeperiod=14)

        # Bollinger Band %B and bandwidth
        _bb = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        upper  = _bb["upperband"]
        middle = _bb["middleband"]
        lower  = _bb["lowerband"]
        dataframe["%-bb_pct_b"] = (dataframe["close"] - lower) / (upper - lower + 1e-10)
        dataframe["%-bb_width"] = (upper - lower) / (middle + 1e-10)

        # ATR normalized to price (volatility regime)
        dataframe["%-atr_norm"] = ta.ATR(dataframe, timeperiod=14) / (dataframe["close"] + 1e-10)

        # ADX (regime quality — trending vs ranging)
        dataframe["%-adx_14"] = ta.ADX(dataframe, timeperiod=14)

        # OBV rate of change (volume-price direction pressure)
        obv = ta.OBV(dataframe)
        dataframe["%-obv_roc_5"] = (obv - obv.shift(5)) / (obv.shift(5).abs() + 1e-10)

        # MFI (money flow: price × volume direction)
        dataframe["%-mfi_14"] = ta.MFI(dataframe, timeperiod=14)

        # Candle quality
        body = abs(dataframe["close"] - dataframe["open"])
        rng  = dataframe["high"] - dataframe["low"] + 1e-10
        dataframe["%-candle_body"]    = body / rng
        dataframe["%-close_gt_open"]  = np.where(dataframe["close"] > dataframe["open"], 1.0, -1.0)

        # MACD histogram (momentum confirmation)
        _macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["%-macd_hist"] = _macd["macdhist"]

        # CCI (overbought/oversold context)
        dataframe["%-cci_20"] = ta.CCI(dataframe, timeperiod=20)

        # EMA trend bias
        dataframe["%-ema_50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["%-ema_bias"] = (
            (dataframe["close"] - dataframe["%-ema_50"]) / (dataframe["%-ema_50"] + 1e-10)
        )

        return dataframe

    def feature_engineering_standard(
        self, dataframe: DataFrame, metadata: dict, **kwargs
    ) -> DataFrame:
        """Time and pair identity features."""
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
        Label: 1.0 = TP (+1.5%) hit before SL (-0.5%) in next 18 candles (90 min)
               0.0 = SL hit first, or neither target hit
        Exact match with minimal_roi and stoploss.
        """
        tp_pct    = 0.015   # matches minimal_roi
        sl_pct    = 0.005   # matches stoploss
        lookahead = 18      # 90 minutes at 5m resolution

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

    # ─── Indicators (non-FreqAI) ────────────────────────────────────────────

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self.freqai.start(dataframe, metadata, self)

        # FreqAI strips %-columns — recompute signal indicators with plain names
        dataframe["momentum_30m"] = dataframe["close"].pct_change(6)

        vol_sma20 = dataframe["volume"].rolling(20).mean().replace(0, np.nan)
        dataframe["vol_ratio"] = (dataframe["volume"] / vol_sma20).fillna(1.0)

        dataframe["vwap_dev"] = self._session_vwap_dev(dataframe)

        dataframe["adx_14"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi_14"] = ta.RSI(dataframe, timeperiod=14)

        return dataframe

    # ─── Entry Signals ────────────────────────────────────────────────────

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        hour = pd.to_datetime(dataframe["date"]).dt.hour
        in_window = (hour >= self.TRADE_HOUR_START) & (hour < self.TRADE_HOUR_END)

        # FreqAI gate — fallback to True if model not yet available
        if "&-target_mean" in dataframe.columns:
            ai_gate = dataframe["&-target_mean"] > self.LONG_THRESHOLD
        else:
            ai_gate = pd.Series(True, index=dataframe.index)

        # 30-min momentum: price up > 0.25% over last 6 candles
        momentum = dataframe["momentum_30m"] > 0.0025

        # VWAP filter: price at least 0.1% above session VWAP (momentum direction confirmed)
        above_vwap = dataframe["vwap_dev"] > 0.001

        # Volume confirmation: above-average activity
        vol_spike = dataframe["vol_ratio"] > 1.3

        # Regime gate: ADX > 20 = trending market where momentum persists
        trending = dataframe["adx_14"] > 20

        # RSI momentum zone: 45–70 (not oversold noise, not overbought extreme)
        rsi_zone = (dataframe["rsi_14"] > 45) & (dataframe["rsi_14"] < 70)

        entry = momentum & above_vwap & vol_spike & trending & rsi_zone & in_window & ai_gate
        dataframe.loc[entry, "enter_long"] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # ROI table handles TP (+1.5%), stoploss handles SL (-0.5%)
        return dataframe

    def confirm_trade_entry(
        self, pair: str, order_type: str, amount: float, rate: float,
        time_in_force: str, current_time: datetime, entry_tag: str,
        side: str, **kwargs
    ) -> bool:
        hour = current_time.hour
        return self.TRADE_HOUR_START <= hour < self.TRADE_HOUR_END
