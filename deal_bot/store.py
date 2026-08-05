"""
SQLite price history.

ไฟล์ .sqlite ถูก commit กลับ repo ทุกรอบ (pattern เดียวกับ dryrun/tradesv3.sqlite)
ประวัติราคานี้คือทรัพย์สินตัวจริงของโปรเจกต์ — ไม่ใช่โค้ด
ยิ่งเก็บนาน ยิ่งตัดสินดีลได้แม่น และเป็นข้อมูลที่ Google/AI ไม่มี
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deal_bot.config import DB_PATH


@dataclass
class PricePoint:
    ts: datetime
    price: float
    in_stock: bool = True


@dataclass
class AlertRecord:
    product_id: str
    ts: datetime
    price: float
    tag: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    product_id  TEXT    NOT NULL,
    ts          TEXT    NOT NULL,
    price       REAL    NOT NULL,
    in_stock    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (product_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_prices_product ON prices(product_id, ts);

CREATE TABLE IF NOT EXISTS alerts (
    tag          TEXT PRIMARY KEY,
    product_id   TEXT NOT NULL,
    ts           TEXT NOT NULL,
    price        REAL NOT NULL,
    ref_price    REAL NOT NULL,
    discount_pct REAL NOT NULL,
    score        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_product ON alerts(product_id, ts);

CREATE TABLE IF NOT EXISTS fetch_log (
    product_id TEXT NOT NULL,
    ts         TEXT NOT NULL,
    ok         INTEGER NOT NULL,
    detail     TEXT
);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


class PriceStore:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- writes ----------------------------------------------------------

    def record_price(self, product_id: str, point: PricePoint) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO prices (product_id, ts, price, in_stock) VALUES (?,?,?,?)",
            (product_id, _iso(point.ts), point.price, int(point.in_stock)),
        )
        self.conn.commit()

    def record_alert(self, tag: str, product_id: str, ts: datetime, price: float,
                     ref_price: float, discount_pct: float, score: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO alerts "
            "(tag, product_id, ts, price, ref_price, discount_pct, score) VALUES (?,?,?,?,?,?,?)",
            (tag, product_id, _iso(ts), price, ref_price, discount_pct, score),
        )
        self.conn.commit()

    def log_fetch(self, product_id: str, ts: datetime, ok: bool, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO fetch_log (product_id, ts, ok, detail) VALUES (?,?,?,?)",
            (product_id, _iso(ts), int(ok), detail[:500]),
        )
        self.conn.commit()

    # --- reads -----------------------------------------------------------

    def history(self, product_id: str, days: int) -> list[PricePoint]:
        """ประวัติราคาย้อนหลัง N วัน เรียงจากเก่าไปใหม่"""
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=days))
        rows = self.conn.execute(
            "SELECT ts, price, in_stock FROM prices "
            "WHERE product_id = ? AND ts >= ? ORDER BY ts ASC",
            (product_id, cutoff),
        ).fetchall()
        return [PricePoint(_parse(ts), price, bool(stock)) for ts, price, stock in rows]

    def last_alert(self, product_id: str) -> AlertRecord | None:
        row = self.conn.execute(
            "SELECT tag, ts, price FROM alerts WHERE product_id = ? ORDER BY ts DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        if not row:
            return None
        tag, ts, price = row
        return AlertRecord(product_id=product_id, ts=_parse(ts), price=price, tag=tag)

    def coverage(self) -> list[tuple[str, int, str, str]]:
        """(product_id, จำนวนจุดราคา, วันแรก, วันล่าสุด) — ใช้เขียนรายงาน"""
        return self.conn.execute(
            "SELECT product_id, COUNT(*), MIN(ts), MAX(ts) FROM prices GROUP BY product_id"
        ).fetchall()

    def recent_alerts(self, limit: int = 20) -> list[tuple]:
        return self.conn.execute(
            "SELECT ts, product_id, price, ref_price, discount_pct, tag "
            "FROM alerts ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def close(self) -> None:
        self.conn.close()
