"""
Telegram message formatting and sending for deal alerts.
ใช้ pattern เดียวกับ alert_bot/telegram_alert.py (HTML parse mode, env var เดิม)
"""

import os

import requests

from deal_bot.detector import Deal

# ยิ่งดาวเยอะ = ยิ่งเป็นราคาที่แทบไม่เคยเห็น
DEPTH_STARS = [
    (45, "⭐⭐⭐", "ต่ำสุดที่เคยเก็บได้"),
    (30, "⭐⭐", "ลดลึกกว่าปกติมาก"),
    (20, "⭐", "ลดจริงเกินค่าเฉลี่ย"),
    (0, "•", "ลดพอสมควร"),
]


def _stars(discount_pct: float) -> tuple[str, str]:
    for threshold, star, label in DEPTH_STARS:
        if discount_pct >= threshold:
            return star, label
    return "•", ""


def format_deal(deal: Deal, link: str) -> str:
    star, label = _stars(deal.discount_pct)
    p = deal.product

    msg = (
        f"🏷️ <b>{p.name}</b>\n"
        f"\n"
        f"💵 ราคาตอนนี้: <b>{deal.price:,.0f} ฿</b>\n"
        f"📉 ลด <b>{deal.discount_pct:.0f}%</b> จากราคาปกติ {deal.ref_price:,.0f} ฿"
        f"  (ประหยัด {deal.saving:,.0f} ฿)\n"
        f"\n"
        f"📊 <b>เทียบกับประวัติราคาจริง</b>\n"
        f"   ต่ำกว่าที่ควรจะเป็น {deal.off_trend_pct:.0f}%\n"
        f"   ถูกที่สุดในรอบ {deal.low_days} วัน\n"
        f"   อยู่ใน {deal.percentile:.0f}% ล่างสุดของราคาที่เคยเห็น\n"
        f"   ข้อมูลสะสม {deal.history_days} วัน\n"
    )

    # ของไอทีถูกลงเองตามอายุรุ่น — บอกไปตรง ๆ ว่ารอแล้วได้อะไร
    # เสียยอดคลิกบ้างก็ยอม ความเชื่อถือคือสิ่งเดียวที่ทำให้ช่องนี้มีค่า
    if deal.drift_pct_30d <= -3.0:
        msg += (
            f"\n"
            f"⏳ <i>รุ่นนี้ราคาไหลลงเองเดือนละ ~{abs(deal.drift_pct_30d):.0f}% "
            f"ถ้าไม่รีบใช้ รออีกเดือนอาจได้ราคาใกล้เคียงกัน</i>\n"
        )
    elif deal.drift_pct_30d >= 2.0:
        msg += (
            f"\n"
            f"📈 <i>รุ่นนี้ราคาขยับขึ้นเดือนละ ~{deal.drift_pct_30d:.0f}% "
            f"(ของเริ่มขาด) รอบนี้ถือว่าจังหวะดี</i>\n"
        )

    if deal.inflate_guard:
        msg += (
            f"\n"
            f"🛡️ <i>ตรวจพบว่าร้านดันราคาขึ้นก่อนลด — "
            f"ตัวเลข % ข้างบนคิดจากราคาก่อนโดนดันแล้ว</i>\n"
        )

    msg += (
        f"\n"
        f"{star} {label}\n"
        f"\n"
        f"🛒 <a href=\"{link}\">ดูสินค้า</a>\n"
        f"\n"
        f"<i>ลิงก์นี้เป็น affiliate — ราคาที่คุณจ่ายเท่าเดิม</i>\n"
        f"<i>เราไม่ได้อ่านป้ายลดราคาของร้าน แต่เทียบกับราคาที่บันทึกเองทุกวัน</i>"
    )
    return msg


def send_telegram(message: str, token: str = None, chat_id: str = None) -> bool:
    token = token or os.environ.get("TELEGRAM_TOKEN")
    chat_id = chat_id or os.environ.get("DEAL_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("[telegram] TELEGRAM_TOKEN or chat id not set — skipping send")
        print("[telegram] Message that would be sent:")
        print(message)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }, timeout=10)

    if resp.status_code == 200:
        print("[telegram] Sent OK")
        return True
    print(f"[telegram] Failed: {resp.status_code} {resp.text}")
    return False
