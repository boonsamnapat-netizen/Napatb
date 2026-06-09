"""
Telegram message formatting and sending for Fibonacci setup alerts.
"""

import os
import requests
from alert_bot.scanner import FiboSetup


TREND_STARS = {0: "⚠️", 1: "⚠️", 2: "⭐", 3: "⭐⭐", 4: "⭐⭐⭐"}
TREND_LABEL = {
    0: "No trend filters",
    1: "Weak trend",
    2: "Trend confirmed (2/4)",
    3: "Strong trend (3/4)",
    4: "Very strong trend (4/4)",
}


def format_alert(setup: FiboSetup) -> str:
    risk_pct    = abs(setup.risk_pct)
    reward_pct  = setup.reward_pct
    rr          = setup.rr_ratio
    stars       = TREND_STARS.get(setup.trend_score, "⭐")
    trend_label = TREND_LABEL.get(setup.trend_score, "")

    candle_str = setup.candle_time.strftime("%Y-%m-%d %H:%M UTC")

    msg = (
        f"🎯 <b>FIBO SETUP: {setup.pair}</b>\n"
        f"\n"
        f"⏰ Candle: {candle_str}\n"
        f"💵 Current close: <b>${setup.close:,.2f}</b>\n"
        f"\n"
        f"📥 <b>Entry zone</b>\n"
        f"   Low  (61.8%): ${setup.entry_low:,.2f}\n"
        f"   High (50.0%): ${setup.entry_high:,.2f}\n"
        f"\n"
        f"🔴 <b>Stop Loss</b> (78.6%): ${setup.sl:,.2f}\n"
        f"   Risk: -{risk_pct:.1f}%\n"
        f"\n"
        f"🎯 <b>Take Profit</b> (161.8% ext): ${setup.tp:,.2f}\n"
        f"   Reward: +{reward_pct:.1f}%\n"
        f"\n"
        f"⚡ <b>R:R = {rr:.1f}:1</b>\n"
        f"\n"
        f"📐 Swing structure\n"
        f"   Low:  ${setup.swing_low:,.2f}\n"
        f"   High: ${setup.swing_high:,.2f}\n"
        f"\n"
        f"{stars} Trend: {trend_label}\n"
        f"\n"
        f"⚠️ <i>This is an alert — you decide whether to enter.</i>\n"
        f"<i>Check higher-TF structure before clicking BUY.</i>"
    )
    return msg


def send_telegram(message: str, token: str = None, chat_id: str = None) -> bool:
    token   = token   or os.environ.get("TELEGRAM_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("[telegram] TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set — skipping send")
        print("[telegram] Message that would be sent:")
        print(message)
        return False

    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }, timeout=10)

    if resp.status_code == 200:
        print(f"[telegram] Sent OK")
        return True
    else:
        print(f"[telegram] Failed: {resp.status_code} {resp.text}")
        return False


def send_no_setup_heartbeat(pairs: list[str], token: str = None, chat_id: str = None):
    """Hourly heartbeat when no setup found (optional, disabled by default)."""
    pass  # uncomment and send a short "no setup" message if you want it
