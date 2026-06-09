#!/usr/bin/env python3
"""
AI Trading Backtest - Phase 2+3
Strategies: A=EMA Cross+Volume, B=VWAP Reversion, C=RSI+Bollinger, D=BTC Lead->ETH/SOL
Pairs: BTC/USDT, ETH/USDT, SOL/USDT
Exchange: Binance (public API)
"""

import ccxt
import pandas as pd
import numpy as np
import time
import warnings
import ssl
import urllib3
warnings.filterwarnings('ignore')
urllib3.disable_warnings()
ssl._create_default_https_context = ssl._create_unverified_context

# === CONFIG ===
PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
START_DATE = '2024-09-01'
END_DATE   = '2025-03-01'
INITIAL_CAPITAL = 10_000  # USDT
COMMISSION = 0.0004        # 0.04% per side (Binance taker)
TRADE_HOUR_START = 13      # UTC
TRADE_HOUR_END   = 17      # UTC

STRAT_PARAMS = {
    'A_EMA_Cross': {'tp': 0.004, 'sl': 0.002},
    'B_VWAP_Rev':  {'tp': 0.003, 'sl': 0.002},
    'C_RSI_BB':    {'tp': 0.004, 'sl': 0.0025},
    'D_BTC_Lead':  {'tp': 0.004, 'sl': 0.0025},
}

# === DATA ===
# Research-based parameters (Sep 2024 – Mar 2025 bull market)
SYNTH_PARAMS = {
    'BTC/USDT': {'start_price': 57_000, 'annual_vol': 0.41, 'annual_drift': 0.80},
    'ETH/USDT': {'start_price': 2_400,  'annual_vol': 0.60, 'annual_drift': 0.60},
    'SOL/USDT': {'start_price': 150,    'annual_vol': 0.80, 'annual_drift': 1.20},
}
# Correlation matrix: BTC-ETH 0.85, BTC-SOL 0.83, ETH-SOL 0.80
CORR = np.array([[1.00, 0.85, 0.83],
                 [0.85, 1.00, 0.80],
                 [0.83, 0.80, 1.00]])

def generate_synthetic_data(start, end, timeframe='5m'):
    """Correlated GBM using research-derived volatility, drift, and intraday patterns."""
    freq = '5min' if timeframe == '5m' else '15min'
    dt_min = 5 if timeframe == '5m' else 15
    idx = pd.date_range(start=start, end=end, freq=freq, tz='UTC')
    n = len(idx)
    dt = dt_min / (252 * 24 * 60)  # fraction of trading year

    # Intraday vol multiplier (from research)
    vol_mult = np.where(
        (idx.hour >= 13) & (idx.hour < 17), 1.4,
        np.where((idx.hour >= 2) & (idx.hour < 8), 0.6, 1.0)
    )

    L = np.linalg.cholesky(CORR)
    np.random.seed(42 if timeframe == '5m' else 99)
    Z = np.random.normal(0, 1, (n, 3)) @ L.T  # correlated normals

    data = {}
    for i, (pair, p) in enumerate(SYNTH_PARAMS.items()):
        mu = p['annual_drift'] - 0.5 * p['annual_vol'] ** 2
        rets = mu * dt + p['annual_vol'] * np.sqrt(dt) * Z[:, i] * vol_mult
        close = p['start_price'] * np.exp(np.cumsum(rets))
        close = np.maximum(close, p['start_price'] * 0.2)

        r = p['annual_vol'] * np.sqrt(dt) * close * vol_mult
        opens  = np.roll(close, 1); opens[0] = close[0]
        highs  = np.maximum(opens, close) + np.abs(np.random.normal(0, r * 0.3, n))
        lows   = np.minimum(opens, close) - np.abs(np.random.normal(0, r * 0.3, n))
        base_v = {'BTC/USDT': 500, 'ETH/USDT': 3000, 'SOL/USDT': 50_000}[pair]
        volume = base_v * vol_mult * np.abs(np.random.normal(1, 0.3, n))

        data[pair] = pd.DataFrame(
            {'open': opens, 'high': highs, 'low': lows, 'close': close, 'volume': volume},
            index=idx
        )
    return data

