#!/usr/bin/env python3
"""
Deal bot entry point.
Run manually or via GitHub Actions (scheduled twice daily).

Usage:
  python -m deal_bot.main            # รันจริง
  DEAL_DRY_RUN=1 python -m deal_bot.main   # ราคาสังเคราะห์ ไม่ส่ง Telegram

Env vars:
  TELEGRAM_TOKEN          - Telegram bot token
  DEAL_TELEGRAM_CHAT_ID   - แชนเนลดีล (แยกจากแชนเนลเทรด; ไม่ตั้งจะใช้ TELEGRAM_CHAT_ID)
  INVOLVE_API_KEY/SECRET  - ถ้าไม่ตั้ง จะใช้ลิงก์ตรง (ยังเก็บราคาได้ปกติ แต่ไม่ได้ค่าคอม)
  DEAL_DRY_RUN=1          - โหมดซ้อม
  ดูเกณฑ์ทั้งหมดที่ปรับได้ใน deal_bot/config.py
"""

import os
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from deal_bot.affiliate import build_link, make_tag
from deal_bot.config import Thresholds, load_watchlist
from deal_bot.detector import Deal, Skip, evaluate
from deal_bot.sources import PriceUnavailable, get_source
from deal_bot.store import PriceStore
from deal_bot.telegram_alert import format_deal, send_telegram

STATUS_PATH = Path(__file__).parent / "status.md"
DRY_RUN = os.environ.get("DEAL_DRY_RUN") == "1"


def collect_prices(store: PriceStore, products, cfg: Thresholds):
    """ดึงราคารอบนี้ทุกชิ้น แล้วบันทึกลง DB — เก็บทุกครั้งแม้ไม่มีดีล"""
    results = []
    for product in products:
        source = get_source(product.source)
        now = datetime.now(timezone.utc)
        try:
            point = source.fetch(product)
        except PriceUnavailable as e:
            print(f"[deal_bot] ⚠️  {product.id}: {e}")
            store.log_fetch(product.id, now, ok=False, detail=str(e))
            continue
        except Exception as e:  # แหล่งราคาพังไม่ควรทำให้ทั้งรอบล่ม
            print(f"[deal_bot] ❌ {product.id} unexpected: {e}")
            store.log_fetch(product.id, now, ok=False, detail=repr(e))
            continue

        store.record_price(product.id, point)
        store.log_fetch(product.id, now, ok=True)
        history = store.history(product.id, days=cfg.window_days)
        results.append((product, history))
        print(f"[deal_bot] {product.id}: {point.price:,.0f} ฿ ({len(history)} จุดในหน้าต่าง)")
    return results


def find_deals(store: PriceStore, snapshots, cfg: Thresholds):
    deals, skips = [], []
    for product, history in snapshots:
        outcome = evaluate(product, history, cfg, store.last_alert(product.id))
        if isinstance(outcome, Deal):
            deals.append(outcome)
            print(f"[deal_bot] ✅ {product.id}: -{outcome.discount_pct:.0f}% "
                  f"score={outcome.score:.0f}")
        else:
            skips.append(outcome)
            print(f"[deal_bot]    {product.id}: {outcome.reason} {outcome.detail}")

    deals.sort(key=lambda d: d.score, reverse=True)
    return deals, skips


def announce(store: PriceStore, deals: list[Deal], cfg: Thresholds) -> int:
    """ยิงดีลที่คะแนนสูงสุด ไม่เกิน max_alerts_per_run ชิ้น"""
    sent = 0
    for deal in deals[:cfg.max_alerts_per_run]:
        tag = make_tag(deal.product)
        link = build_link(deal.product, tag)
        message = format_deal(deal, link)

        if DRY_RUN:
            print(f"\n{'=' * 60}\n{message}\n{'=' * 60}")
        else:
            send_telegram(message)

        store.record_alert(
            tag=tag,
            product_id=deal.product.id,
            ts=datetime.now(timezone.utc),
            price=deal.price,
            ref_price=deal.ref_price,
            discount_pct=deal.discount_pct,
            score=deal.score,
        )
        sent += 1

    if len(deals) > cfg.max_alerts_per_run:
        print(f"[deal_bot] อีก {len(deals) - cfg.max_alerts_per_run} ดีลถูกพักไว้รอบหน้า "
              f"(กันสแปมช่อง)")
    return sent


