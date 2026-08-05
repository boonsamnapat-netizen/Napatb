"""
Configuration for the deal bot.

ทุกค่าปรับผ่าน env var ได้ (เหมือน alert_bot) เพื่อให้ tune ผ่าน workflow ได้
โดยไม่ต้องแก้โค้ด — ค่า default ตั้งไว้แบบ "เข้มไว้ก่อน" คือยอมพลาดดีลดีกว่า
ยิงดีลปลอมใส่ผู้ติดตาม เพราะช่องทาง Telegram พังจากการสแปมเร็วกว่าพังจากเงียบ
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

WATCHLIST_PATH = Path(os.environ.get("DEAL_WATCHLIST", Path(__file__).parent / "watchlist.json"))
DB_PATH = Path(os.environ.get("DEAL_DB", Path(__file__).parent / "prices.sqlite"))


@dataclass
class Product:
    """สินค้าหนึ่งชิ้นที่เราเฝ้าราคา"""
    id: str
    name: str
    url: str
    category: str
    commission_pct: float       # ค่าคอมที่เราได้จริงจาก advertiser (%)
    source: str = "http"        # http | mock
    price_regex: str = ""       # fallback ถ้าเว็บไม่มี JSON-LD
    currency: str = "THB"
    enabled: bool = True

    @property
    def merchant(self) -> str:
        from urllib.parse import urlparse
        return urlparse(self.url).netloc.replace("www.", "") or "unknown"


@dataclass
class Thresholds:
    """
    เกณฑ์ตัดสินว่า "นี่คือดีลจริง"

    หลักคิด: เราไม่เชื่อราคาป้าย/ราคาขีดฆ่าของร้าน เพราะปั่นได้
    เราเชื่อเฉพาะ **ประวัติราคาที่เราเก็บเอง** เท่านั้น
    """
    # --- หน้าต่างข้อมูล ---
    window_days: int = 60            # ช่วงที่ใช้คำนวณราคาอ้างอิง
    min_history_days: int = 14       # ต้องมีข้อมูลอย่างน้อยกี่วันถึงจะกล้าตัดสิน
    min_observations: int = 10       # จำนวนจุดราคาขั้นต่ำในหน้าต่าง
    exclude_recent_hours: int = 24   # ไม่เอาราคา 24 ชม.ล่าสุดมาคิดเป็นฐาน

    # --- กันเล่ห์ "ขึ้นราคาแล้วลด" ---
    inflate_lookback_days: int = 7   # นับ 7 วันล่าสุดเป็น "ช่วงที่อาจโดนปั่น"
    inflate_tol_pct: float = 5.0     # ถ้า median 7 วันสูงกว่าของเก่าเกิน 5% = สงสัย

    # --- เกณฑ์ผ่าน ---
    min_discount_pct: float = 15.0   # ลดจากราคาอ้างอิงอย่างน้อยกี่ %
    max_percentile: float = 10.0     # ราคาต้องอยู่ใน 10% ล่างสุดของประวัติ
    min_saving: float = 150.0        # ประหยัดขั้นต่ำ (บาท) — กัน 20% ของ 60 บาท
    min_commission: float = 20.0     # ค่าคอมคาดหวังขั้นต่ำต่อออเดอร์ (บาท)

    # --- กันสแปมช่อง ---
    cooldown_days: int = 14          # ไม่ยิงสินค้าเดิมซ้ำภายในกี่วัน
    re_alert_improve_pct: float = 5.0  # เว้นแต่ราคาลดลงอีกอย่างน้อยกี่ %
    max_alerts_per_run: int = 3      # ยิงมากสุดกี่ชิ้นต่อรอบ

    @classmethod
    def from_env(cls) -> "Thresholds":
        def f(name, default):
            return float(os.environ.get(name, default))

        def i(name, default):
            return int(os.environ.get(name, default))

        return cls(
            window_days=i("DEAL_WINDOW_DAYS", 60),
            min_history_days=i("DEAL_MIN_HISTORY_DAYS", 14),
            min_observations=i("DEAL_MIN_OBS", 10),
            exclude_recent_hours=i("DEAL_EXCLUDE_RECENT_HOURS", 24),
            inflate_lookback_days=i("DEAL_INFLATE_LOOKBACK_DAYS", 7),
            inflate_tol_pct=f("DEAL_INFLATE_TOL_PCT", 5.0),
            min_discount_pct=f("DEAL_MIN_DISCOUNT_PCT", 15.0),
            max_percentile=f("DEAL_MAX_PERCENTILE", 10.0),
            min_saving=f("DEAL_MIN_SAVING", 150.0),
            min_commission=f("DEAL_MIN_COMMISSION", 20.0),
            cooldown_days=i("DEAL_COOLDOWN_DAYS", 14),
            re_alert_improve_pct=f("DEAL_RE_ALERT_IMPROVE_PCT", 5.0),
            max_alerts_per_run=i("DEAL_MAX_ALERTS", 3),
        )


def load_watchlist(path: Path = WATCHLIST_PATH) -> list[Product]:
    """อ่าน watchlist.json — ข้ามรายการที่ enabled=false"""
    if not path.exists():
        print(f"[config] watchlist not found: {path}")
        return []

    raw = json.loads(path.read_text(encoding="utf-8"))
    products = []
    for item in raw.get("products", []):
        p = Product(**item)
        if p.enabled:
            products.append(p)
    print(f"[config] Loaded {len(products)} enabled product(s) from watchlist")
    return products
