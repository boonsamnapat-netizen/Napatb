#!/usr/bin/env python3
"""
Probe — ตรวจว่าดึงราคาจาก URL หนึ่ง ๆ ได้จริงไหม ก่อนเอาเข้า watchlist

  python -m deal_bot.probe                        # ตรวจทุกรายการใน watchlist (รวมที่ปิดอยู่)
  python -m deal_bot.probe https://... https://... # ตรวจ URL ที่ระบุ

ทำไมต้องมีเครื่องมือนี้แยก: URL ที่ดึงราคาไม่ได้จะกลายเป็นสินค้าที่ "ไม่มีประวัติราคา"
เงียบ ๆ อยู่ใน watchlist หลายเดือนโดยไม่มีใครรู้ ตรวจตั้งแต่วันแรกถูกกว่ามาก

ผลลัพธ์เขียนลง deal_bot/probe_report.md เพื่อให้เห็นย้อนหลังว่าเว็บไหนเคยพัง
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from deal_bot.config import WATCHLIST_PATH, Product
from deal_bot.sources import (
    HttpSource,
    PriceUnavailable,
    extract_jsonld_price,
    extract_regex_price,
)

REPORT_PATH = Path(__file__).parent / "probe_report.md"


def probe_product(source: HttpSource, product: Product) -> dict:
    """คืนผลการตรวจหนึ่งรายการ พร้อมบอกว่าราคามาจากวิธีไหน"""
    row = {
        "id": product.id,
        "merchant": product.merchant,
        "ok": False,
        "price": None,
        "method": "—",
        "in_stock": None,
        "note": "",
    }

    try:
        resp = source.session.get(product.url, timeout=20)
    except Exception as e:
        row["note"] = f"เชื่อมต่อไม่ได้: {type(e).__name__}"
        return row

    if resp.status_code != 200:
        row["note"] = f"HTTP {resp.status_code}"
        return row

    price, in_stock = extract_jsonld_price(resp.text)
    if price is not None:
        row.update(ok=True, price=price, method="JSON-LD", in_stock=in_stock)
        return row

    price = extract_regex_price(resp.text, product.price_regex)
    if price is not None:
        row.update(ok=True, price=price, method="regex", in_stock=True)
        return row

    has_ld = "application/ld+json" in resp.text
    row["note"] = (
        "มี JSON-LD แต่ไม่มี offers.price — ต้องตั้ง price_regex"
        if has_ld else
        "ไม่มี JSON-LD และ regex ไม่ match (หน้าอาจ render ด้วย JS)"
    )
    return row


def write_report(rows: list[dict]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ok = sum(1 for r in rows if r["ok"])

    lines = [
        "# Probe Report — ตรวจการดึงราคา",
        "",
        f"**รันเมื่อ:** {now}",
        f"**ผ่าน:** {ok}/{len(rows)}",
        "",
        "| สินค้า | ร้าน | ผล | ราคา | วิธี | มีของ | หมายเหตุ |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        price = f"{r['price']:,.0f}" if r["price"] else "—"
        stock = "" if r["in_stock"] is None else ("✅" if r["in_stock"] else "❌")
        lines.append(
            f"| {r['id']} | {r['merchant']} | {'✅' if r['ok'] else '❌'} | "
            f"{price} | {r['method']} | {stock} | {r['note']} |"
        )

    lines += [
        "",
        "---",
        "",
        "แก้ยังไงเมื่อ ❌:",
        "1. หน้า render ด้วย JS → ใช้ร้านอื่นที่ขายรุ่นเดียวกัน หรือรอ API จาก network",
        "2. มี JSON-LD แต่ไม่มีราคา → ตั้ง `price_regex` ใน watchlist.json",
        "3. HTTP 403/429 → เว็บกันบอท ลดความถี่ หรือตัดสินค้านั้นทิ้ง",
        "",
    ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"[probe] Report written to {REPORT_PATH}")


def main() -> int:
    urls = sys.argv[1:]

    if urls:
        products = [
            Product(id=f"probe-{i+1}", name=url[:60], url=url,
                    category="probe", commission_pct=0.0)
            for i, url in enumerate(urls)
        ]
    else:
        raw = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
        # ตรวจทุกรายการ รวมที่ enabled=false เพราะนั่นคือของที่รอ verify อยู่พอดี
        products = [Product(**item) for item in raw.get("products", [])]
        products = [p for p in products if p.source == "http"]

    if not products:
        print("[probe] ไม่มีอะไรให้ตรวจ")
        return 0

    print(f"[probe] ตรวจ {len(products)} รายการ...")
    source = HttpSource()
    rows = []
    for product in products:
        row = probe_product(source, product)
        rows.append(row)
        mark = "✅" if row["ok"] else "❌"
        detail = f"{row['price']:,.0f} ฿ ({row['method']})" if row["ok"] else row["note"]
        print(f"[probe] {mark} {product.id}: {detail}")

    write_report(rows)
    failed = sum(1 for r in rows if not r["ok"])
    print(f"[probe] เสร็จ — ผ่าน {len(rows) - failed}/{len(rows)}")
    return failed


if __name__ == "__main__":
    sys.exit(0 if main() == 0 else 1)
