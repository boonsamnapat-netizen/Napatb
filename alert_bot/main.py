#!/usr/bin/env python3
"""
Fibonacci alert bot entry point.
Run manually or via GitHub Actions (scheduled hourly).

Usage:
  python -m alert_bot.main

Env vars:
  TELEGRAM_TOKEN   - Telegram bot token (from @BotFather)
  TELEGRAM_CHAT_ID - Your chat/channel ID (get from @userinfobot)
  PAIRS            - Comma-separated pairs to scan (default: BTC/USDT,ETH/USDT)
"""

import os
import sys
from alert_bot.scanner import scan_all
from alert_bot.telegram_alert import format_alert, send_telegram


PAIRS = os.environ.get("PAIRS", "BTC/USDT,ETH/USDT").split(",")


def main():
    print(f"[main] Scanning {PAIRS}...")
    setups = scan_all(PAIRS)

    if not setups:
        print("[main] No Fibonacci setups detected on current candle.")
        return 0

    print(f"[main] Found {len(setups)} setup(s)!")
    for setup in setups:
        msg = format_alert(setup)
        print(f"\n{'='*60}")
        print(msg)
        print('='*60)
        send_telegram(msg)

    return len(setups)


if __name__ == "__main__":
    count = main()
    sys.exit(0)
