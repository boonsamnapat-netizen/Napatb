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
    source: str = "feed"        # feed | http | mock
    feed_id: str = ""           # คีย์ที่ใช้หาแถวใน datafeed (ถ้า source=feed)
    price_regex: str = ""       # fallback ถ้าเว็บไม่มี JSON-LD (ถ้า source=http)
    currency: str = "THB"
    enabled: bool = True

    @property
    def lookup_key(self) -> str:
        return self.feed_id or self.id

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
    window_days: int = 90            # หน้าต่างยาว: ใช้หา percentile + เส้นแนวโน้ม
    ref_window_days: int = 21        # หน้าต่างสั้น: ใช้คำนวณราคาอ้างอิง
    min_history_days: int = 14       # ต้องมีข้อมูลอย่างน้อยกี่วันถึงจะกล้าตัดสิน
    min_observations: int = 10       # จำนวนจุดราคาขั้นต่ำในหน้าต่าง
    exclude_recent_hours: int = 24   # ไม่เอาราคา 24 ชม.ล่าสุดมาคิดเป็นฐาน

    # --- กันการเข้าใจผิดว่า "ของเสื่อมราคา" คือ "ดีล" (สำคัญมากกับ electronics) ---
    # สินค้าอิเล็กทรอนิกส์ราคาลดลงเรื่อย ๆ ตามอายุรุ่นอยู่แล้ว ถ้าเทียบกับ median
    # 90 วันเฉย ๆ จะเห็นเป็น "ส่วนลด" ตลอดเวลาทั้งที่ไม่มีอะไรเกิดขึ้น
    # เราจึงลากเส้นแนวโน้มการเสื่อมราคาของสินค้าชิ้นนั้นเอง แล้วถามว่า
    # "วันนี้ถูกกว่าที่ควรจะเป็นตามแนวโน้มไหม" ไม่ใช่ "ถูกกว่าเมื่อก่อนไหม"
    min_off_trend_pct: float = 8.0   # ต้องต่ำกว่าเส้นแนวโน้มอย่างน้อยกี่ %

    # --- กันเล่ห์ "ขึ้นราคาแล้วลด" ---
    inflate_lookback_days: int = 7   # นับ 7 วันล่าสุดเป็น "ช่วงที่อาจโดนปั่น"
    inflate_tol_pct: float = 5.0     # ถ้า median 7 วันสูงกว่าของเก่าเกิน 5% = สงสัย

    # --- เกณฑ์ผ่าน ---
    min_discount_pct: float = 12.0   # ลดจากราคาอ้างอิงอย่างน้อยกี่ %
    max_percentile: float = 10.0     # ราคาต้องอยู่ใน 10% ล่างสุดของประวัติ
    min_saving: float = 150.0        # ประหยัดขั้นต่ำ (บาท) — กัน 20% ของ 60 บาท
    # วัดเป็น "บาทต่อออเดอร์" ไม่ใช่ % — สำหรับ electronics ค่าคอมมี %ต่ำ (1-3%)
    # แต่ราคาต่อชิ้นสูง 2% ของจอ 15,000 = 300 บาท ซึ่งดีกว่า 12% ของครีม 800 = 96 บาท
    # เกณฑ์ที่ถูกต้องจึงเป็นเงินต่อออเดอร์ ไม่ใช่อัตรา
    min_commission: float = 100.0    # ค่าคอมคาดหวังขั้นต่ำต่อออเดอร์ (บาท)

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
            window_days=i("DEAL_WINDOW_DAYS", 90),
            ref_window_days=i("DEAL_REF_WINDOW_DAYS", 21),
            min_history_days=i("DEAL_MIN_HISTORY_DAYS", 14),
            min_observations=i("DEAL_MIN_OBS", 10),
            exclude_recent_hours=i("DEAL_EXCLUDE_RECENT_HOURS", 24),
            inflate_lookback_days=i("DEAL_INFLATE_LOOKBACK_DAYS", 7),
            inflate_tol_pct=f("DEAL_INFLATE_TOL_PCT", 5.0),
            min_off_trend_pct=f("DEAL_MIN_OFF_TREND_PCT", 8.0),
            min_discount_pct=f("DEAL_MIN_DISCOUNT_PCT", 12.0),
            max_percentile=f("DEAL_MAX_PERCENTILE", 10.0),
            min_saving=f("DEAL_MIN_SAVING", 150.0),
            min_commission=f("DEAL_MIN_COMMISSION", 100.0),
            cooldown_days=i("DEAL_COOLDOWN_DAYS", 14),
            re_alert_improve_pct=f("DEAL_RE_ALERT_IMPROVE_PCT", 5.0),
            max_alerts_per_run=i("DEAL_MAX_ALERTS", 3),
        )


@dataclass
class FeedConfig:
    """
    ตั้งค่าการอ่าน datafeed ของ advertiser

    Involve Asia (และ network อื่นแทบทุกเจ้า) ให้ datafeed เป็นไฟล์ที่มี
    ชื่อสินค้า / ราคา / URL / รูป / คำอธิบาย — ซึ่งเป็นทางการและไม่ต้อง scrape

    ชื่อคอลัมน์ต่างกันไปตาม advertiser เลยทำเป็น mapping แทนที่จะ hardcode
    ถ้า Lazada เปลี่ยนชื่อคอลัมน์ แก้ที่ watchlist.json ไฟล์เดียวจบ
    """
    enabled: bool = False
    path: str = ""
    format: str = "csv"          # csv | json
    key_field: str = "sku"       # คอลัมน์ที่ใช้เป็นคีย์สินค้า
    fields: dict = field(default_factory=dict)

    def get(self, row: dict, logical: str, default=None):
        """อ่านค่าจากแถวโดยใช้ mapping — logical คือชื่อที่โค้ดเราใช้"""
        column = self.fields.get(logical, logical)
        return row.get(column, default)


def load_feed_config(path: Path = WATCHLIST_PATH) -> FeedConfig:
    if not path.exists():
        return FeedConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return FeedConfig(**raw.get("feed", {}))


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
