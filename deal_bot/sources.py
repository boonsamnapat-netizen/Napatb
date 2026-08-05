"""
Price sources — ที่มาของราคา

ออกแบบเป็น adapter เพราะแหล่งราคาจะเปลี่ยนแน่นอน (เว็บเปลี่ยน layout, ได้ API key
เพิ่ม, ย้าย marketplace) แต่ตัว detector ไม่ควรต้องรู้เรื่องพวกนี้เลย

ตอนนี้มี 2 ตัว:
  HttpSource — ดึงหน้าเว็บแล้วอ่านราคา (JSON-LD ก่อน แล้วค่อย regex)
  MockSource — ราคาสังเคราะห์แบบ deterministic ไว้ทดสอบ logic โดยไม่แตะเน็ต
"""

import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone

import requests

from deal_bot.config import Product
from deal_bot.store import PricePoint

USER_AGENT = os.environ.get(
    "DEAL_USER_AGENT",
    "Mozilla/5.0 (compatible; NapatbDealBot/0.1; +https://github.com/boonsamnapat-netizen/Napatb)",
)
TIMEOUT = int(os.environ.get("DEAL_HTTP_TIMEOUT", "20"))


class PriceUnavailable(Exception):
    """ดึงราคาไม่ได้รอบนี้ — ไม่ใช่เรื่องคอขาดบาดตาย ข้ามไปสินค้าถัดไป"""


# --- JSON-LD ------------------------------------------------------------
# เว็บ e-commerce ส่วนใหญ่ฝัง schema.org/Product ไว้ในหน้า เพราะ Google ต้องการ
# อ่านตรงนี้เสถียรกว่า scrape HTML มาก และไม่ต้องพึ่ง parser lib

_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _walk(node):
    """เดินทุก dict ในโครงสร้าง JSON-LD (บางเว็บซ้อน @graph หรือเป็น list)"""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def extract_jsonld_price(html: str) -> tuple[float | None, bool]:
    """คืน (ราคา, มีของไหม) จาก JSON-LD — คืน (None, True) ถ้าหาไม่เจอ"""
    for block in _LD_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue

        for node in _walk(data):
            offers = node.get("offers")
            if not offers:
                continue
            for offer in _walk(offers):
                raw = offer.get("price") or offer.get("lowPrice")
                if raw is None:
                    continue
                try:
                    price = float(str(raw).replace(",", "").strip())
                except ValueError:
                    continue
                availability = str(offer.get("availability", "")).lower()
                in_stock = "outofstock" not in availability and "soldout" not in availability
                if price > 0:
                    return price, in_stock
    return None, True


def extract_regex_price(html: str, pattern: str) -> float | None:
    """fallback สำหรับเว็บที่ไม่มี JSON-LD — ตั้ง capture group ไว้ที่ตัวเลข"""
    if not pattern:
        return None
    m = re.search(pattern, html, re.DOTALL)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "").strip())
    except (ValueError, IndexError):
        return None


class HttpSource:
    """ดึงราคาจากหน้าสินค้าโดยตรง"""

    name = "http"

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "th,en;q=0.8",
        })

    def fetch(self, product: Product) -> PricePoint:
        try:
            resp = self.session.get(product.url, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise PriceUnavailable(f"request failed: {e}") from e

        if resp.status_code != 200:
            raise PriceUnavailable(f"HTTP {resp.status_code}")

        price, in_stock = extract_jsonld_price(resp.text)
        if price is None:
            price = extract_regex_price(resp.text, product.price_regex)
            if price is None:
                raise PriceUnavailable("ไม่พบราคาใน JSON-LD และ regex ไม่ match")

        return PricePoint(ts=datetime.now(timezone.utc), price=price, in_stock=in_stock)


class MockSource:
    """
    ราคาสังเคราะห์ deterministic — ใช้ตอน DEAL_DRY_RUN=1

    รูปแบบ: ราคาฐานคงที่ + คลื่นไซน์ + สุ่มเล็กน้อยจาก hash (ไม่ใช้ random
    เพื่อให้รันซ้ำได้ผลเดิม เหมือน backtest) product ที่ id ลงท้ายด้วย "-deal"
    จะถูกกดราคาลง 30% เพื่อทดสอบว่า pipeline ยิง alert ออกจริง
    """

    name = "mock"

    def __init__(self, day_offset: int = 0):
        self.day_offset = day_offset

    def fetch(self, product: Product) -> PricePoint:
        seed = int(hashlib.md5(product.id.encode()).hexdigest()[:8], 16)
        base = 800 + (seed % 40) * 50
        wave = math.sin((self.day_offset + seed % 17) / 6.0) * base * 0.06
        price = base + wave

        if product.id.endswith("-deal"):
            price *= 0.70

        return PricePoint(ts=datetime.now(timezone.utc), price=round(price, 2), in_stock=True)


def get_source(name: str) -> HttpSource | MockSource:
    if name == "mock" or os.environ.get("DEAL_DRY_RUN") == "1":
        return MockSource()
    return HttpSource()
