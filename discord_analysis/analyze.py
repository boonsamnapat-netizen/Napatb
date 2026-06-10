#!/usr/bin/env python3
"""
Analyze trading signals parsed from a Discord channel.

Usage:
    python analyze.py signals.csv
    python analyze.py signals.csv --plot

Requires: pandas, matplotlib (pip install pandas matplotlib)
"""

import argparse
import sys
import pandas as pd


def load(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def summary(df: pd.DataFrame):
    print("=" * 60)
    print("  DISCORD SIGNAL ANALYSIS SUMMARY")
    print("=" * 60)

    signals = df[df["signal_type"] == "SIGNAL"]
    results = df[df["signal_type"].isin(["SL_HIT", "TP_HIT"])]

    # ---- Overview ----
    print(f"\n📊 Total messages parsed : {len(df)}")
    print(f"   New signals           : {len(signals)}")
    print(f"   TP HIT (wins)         : {len(df[df['signal_type']=='TP_HIT'])}")
    print(f"   SL HIT (losses)       : {len(df[df['signal_type']=='SL_HIT'])}")

    # ---- Win Rate from results ----
    if len(results) > 0:
        win_rate = len(df[df["signal_type"] == "TP_HIT"]) / len(results) * 100
        print(f"\n🏆 Actual win rate  : {win_rate:.1f}%  ({len(df[df['signal_type']=='TP_HIT'])}/{len(results)} resolved trades)")

    # ---- Latest bot-reported stats ----
    latest_signal = signals.dropna(subset=["win_rate_pct"]).tail(1)
    if not latest_signal.empty:
        row = latest_signal.iloc[0]
        print(f"\n📈 Bot-reported stats (latest signal):")
        print(f"   Win rate     : {row.get('win_rate_pct', 'N/A')}%")
        print(f"   Profit Factor: {row.get('profit_factor', 'N/A')}")
        wins   = row.get("wins")
        losses = row.get("losses")
        if pd.notna(wins) and pd.notna(losses):
            print(f"   W / L        : {int(wins)} / {int(losses)}")

    # ---- P&L Analysis ----
    pnl_data = results.dropna(subset=["pnl"])
    if not pnl_data.empty:
        total_pnl   = pnl_data["pnl"].sum()
        avg_win_pnl = pnl_data[pnl_data["pnl"] > 0]["pnl"].mean()
        avg_los_pnl = pnl_data[pnl_data["pnl"] < 0]["pnl"].mean()
        max_dd      = pnl_data["pnl"].cumsum().sub(pnl_data["pnl"].cumsum().cummax()).min()

        print(f"\n💰 P&L Summary (from result messages):")
        print(f"   Total P&L        : ${total_pnl:+.2f}")
        print(f"   Avg winning trade: ${avg_win_pnl:+.2f}" if pd.notna(avg_win_pnl) else "   Avg winning trade: N/A")
        print(f"   Avg losing trade : ${avg_los_pnl:+.2f}"  if pd.notna(avg_los_pnl)  else "   Avg losing trade : N/A")
        print(f"   Max drawdown     : ${max_dd:.2f}")

    # ---- Balance Curve ----
    bal_data = df.dropna(subset=["balance"]).sort_values("timestamp")
    if not bal_data.empty:
        start_bal = bal_data.iloc[0]["balance"]
        end_bal   = bal_data.iloc[-1]["balance"]
        print(f"\n💼 Account Balance:")
        print(f"   Start : ${start_bal:.2f}")
        print(f"   End   : ${end_bal:.2f}")
        print(f"   Change: ${end_bal - start_bal:+.2f}  ({(end_bal/start_bal - 1)*100:+.1f}%)")

    # ---- Direction breakdown ----
    if len(signals) > 0:
        dir_counts = signals["direction"].value_counts()
        print(f"\n📐 Signal Direction:")
        for d, c in dir_counts.items():
            print(f"   {d}: {c} signals")

    # ---- RR Stats ----
    rr_data = signals.dropna(subset=["rr"])
    if not rr_data.empty:
        print(f"\n⚖️  Risk/Reward:")
        print(f"   Avg RR  : {rr_data['rr'].mean():.2f}")
        print(f"   Min RR  : {rr_data['rr'].min():.2f}")
        print(f"   Max RR  : {rr_data['rr'].max():.2f}")

    # ---- Time range ----
    if len(df) > 0:
        print(f"\n📅 Date range: {df['timestamp'].min().date()} → {df['timestamp'].max().date()}")

    print("\n" + "=" * 60)


def plot_charts(df: pd.DataFrame):
    try:
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        print("matplotlib not installed. Run: pip install matplotlib")
        return

    fig, axes = plt.subplots(3, 1, figsize=(12, 12))
    fig.suptitle("Discord Signal Analysis — 3x-btc-signal", fontsize=14)

    # ---- Balance Curve ----
    ax = axes[0]
    bal = df.dropna(subset=["balance"]).sort_values("timestamp")
    if not bal.empty:
        ax.plot(bal["timestamp"], bal["balance"], marker="o", markersize=3, linewidth=1.5)
        ax.set_title("Account Balance Over Time")
        ax.set_ylabel("Balance ($)")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        ax.grid(True, alpha=0.3)
        fig.autofmt_xdate()

    # ---- P&L per trade ----
    ax2 = axes[1]
    results = df[df["signal_type"].isin(["SL_HIT", "TP_HIT"])].dropna(subset=["pnl"])
    if not results.empty:
        colors = ["green" if p > 0 else "red" for p in results["pnl"]]
        ax2.bar(range(len(results)), results["pnl"], color=colors, width=0.8)
        ax2.axhline(0, color="black", linewidth=0.8)
        ax2.set_title("P&L per Resolved Trade")
        ax2.set_ylabel("P&L ($)")
        ax2.set_xlabel("Trade #")
        ax2.grid(True, alpha=0.3, axis="y")

    # ---- Cumulative P&L ----
    ax3 = axes[2]
    if not results.empty:
        cum_pnl = results["pnl"].cumsum().values
        ax3.plot(range(len(cum_pnl)), cum_pnl, color="blue", linewidth=1.5)
        ax3.fill_between(range(len(cum_pnl)), cum_pnl, 0,
                         where=[v >= 0 for v in cum_pnl], alpha=0.2, color="green")
        ax3.fill_between(range(len(cum_pnl)), cum_pnl, 0,
                         where=[v < 0 for v in cum_pnl], alpha=0.2, color="red")
        ax3.axhline(0, color="black", linewidth=0.8)
        ax3.set_title("Cumulative P&L")
        ax3.set_ylabel("Cumulative P&L ($)")
        ax3.set_xlabel("Trade #")
        ax3.grid(True, alpha=0.3)

    plt.tight_layout()
    out = "signal_analysis.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Chart saved → {out}")
    plt.show()


def main():
    parser = argparse.ArgumentParser(description="Analyze Discord trading signals CSV")
    parser.add_argument("input", help="CSV file from parse_signals.py")
    parser.add_argument("--plot", action="store_true", help="Generate charts")
    args = parser.parse_args()

    df = load(args.input)
    if df.empty:
        print("No data found.")
        sys.exit(1)

    summary(df)

    if args.plot:
        plot_charts(df)


if __name__ == "__main__":
    main()
