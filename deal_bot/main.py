#!/usr/bin/env python3
"""
Deal bot entry point.
Run manually or via GitHub Actions (scheduled twice daily).

ช่องทางเดียวของโปรเจกต์นี้คือ **Facebook Reels** ผลลัพธ์หลักจึงเป็น
คิวถ่ายคลิป (`content_queue.md`) ไม่ใช่การแจ้งเตือน

Usage:
  python -m deal_bot.main
  DEAL_DRY_RUN=1 python -m deal_bot.main    # ราคาสังเคราะห์ ไม่แตะเน็ต

Env vars:
  INVOLVE_API_KEY/SECRET  - ถ้าไม่ตั้ง จะใช้ลิงก์ตรง (ยังเก็บราคาได้ แต่ไม่ได้ค่าคอม)
  DEAL_CHANNEL            - ช่องทางที่ฝังใน tag (default: fbreel)
  DEAL_TELEGRAM_ENABLED=1 - ส่ง Telegram ด้วย (ปิดไว้ — ไม่ได้อยู่ในแผนแล้ว)
  DEAL_DRY_RUN=1          - โหมดซ้อม
  ดูเกณฑ์ทั้งหมดที่ปรับได้ใน deal_bot/config.py
"""

import os
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from deal_bot import content_queue
from deal_bot.affiliate import build_link, make_tag
from deal_bot.config import Thresholds, load_feed_config, load_watchlist
from deal_bot.detector import Deal, Skip, evaluate
from deal_bot.sources import PriceUnavailable, build_sources
from deal_bot.store import PriceStore

STATUS_PATH = Path(__file__).parent / "status.md"
DRY_RUN = os.environ.get("DEAL_DRY_RUN") == "1"
TELEGRAM_ENABLED = os.environ.get("DEAL_TELEGRAM_ENABLED") == "1"


def collect_prices(store: PriceStore, products, sources, cfg: Thresholds):
    """ดึงราคารอบนี้ทุกชิ้น แล้วบันทึกลง DB — เก็บทุกครั้งแม้ไม่มีดีล"""
    results = []
    for product in products:
        source = sources.get(product.source) or sources["http"]
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
                  f"(ต่ำกว่าแนวโน้ม {outcome.off_trend_pct:.0f}%) score={outcome.score:.0f}")
        else:
            skips.append(outcome)
            print(f"[deal_bot]    {product.id}: {outcome.reason} {outcome.detail}")

    deals.sort(key=lambda d: d.score, reverse=True)
    return deals, skips


def queue_for_filming(store: PriceStore, deals: list[Deal], cfg: Thresholds):
    """
    เข้าคิวถ่ายคลิปเฉพาะตัวที่คะแนนสูงสุด

    จำกัดจำนวนต่อรอบด้วยเหตุผลเดียวกับตอนเป็นแชนเนล Telegram — เราถ่ายคลิป
    ได้สัปดาห์ละไม่กี่ตัวอยู่แล้ว คิวยาว 20 รายการคือคิวที่ไม่มีใครทำตาม
    ตัวที่เหลือไม่หายไปไหน รอบหน้ามันจะกลับมาถ้าราคายังดีอยู่
    """
    selected = deals[:cfg.max_alerts_per_run]
    links = {}

    for deal in selected:
        tag = make_tag(deal.product)
        links[deal.product.id] = build_link(deal.product, tag)

        store.record_alert(
            tag=tag,
            product_id=deal.product.id,
            ts=datetime.now(timezone.utc),
            price=deal.price,
            ref_price=deal.ref_price,
            discount_pct=deal.discount_pct,
            score=deal.score,
        )

        if TELEGRAM_ENABLED:
            from deal_bot.telegram_alert import format_deal, send_telegram
            send_telegram(format_deal(deal, links[deal.product.id]))

    if len(deals) > cfg.max_alerts_per_run:
        print(f"[deal_bot] อีก {len(deals) - cfg.max_alerts_per_run} ตัวรอรอบหน้า")

    return selected, links