def fetch_ohlcv(symbol, timeframe, start, end):
    """Try live Binance first; fall back to synthetic data if network blocked."""
    try:
        exchange = ccxt.binance({'enableRateLimit': True, 'verify': False})
        exchange.session.verify = False
        since = int(pd.Timestamp(start, tz='UTC').timestamp() * 1000)
        end_ts = int(pd.Timestamp(end, tz='UTC').timestamp() * 1000)
        rows = []
        while since < end_ts:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            since = batch[-1][0] + 1
            time.sleep(0.08)
        df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
        df = df[df['ts'] < pd.Timestamp(end, tz='UTC')].copy()
        df.set_index('ts', inplace=True)
        return df.astype(float), 'live'
    except Exception:
        return None, 'synthetic'

# === INDICATORS ===
def ema(s, n):   return s.ewm(span=n, adjust=False).mean()
def sma(s, n):   return s.rolling(n).mean()

def rsi(s, n=14):
    d = s.diff()
    gain = d.clip(lower=0).ewm(com=n-1, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(com=n-1, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))

def bollinger(s, n=20, k=2):
    mid = sma(s, n)
    std = s.rolling(n).std()
    return mid + k*std, mid, mid - k*std

def vwap_daily(df):
    tp = (df['high'] + df['low'] + df['close']) / 3
    grp = df.index.date
    cum_tpv = (tp * df['volume']).groupby(grp).cumsum()
    cum_v   = df['volume'].groupby(grp).cumsum()
    return cum_tpv / cum_v