def write_status(store: PriceStore, products, deals, skips, sent: int) -> None:
    """
    รายงานสถานะ commit กลับ repo ทุกรอบ — ตอบ 2 คำถาม:
      1. เก็บข้อมูลครบไหม (แหล่งไหนพัง)
      2. ทำไมถึงไม่มีดีล (เกณฑ์ตึงไป หรือแค่ยังไม่มีของถูกจริง)
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    reasons = Counter(s.reason for s in skips)

    lines = [
        "# Deal Bot Status",
        "",
        f"**อัปเดต:** {now}" + ("  _(dry-run)_" if DRY_RUN else ""),
        "",
        "## ภาพรวม",
        "| ตัวชี้วัด | ค่า |",
        "|---|---|",
        f"| สินค้าที่เฝ้าอยู่ | {len(products)} |",
        f"| ดีลที่ผ่านเกณฑ์รอบนี้ | {len(deals)} |",
        f"| ยิงแจ้งเตือน | {sent} |",
        "",
        "## เหตุผลที่ไม่ผ่าน",
    ]

    if reasons:
        lines += ["| เหตุผล | จำนวน |", "|---|---|"]
        lines += [f"| {reason} | {count} |" for reason, count in reasons.most_common()]
    else:
        lines.append("*ผ่านหมดทุกชิ้น*")

    lines += ["", "## ความครบของข้อมูลราคา", "| สินค้า | จุดราคา | เก็บตั้งแต่ | ล่าสุด |", "|---|---|---|---|"]
    for pid, count, first, last in sorted(store.coverage()):
        lines.append(f"| {pid} | {count} | {first[:10]} | {last[:10]} |")
    if not store.coverage():
        lines.append("| *ยังไม่มีข้อมูล* | | | |")

    lines += ["", "## แจ้งเตือนล่าสุด", "| เวลา | สินค้า | ราคา | อ้างอิง | ลด | tag |", "|---|---|---|---|---|---|"]
    recent = store.recent_alerts(10)
    for ts, pid, price, ref, disc, tag in recent:
        lines.append(f"| {ts[:16]} | {pid} | {price:,.0f} | {ref:,.0f} | {disc:.0f}% | `{tag}` |")
    if not recent:
        lines.append("| *ยังไม่เคยยิง* | | | | | |")

    lines += [
        "",
        "---",
        "",
        "> tag ในตารางบนคือ sub-id ที่ฝังไปกับลิงก์ ใช้จับคู่กับ conversion report",
        "> เพื่อคำนวณ EPC ตอนต่อระบบวัดผลในเฟสถัดไป",
        "",
    ]

    STATUS_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"[deal_bot] Status written to {STATUS_PATH}")


def main() -> int:
    cfg = Thresholds.from_env()
    products = load_watchlist()

    if not products:
        print("[deal_bot] watchlist ว่าง — เพิ่มสินค้าใน deal_bot/watchlist.json ก่อน")
        return 0

    store = PriceStore()
    try:
        snapshots = collect_prices(store, products, cfg)
        deals, skips = find_deals(store, snapshots, cfg)
        sent = announce(store, deals, cfg)
        write_status(store, products, deals, skips, sent)
    finally:
        store.close()

    print(f"[deal_bot] เสร็จสิ้น — {len(deals)} ดีล, ยิง {sent} ครั้ง")
    return sent


if __name__ == "__main__":
    try:
        sys.exit(0 if main() >= 0 else 1)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
