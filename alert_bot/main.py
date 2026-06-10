#!/usr/bin/env python3
"""
Fibonacci alert bot entry point.
Run manually or via GitHub Actions (scheduled hourly).

Usage:
  python -m alert_bot.main

Env vars:
  TELEGRAM_TOKEN   - Telegram bot token (from @BotFather)
  TELEGRAM_CHAT_ID - Your chat/channel ID (get from @userinfobot)
  TOP_N            - How many top-volume pairs to scan (default: 20)
  PAIRS            - Override with specific pairs, comma-separated
                     e.g. "BTC/USDT,ETH/USDT,SOL/USDT"
                     If set, TOP_N is ignored.
"""

import os
import sys
from alert_bot.scanner import scan_all
from alert_bot.telegram_alert import format_alert, send_telegram


def main():
    # Allow manual pair override; otherwise scan top N by volume
    pairs_env = os.environ.get("PAIRS", "").strip()
    top_n     = int(os.environ.get("TOP_N", "20"))

    if pairs_env:
        pairs = [p.strip() for p in pairs_env.split(",") if p.strip()]
        print(f"[main] Scanning fixed list: {pairs}")
        setups = scan_all(pairs=pairs)
    else:
        print(f"[main] Scanning top {top_n} OKX pairs by 24H volume...")
        setups = scan_all(pairs=None, top_n=top_n)

    if not setups:
        print("[main] No Fibonacci setups on current candle.")
        return 0

    print(f"\n[main] {len(setups)} setup(s) found — sending alerts...")
    for setup in setups:
        msg = format_alert(setup)
        print(f"\n{'='*60}")
        print(msg)
        print('='*60)
        send_telegram(msg)

    return len(setups)


if __name__ == "__main__":
    sys.exit(0 if main() >= 0 else 1)
