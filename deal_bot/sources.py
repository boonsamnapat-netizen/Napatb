"""
Price sources — ที่มาของราคา

ออกแบบเป็น adapter เพราะแหล่งราคาจะเปลี่ยนแน่นอน แต่ detector ไม่ต้องรู้เลย

  FeedSource — อ่านราคาจาก datafeed ของ advertiser  ← ทางหลักตอนนี้ (Lazada)
  HttpSource — ดึงหน้าเว็บแล้วอ่านราคา (JSON-LD ก่อน แล้วค่อย regex)
  MockSource — ราคาสังเคราะห์แบบ deterministic ไว้ทดสอบโดยไม่แตะเน็ต

ทำไม FeedSource เป็นทางหลัก: Lazada หน้าสินค้าเป็น JS ล้วนและกัน scrape
การดึงราคาจากหน้าเว็บจึงไม่ใช่แค่ยาก แต่เปราะและผิดเจตนาของแพลตฟอร์ม
datafeed เป็นช่องทางที่ advertiser ตั้งใจให้ publisher ใช้ — เสถียรกว่ามาก
และให้ทั้ง catalog แทนที่จะต้องไล่ใส่ URL ทีละตัว
"""

import csv
import hashlib
import io
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

from deal_bot.config import FeedConfig, Product
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


class FeedSource:
    """
    อ่านราคาจาก datafeed ของ advertiser (CSV หรือ JSON)

    โหลดไฟล์ครั้งเดียวต่อรอบแล้วทำ index ไว้ — ไม่ใช่ยิงเน็ตต่อสินค้าหนึ่งชิ้น
    path เป็นได้ทั้งไฟล์ในเครื่องและ URL (network บางเจ้าให้ลิงก์ดาวน์โหลด)
    """

    name = "feed"

    def __init__(self, cfg: FeedConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self._index: dict[str, dict] | None = None

    # --- โหลด + index ---------------------------------------------------

    def _read_raw(self) -> str:
        path = self.cfg.path
        if not path:
            raise PriceUnavailable("ไม่ได้ตั้ง feed.path ใน watchlist.json")

        if path.startswith("http://") or path.startswith("https://"):
            try:
                resp = self.session.get(path, timeout=TIMEOUT * 3)
                resp.raise_for_status()
            except requests.RequestException as e:
                raise PriceUnavailable(f"โหลด feed ไม่ได้: {e}") from e
            return resp.text

        f = Path(path)
        if not f.exists():
            raise PriceUnavailable(f"ไม่พบไฟล์ feed: {f}")
        return f.read_text(encoding="utf-8")

    def _parse(self, raw: str) -> list[dict]:
        if self.cfg.format == "json":
            data = json.loads(raw)
            if isinstance(data, dict):
                # feed หลายเจ้าห่อ list ไว้ใน key เดียว หาอันแรกที่เป็น list
                for value in data.values():
                    if isinstance(value, list):
                        return value
                return []
            return data
        return list(csv.DictReader(io.StringIO(raw)))

    def load(self) -> dict[str, dict]:
        if self._index is not None:
            return self._index

        rows = self._parse(self._read_raw())
        key = self.cfg.key_field
        index = {}
        for row in rows:
            k = str(row.get(key, "")).strip()
            if k:
                index[k] = row

        print(f"[feed] โหลด {len(index)} แถวจาก {self.cfg.path}")
        self._index = index
        return index

    # --- ดึงราคา --------------------------------------------------------

    @staticmethod
    def _to_float(value) -> float | None:
        if value is None:
            return None
        text = str(value).replace(",", "").replace("฿", "").strip()
        # feed บางเจ้าใส่สกุลเงินมาด้วย เช่น "THB 1290.00"
        m = re.search(r"[0-9]+(?:\.[0-9]+)?", text)
        return float(m.group()) if m else None

    def fetch(self, product: Product) -> PricePoint:
        row = self.load().get(product.lookup_key)
        if row is None:
            raise PriceUnavailable(f"ไม่พบ {product.lookup_key} ใน feed")

        price = self._to_float(self.cfg.get(row, "price"))
        if price is None or price <= 0:
            raise PriceUnavailable(f"ราคาใน feed อ่านไม่ออก: {self.cfg.get(row, 'price')!r}")

        availability = str(self.cfg.get(row, "availability", "")).strip().lower()
        out_of_stock = availability in {"0", "false", "no", "outofstock", "out of stock", "sold out"}

        return PricePoint(
            ts=datetime.now(timezone.utc),
            price=price,
            in_stock=not out_of_stock,
        )


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


def build_sources(feed_cfg: FeedConfig) -> dict:
    """
    สร้าง source ทุกตัวครั้งเดียวต่อรอบ แล้วให้ main เลือกใช้ตาม product.source
    สำคัญกับ FeedSource เพราะมันโหลดไฟล์ทั้งก้อน ไม่ควรสร้างใหม่ต่อสินค้าหนึ่งชิ้น
    """
    dry_run = os.environ.get("DEAL_DRY_RUN") == "1"
    mock = MockSource()
    if dry_run:
        return {"feed": mock, "http": mock, "mock": mock}
    return {"feed": FeedSource(feed_cfg), "http": HttpSource(), "mock": mock}