# === BACKTEST ENGINE ===
def run_backtest(df, signals, tp_pct, sl_pct):
    capital = float(INITIAL_CAPITAL)
    position = 0
    entry_price = 0.0
    trades = []

    highs  = df['high'].values
    lows   = df['low'].values
    closes = df['close'].values
    sigs   = signals.values

    for i in range(len(sigs)):
        if position == 0:
            if sigs[i] != 0:
                position = int(sigs[i])
                entry_price = closes[i]
                capital -= capital * COMMISSION
        else:
            h, l = highs[i], lows[i]
            if position == 1:
                sl_p = entry_price * (1 - sl_pct)
                tp_p = entry_price * (1 + tp_pct)
                if l <= sl_p:
                    pnl = sl_p / entry_price - 1
                    capital *= (1 + pnl); capital -= capital * COMMISSION
                    trades.append({'dir': 'L', 'res': 'SL', 'pnl': pnl * 100})
                    position = 0
                elif h >= tp_p:
                    pnl = tp_p / entry_price - 1
                    capital *= (1 + pnl); capital -= capital * COMMISSION
                    trades.append({'dir': 'L', 'res': 'TP', 'pnl': pnl * 100})
                    position = 0
            else:
                sl_p = entry_price * (1 + sl_pct)
                tp_p = entry_price * (1 - tp_pct)
                if h >= sl_p:
                    pnl = entry_price / sl_p - 1
                    capital *= (1 + pnl); capital -= capital * COMMISSION
                    trades.append({'dir': 'S', 'res': 'SL', 'pnl': pnl * 100})
                    position = 0
                elif l <= tp_p:
                    pnl = entry_price / tp_p - 1
                    capital *= (1 + pnl); capital -= capital * COMMISSION
                    trades.append({'dir': 'S', 'res': 'TP', 'pnl': pnl * 100})
                    position = 0

    if not trades:
        return None

    t = pd.DataFrame(trades)
    wins = t[t['res'] == 'TP']
    losses = t[t['res'] == 'SL']
    win_rate = len(wins) / len(t) * 100
    total_ret = (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    pf = abs(wins['pnl'].sum() / losses['pnl'].sum()) if len(losses) > 0 and losses['pnl'].sum() != 0 else 999
    avg_win = wins['pnl'].mean() if len(wins) else 0
    avg_loss = losses['pnl'].mean() if len(losses) else 0

    return {
        'trades': len(t),
        'win_rate': win_rate,
        'return_pct': total_ret,
        'profit_factor': pf,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'capital': capital,
    }

# === STRATEGIES ===
def in_window(idx):
    return (idx.hour >= TRADE_HOUR_START) & (idx.hour < TRADE_HOUR_END)

def strat_a(df5, df15):
    """EMA Cross (9/21) + Volume Filter + 15m Trend Bias"""
    df = df5.copy()
    df['e9']  = ema(df['close'], 9)
    df['e21'] = ema(df['close'], 21)
    df['vsma'] = sma(df['volume'], 20)

    df15 = df15.copy()
    df15['e50'] = ema(df15['close'], 50)
    trend = np.where(df15['close'] > df15['e50'], 1, -1)
    trend_s = pd.Series(trend, index=df15.index).reindex(df.index, method='ffill').fillna(0)

    cross_up   = (df['e9'] > df['e21']) & (df['e9'].shift(1) <= df['e21'].shift(1))
    cross_down = (df['e9'] < df['e21']) & (df['e9'].shift(1) >= df['e21'].shift(1))
    vol_ok = df['volume'] > 1.5 * df['vsma']
    win = in_window(df.index)

    sig = pd.Series(0, index=df.index)
    sig[cross_up   & vol_ok & (trend_s == 1)  & win] = 1
    sig[cross_down & vol_ok & (trend_s == -1) & win] = -1
    return sig

def strat_b(df5):
    """VWAP Reversion + RSI confirmation"""
    df = df5.copy()
    df['vwap'] = vwap_daily(df)
    df['rsi']  = rsi(df['close'], 14)
    df['dev']  = (df['close'] - df['vwap']) / df['vwap']
    win = in_window(df.index)

    sig = pd.Series(0, index=df.index)
    sig[(df['dev'] < -0.004) & (df['rsi'] < 40) & win] = 1
    sig[(df['dev'] >  0.004) & (df['rsi'] > 60) & win] = -1
    return sig

def strat_c(df5):
    """RSI + Bollinger Band mean reversion"""
    df = df5.copy()
    df['rsi'] = rsi(df['close'], 14)
    df['bbu'], _, df['bbl'] = bollinger(df['close'], 20, 2)
    win = in_window(df.index)

    sig = pd.Series(0, index=df.index)
    sig[(df['close'] <= df['bbl']) & (df['rsi'] < 35) & win] = 1
    sig[(df['close'] >= df['bbu']) & (df['rsi'] > 65) & win] = -1
    return sig

def strat_d(df_target, df_btc):
    """BTC EMA cross → enter ETH/SOL in same direction within 3 candles"""
    btc = df_btc.copy()
    btc['e9']  = ema(btc['close'], 9)
    btc['e21'] = ema(btc['close'], 21)
    up   = (btc['e9'] > btc['e21']) & (btc['e9'].shift(1) <= btc['e21'].shift(1))
    down = (btc['e9'] < btc['e21']) & (btc['e9'].shift(1) >= btc['e21'].shift(1))

    df = df_target.copy()
    df['rsi'] = rsi(df['close'], 14)
    win = in_window(df.index)

    up_r   = up.reindex(df.index, method='ffill', fill_value=False)
    down_r = down.reindex(df.index, method='ffill', fill_value=False)
    up_w   = up_r | up_r.shift(1).fillna(False) | up_r.shift(2).fillna(False)
    down_w = down_r | down_r.shift(1).fillna(False) | down_r.shift(2).fillna(False)

    sig = pd.Series(0, index=df.index)
    sig[up_w   & (df['rsi'] < 60) & win] = 1
    sig[down_w & (df['rsi'] > 40) & win] = -1
    return sig

# === MAIN ===
def main():
    print("=" * 70)
    print(" AI TRADING BACKTEST — Phase 2+3")
    print(f" Period : {START_DATE}  →  {END_DATE}")
    print(f" Window : {TRADE_HOUR_START}:00–{TRADE_HOUR_END}:00 UTC  |  Capital: ${INITIAL_CAPITAL:,}")
    print(f" Fee    : {COMMISSION*100:.2f}% per side (Binance taker)")
    print("=" * 70)

    # --- Fetch / generate data ---
    print("\n[1/3] Loading market data...")

    def probe_live():
        try:
            ex = ccxt.binance({'enableRateLimit': True, 'verify': False})
            ex.session.verify = False
            result = ex.fetch_ohlcv('BTC/USDT', '5m', limit=5)
            return bool(result and len(result) > 0)
        except Exception:
            return False

    if probe_live():
        print("  ✓ Binance API reachable — fetching live data")
        d5, d15 = {}, {}
        for pair in PAIRS:
            print(f"  {pair}  5m  ...", end='', flush=True)
            df, _ = fetch_ohlcv(pair, '5m',  START_DATE, END_DATE)
            d5[pair] = df; print(f" {len(df):,} candles")
            print(f"  {pair}  15m ...", end='', flush=True)
            df, _ = fetch_ohlcv(pair, '15m', START_DATE, END_DATE)
            d15[pair] = df; print(f" {len(df):,} candles")
    else:
        print("  ⚠ Binance API blocked (cloud network policy)")
        print("  → Using synthetic data (research-derived: vol/drift/correlation)")
        print("  → Swap to live data: run on local machine with internet access\n")
        d5  = generate_synthetic_data(START_DATE, END_DATE, '5m')
        d15 = generate_synthetic_data(START_DATE, END_DATE, '15m')
        for pair in PAIRS:
            print(f"  {pair}  5m={len(d5[pair]):,}  15m={len(d15[pair]):,} candles")

    # --- Run strategies ---
    print("\n[2/3] Running strategies...")
    results = {}
    for pair in PAIRS:
        results[pair] = {}
        f5, f15 = d5[pair], d15[pair]
        p = STRAT_PARAMS

        print(f"\n  {pair}")
        for label, sig_fn, kwargs in [
            ('A_EMA_Cross', strat_a, {'df5': f5, 'df15': f15}),
            ('B_VWAP_Rev',  strat_b, {'df5': f5}),
            ('C_RSI_BB',    strat_c, {'df5': f5}),
        ]:
            sig = sig_fn(**kwargs)
            r = run_backtest(f5, sig, p[label]['tp'], p[label]['sl'])
            results[pair][label] = r
            n = r['trades'] if r else 0
            print(f"    {label:<14} → {n} signals fired")

        if pair != 'BTC/USDT':
            sig = strat_d(f5, d5['BTC/USDT'])
            r = run_backtest(f5, sig, p['D_BTC_Lead']['tp'], p['D_BTC_Lead']['sl'])
            results[pair]['D_BTC_Lead'] = r
            n = r['trades'] if r else 0
            print(f"    {'D_BTC_Lead':<14} → {n} signals fired")

    # --- Print results table ---
    print("\n" + "=" * 70)
    print("[3/3] BACKTEST RESULTS")
    print("=" * 70)
    hdr = f"{'Pair':<12} {'Strategy':<14} {'Trades':>7} {'WinRate':>8} {'Return':>8} {'ProfFact':>9} {'AvgWin':>8} {'AvgLoss':>9}"
    print(hdr)
    print("-" * 70)

    best = []
    for pair in PAIRS:
        for strat, r in results[pair].items():
            if not r or r['trades'] == 0:
                print(f"{pair:<12} {strat:<14} {'—':>7}")
                continue
            wr  = f"{r['win_rate']:.1f}%"
            ret = f"{r['return_pct']:+.2f}%"
            pf  = f"{r['profit_factor']:.2f}"
            aw  = f"{r['avg_win']:+.3f}%"
            al  = f"{r['avg_loss']:+.3f}%"
            print(f"{pair:<12} {strat:<14} {r['trades']:>7} {wr:>8} {ret:>8} {pf:>9} {aw:>8} {al:>9}")
            if r['win_rate'] >= 50 and r['return_pct'] > 0:
                best.append((pair, strat, r))

    print("=" * 70)
    if best:
        print("\n★  STRATEGIES PASSING CONSERVATIVE FILTER (WinRate≥50%, Return>0)")
        for pair, strat, r in sorted(best, key=lambda x: -x[2]['return_pct']):
            print(f"   {pair} / {strat}  →  WR {r['win_rate']:.1f}%  |  Return {r['return_pct']:+.2f}%  |  PF {r['profit_factor']:.2f}")
    else:
        print("\n⚠  No strategy passed the conservative filter — parameter tuning needed.")

    print("\nDone.")

if __name__ == '__main__':
    main()
