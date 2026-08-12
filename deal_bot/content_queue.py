"""
Content queue — ผลลัพธ์หลักของบอทตัวนี้

ช่องทางเดียวของเราคือ Facebook Reels ซึ่งแปลว่าบอทไม่ได้มีหน้าที่ "แจ้งเตือน"
อีกต่อไป หน้าที่จริงคือตอบคำถามเดียว:

    "สัปดาห์นี้ควรถ่ายคลิปสินค้าตัวไหน และพูดตัวเลขอะไร"

ไฟล์ที่ออกมาจึงเป็นคิวงานถ่ายคลิป ไม่ใช่ log — เปิดแล้วถ่ายได้เลย
โดยไม่ต้องกลับไปเปิด dashboard ที่ไหนอีก
"""

from datetime import datetime, timezone
from pathlib import Path

from deal_bot.detector import Deal

QUEUE_PATH = Path(__file__).parent / "content_queue.md"


def hook_line(deal: Deal) -> str:
    """
    ประโยคเปิดคลิปที่พูดได้เลย — เลือกมุมที่ "จริงและตรวจสอบได้" เท่านั้น
    ไม่ใช้คำว่าถูกที่สุด/ห้ามพลาด เพราะพิสูจน์ไม่ได้ และเป็นคำที่ทุกคนใช้จนไร้ความหมาย
    """
    if deal.low_days >= deal.history_days:
        return (f"ราคาต่ำสุดตั้งแต่เราเริ่มจดมา {deal.history_days} วัน "
                f"— ตอนนี้ {deal.price:,.0f} บาท")
    if deal.inflate_guard:
        return (f"ร้านขึ้นราคาก่อนลด แต่คิดจากราคาก่อนโดนดันแล้ว "
                f"ยังถูกลงจริง {deal.discount_pct:.0f}%")
    if deal.drift_pct_30d <= -5:
        return (f"รุ่นนี้ปกติถูกลงเดือนละ {abs(deal.drift_pct_30d):.0f}% "
                f"แต่รอบนี้ต่ำกว่าที่ควรจะเป็นถึง {deal.off_trend_pct:.0f}%")
    return (f"ถูกที่สุดในรอบ {deal.low_days} วัน "
            f"และต่ำกว่าราคาปกติ {deal.discount_pct:.0f}%")


def talking_points(deal: Deal) -> list[str]:
    """ตัวเลขที่พูดได้ในคลิป — ทุกบรรทัดมาจากข้อมูลที่เราจดเอง ไม่ใช่ป้ายร้าน"""
    points = [
        f"ราคาตอนนี้ {deal.price:,.0f} บาท (ปกติช่วงนี้ {deal.ref_price:,.0f})",
        f"ประหยัด {deal.saving:,.0f} บาท คิดเป็น {deal.discount_pct:.0f}%",
        f"ต่ำกว่าราคาที่ควรจะเป็นวันนี้ {deal.off_trend_pct:.0f}%",
        f"ถูกที่สุดในรอบ {deal.low_days} วัน จากข้อมูลที่จดมา {deal.history_days} วัน",
    ]
    if deal.drift_pct_30d <= -3:
        points.append(
            f"บอกคนดูตามตรง: รุ่นนี้ราคาไหลลงเองเดือนละ ~{abs(deal.drift_pct_30d):.0f}% "
            f"ถ้าไม่รีบใช้ รอได้"
        )
    elif deal.drift_pct_30d >= 2:
        points.append(
            f"รุ่นนี้ราคาขยับขึ้นเดือนละ ~{deal.drift_pct_30d:.0f}% (ของเริ่มขาด) "
            f"รอบนี้จังหวะดี"
        )
    if deal.inflate_guard:
        points.append("⚠️ ตรวจพบร้านดันราคาขึ้นก่อนลด — ตัวเลขข้างบนคิดจากฐานก่อนโดนดันแล้ว")
    return points


def render(deals: list[Deal], links: dict[str, str], skipped: int, watched: int) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# คิวถ่ายคลิป — Facebook Reels",
        "",
        f"**อัปเดต:** {now}  ·  เฝ้าอยู่ {watched} ชิ้น  ·  ไม่ผ่านเกณฑ์ {skipped} ชิ้น",
        "",
    ]

    if not deals:
        lines += [
            "## ยังไม่มีอะไรควรถ่าย",
            "",
            "ไม่ใช่บั๊ก — แปลว่ารอบนี้ไม่มีสินค้าไหนที่ราคาผิดปกติจริง",
            "การถ่ายคลิปเรื่องส่วนลดที่ไม่มีอยู่จริงคือวิธีที่เร็วที่สุดในการเสียคนดู",
            "ดูเหตุผลรายชิ้นได้ที่ `status.md`",
            "",
        ]
        return "\n".join(lines)

    lines.append(f"## {len(deals)} ชิ้นที่ควรถ่าย (เรียงตามคะแนน)")
    lines.append("")

    for i, deal in enumerate(deals, 1):
        link = links.get(deal.product.id, deal.product.url)
        lines += [
            f"### {i}. {deal.product.name}",
            "",
            f"- [ ] ถ่ายแล้ว &nbsp;&nbsp; - [ ] โพสต์แล้ว",
            "",
            f"**เปิดคลิปด้วย:** {hook_line(deal)}",
            "",
            "**ตัวเลขที่พูดได้:**",
        ]
        lines += [f"- {point}" for point in talking_points(deal)]
        lines += [
            "",
            f"**ค่าคอมคาดหวัง:** ~{deal.expected_commission:,.0f} บาท/ออเดอร์",
            "",
            f"**ลิงก์ (ใส่ใน caption):**",
            "",
            f"```\n{link}\n```",
            "",
            "> อย่าลืมเขียนกำกับว่าเป็นลิงก์ affiliate",
            "",
            "---",
            "",
        ]

    lines += [
        "## วิธีใช้",
        "",
        "1. ถ่ายคลิปตามลำดับในนี้ — อันบนสุดคุ้มที่สุด",
        "2. tag สินค้าใน Reels ผ่าน Facebook Affiliate Partnerships (ฟองคลิกได้บนคลิป)",
        "3. แปะลิงก์ข้างบนใน caption ด้วยอีกทาง — FB caption กดลิงก์ได้",
        "4. ตัวเลขทุกตัวในนี้มาจากราคาที่เราจดเอง ไม่ใช่ป้ายลดราคาของร้าน",
        "   พูดได้เต็มปากว่าตรวจสอบแล้ว",
        "",
    ]
    return "\n".join(lines)


def write(deals: list[Deal], links: dict[str, str], skipped: int, watched: int) -> None:
    QUEUE_PATH.write_text(render(deals, links, skipped, watched), encoding="utf-8")
    print(f"[queue] เขียนคิวถ่ายคลิป {len(deals)} ชิ้น -> {QUEUE_PATH}")
