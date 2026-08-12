#!/usr/bin/env python3
"""
สร้าง watchlist จาก datafeed แทนการไล่ใส่สินค้าทีละตัว

  python -m deal_bot.feed_import --min-price 5000 --limit 40
  python -m deal_bot.feed_import --contains "monitor,จอ" --min-price 8000

อ่านค่า feed จาก watchlist.json (คีย์ "feed") แล้วเขียน "products" กลับเข้าไฟล์เดิม
ของเดิมที่มีอยู่จะถูกเก็บไว้ ไม่ทับ — เพิ่มเฉพาะตัวใหม่

ทำไมต้องกรองด้วยราคา: ค่าคอม Lazada หมวดไอทีอยู่ที่ 1-3% เกณฑ์ของบอทคือ
ค่าคอมคาดหวัง >= 100 บาท/ออเดอร์ แปลว่าของต่ำกว่า ~5,000 บาท ต่อให้ลดหนัก
ก็ไม่ผ่านด่านอยู่ดี ใส่เข้ามาก็แค่เปลืองโควตาการดึงราคา
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from deal_bot.config import WATCHLIST_PATH, load_feed_config
from deal_bot.sources import FeedSource


THAI_BLOCK = range(0x0E00, 0x0E80)


def slugify(text: str, limit: int = 32) -> str:
    """
    เก็บวรรณยุกต์/สระไทยไว้ด้วย — Python มองว่าไม่ใช่ alnum (เป็น combining mark)
    ถ้าตัดทิ้งชื่อจะเละจนอ่านไม่ออก
    """
    keep = [
        c if (c.isalnum() or ord(c) in THAI_BLOCK or c == "-") else "-"
        for c in text.lower()
    ]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:limit] or "item"


def product_id(name: str, feed_key: str) -> str:
    """
    id ต้อง unique เด็ดขาด เพราะมันคือคีย์ของประวัติราคาใน DB
    ถ้าสองสินค้าได้ id เดียวกัน ราคาของทั้งคู่จะถูกเขียนปนกันเป็นเส้นเดียว
    แล้ว detector จะอ่านมั่วโดยไม่มีอะไรฟ้อง — เติม hash ของ SKU กันไว้
    """
    digest = hashlib.sha1(feed_key.encode()).hexdigest()[:6]
    return f"lzd-{slugify(name)}-{digest}"


def main() -> int:
    ap = argparse.ArgumentParser(description="สร้าง watchlist จาก datafeed")
    ap.add_argument("--min-price", type=float, default=5000.0,
                    help="ราคาขั้นต่ำ (default 5000 — ต่ำกว่านี้ค่าคอมไม่ถึงเกณฑ์)")
    ap.add_argument("--max-price", type=float, default=0.0, help="ราคาสูงสุด (0 = ไม่จำกัด)")
    ap.add_argument("--contains", default="",
                    help="คั่นด้วย comma — เอาเฉพาะสินค้าที่ชื่อมีคำเหล่านี้")
    ap.add_argument("--category", default="electronics", help="หมวดที่จะใส่ใน watchlist")
    ap.add_argument("--commission", type=float, default=2.0, help="ค่าคอม % (ยืนยันจาก dashboard)")
    ap.add_argument("--limit", type=int, default=40, help="เอามากี่ตัว")
    ap.add_argument("--dry-run", action="store_true", help="แสดงผลเฉย ๆ ไม่เขียนไฟล์")
    args = ap.parse_args()

    cfg = load_feed_config()
    if not cfg.path:
        print("[import] ยังไม่ได้ตั้ง feed.path ใน watchlist.json")
        return 1

    source = FeedSource(cfg)
    rows = source.load()
    print(f"[import] feed มี {len(rows)} รายการ")

    keywords = [k.strip().lower() for k in args.contains.split(",") if k.strip()]

    picked = []
    for key, row in rows.items():
        name = str(cfg.get(row, "name", "")).strip()
        url = str(cfg.get(row, "url", "")).strip()
        price = FeedSource._to_float(cfg.get(row, "price"))

        if not (name and url and price):
            continue
        if price < args.min_price:
            continue
        if args.max_price and price > args.max_price:
            continue
        if keywords and not any(k in name.lower() for k in keywords):
            continue

        picked.append({
            "id": product_id(name, key),
            "name": name[:120],
            "url": url,
            "category": args.category,
            "commission_pct": args.commission,
            "source": "feed",
            "feed_id": key,
            "currency": "THB",
            "enabled": True,
        })

    # แพงก่อน — ค่าคอมต่อออเดอร์สูงกว่า คุ้มเวลาถ่ายคลิปมากกว่า
    picked.sort(key=lambda p: -(FeedSource._to_float(cfg.get(rows[p["feed_id"]], "price")) or 0))
    picked = picked[:args.limit]

    print(f"[import] ผ่านเกณฑ์ {len(picked)} รายการ")
    for p in picked[:10]:
        print(f"[import]   {p['id']}  —  {p['name'][:60]}")
    if len(picked) > 10:
        print(f"[import]   ... อีก {len(picked) - 10} รายการ")

    if args.dry_run:
        print("[import] --dry-run: ไม่เขียนไฟล์")
        return 0

    data = json.loads(Path(WATCHLIST_PATH).read_text(encoding="utf-8"))
    existing = {p["id"] for p in data.get("products", [])}
    added = [p for p in picked if p["id"] not in existing]
    data.setdefault("products", []).extend(added)

    Path(WATCHLIST_PATH).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[import] เพิ่ม {len(added)} รายการเข้า {WATCHLIST_PATH} "
          f"(ข้าม {len(picked) - len(added)} ที่มีอยู่แล้ว)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