def write_status(store: PriceStore, products, deals, skips, queued: int) -> None:
    """
    รายงานสถานะ commit กลับ repo ทุกรอบ — ตอบ 2 คำถาม:
      1. เก็บข้อมูลครบไหม (แหล่งไหนพัง)
      2. ทำไมถึงไม่มีอะไรให้ถ่าย (เกณฑ์ตึงไป หรือแค่ยังไม่มีของถูกจริง)
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    reasons = Counter(s.reason for s in skips)
    coverage = store.coverage()

    lines = [
        "# Deal Bot Status",
        "",
        f"**อัปเดต:** {now}" + ("  _(dry-run)_" if DRY_RUN else ""),
        "",
        "## ภาพรวม",
        "| ตัวชี้วัด | ค่า |",
        "|---|---|",
        f"| สินค้าที่เฝ้าอยู่ | {len(products)} |",
        f"| ผ่านเกณฑ์รอบนี้ | {len(deals)} |",
        f"| เข้าคิวถ่ายคลิป | {queued} |",
        "",
        "## เหตุผลที่ไม่ผ่าน",
    ]

    if reasons:
        lines += ["| เหตุผล | จำนวน |", "|---|---|"]
        lines += [f"| {reason} | {count} |" for reason, count in reasons.most_common()]
    else:
        lines.append("*ผ่านหมดทุกชิ้น*")

    lines += ["", "## ความครบของข้อมูลราคา",
              "| สินค้า | จุดราคา | เก็บตั้งแต่ | ล่าสุด |", "|---|---|---|---|"]
    for pid, count, first, last in sorted(coverage):
        lines.append(f"| {pid} | {count} | {first[:10]} | {last[:10]} |")
    if not coverage:
        lines.append("| *ยังไม่มีข้อมูล* | | | |")

    lines += ["", "## เข้าคิวล่าสุด",
              "| เวลา | สินค้า | ราคา | อ้างอิง | ลด | tag |", "|---|---|---|---|---|---|"]
    recent = store.recent_alerts(10)
    for ts, pid, price, ref, disc, tag in recent:
        lines.append(f"| {ts[:16]} | {pid} | {price:,.0f} | {ref:,.0f} | {disc:.0f}% | `{tag}` |")
    if not recent:
        lines.append("| *ยังไม่เคยเข้าคิว* | | | | | |")

    lines += [
        "",
        "---",
        "",
        "> tag ในตารางบนคือ sub-id ที่ฝังไปกับลิงก์ (มี channel อยู่ในนั้นแล้ว)",
        "> ใช้จับคู่กับ conversion report เพื่อคำนวณ EPC ต่อคลิปในเฟสถัดไป",
        "",
    ]

    STATUS_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"[deal_bot] Status written to {STATUS_PATH}")


def main() -> int:
    cfg = Thresholds.from_env()
    products = load_watchlist()

    if not products:
        print("[deal_bot] watchlist ว่าง — ใส่สินค้า Lazada ใน deal_bot/watchlist.json ก่อน")
        print("[deal_bot] ถ้ามี datafeed แล้ว ใช้: python -m deal_bot.feed_import --help")
        return 0

    sources = build_sources(load_feed_config())
    store = PriceStore()
    try:
        snapshots = collect_prices(store, products, sources, cfg)
        deals, skips = find_deals(store, snapshots, cfg)
        selected, links = queue_for_filming(store, deals, cfg)
        content_queue.write(selected, links, skipped=len(skips), watched=len(products))
        write_status(store, products, deals, skips, len(selected))
    finally:
        store.close()

    print(f"[deal_bot] เสร็จสิ้น — {len(deals)} ผ่านเกณฑ์, เข้าคิว {len(selected)}")
    return len(selected)


if __name__ == "__main__":
    try:
        sys.exit(0 if main() >= 0 else 1)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
