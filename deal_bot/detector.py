"""
Deal detection — หัวใจของบอทตัวนี้

ปัญหาที่แก้: ร้านค้าออนไลน์ "ลดราคา" ตลอดเวลา ราคาขีดฆ่าบนหน้าเว็บเชื่อไม่ได้
เล่ห์ที่เจอบ่อยคือขึ้นราคา 2 สัปดาห์ก่อนแคมเปญ แล้วลดกลับมาที่ราคาเดิม
แล้วติดป้าย "-40%"

วิธีแก้: ไม่อ่านราคาขีดฆ่าเลย ใช้ประวัติราคาที่เราเก็บเองเป็นฐานอย่างเดียว
และเทียบด้วย median (ไม่ใช่ max) เพราะ median ไม่ขยับตามราคาที่ปั่นระยะสั้น

ทุกฟังก์ชันในไฟล์นี้เป็น pure function — รับ list ราคาเข้าไป คืนผลลัพธ์ออกมา
ไม่แตะ network ไม่แตะ DB เพื่อให้ทดสอบย้อนหลังได้เหมือน backtest.py
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from deal_bot.config import Product, Thresholds
from deal_bot.store import AlertRecord, PricePoint


@dataclass
class Deal:
    product: Product
    price: float
    ref_price: float          # ราคาอ้างอิงที่เราคำนวณเอง (ไม่ใช่ราคาป้าย)
    discount_pct: float
    percentile: float         # ราคานี้อยู่เปอร์เซ็นไทล์ที่เท่าไรของประวัติ
    low_days: int             # ถูกที่สุดในรอบกี่วัน
    expected_commission: float
    score: float
    inflate_guard: bool       # True = ตรวจพบร่องรอยขึ้นราคาก่อนลด
    history_days: int

    @property
    def saving(self) -> float:
        return self.ref_price - self.price


@dataclass
class Skip:
    """เหตุผลที่ไม่ผ่าน — เก็บไว้เขียนรายงาน จะได้รู้ว่าเกณฑ์ตึงไปไหม"""
    product_id: str
    reason: str
    detail: str = ""


def percentile_rank(value: float, values: list[float]) -> float:
    """ราคานี้ต่ำกว่ากี่ % ของราคาทั้งหมดในหน้าต่าง (0 = ถูกที่สุดเท่าที่เคยเห็น)"""
    if not values:
        return 100.0
    below_or_equal = sum(1 for v in values if v <= value)
    return below_or_equal / len(values) * 100.0


def days_since_this_cheap(history: list[PricePoint], price: float, now: datetime) -> int:
    """
    ครั้งสุดท้ายที่ราคาถูกเท่านี้ (หรือถูกกว่า) คือกี่วันก่อน
    ถ้าไม่เคยเลยในหน้าต่าง = ถูกที่สุดตลอดช่วงที่เก็บมา
    """
    for point in reversed(history[:-1]):
        if point.price <= price:
            return max(0, (now - point.ts).days)
    span = (now - history[0].ts).days if history else 0
    return span


def reference_price(history: list[PricePoint], now: datetime, cfg: Thresholds
                    ) -> tuple[float, bool]:
    """
    ราคาอ้างอิง = median ของราคาย้อนหลัง (ตัด 24 ชม.ล่าสุดออก เพราะนั่นคือราคาที่กำลังตัดสิน)

    ถ้า median ของ 7 วันล่าสุดสูงกว่า median ของช่วงเก่ากว่าเกิน inflate_tol_pct
    แปลว่ามีการดันราคาขึ้นก่อนหน้านี้ → ใช้ median ช่วงเก่าเป็นฐานแทน (ฐานต่ำกว่า
    = ส่วนลดที่คำนวณได้น้อยลง = ผ่านเกณฑ์ยากขึ้น) นี่คือจุดที่ตัดดีลปลอมทิ้ง
    """
    cutoff_recent = now - timedelta(hours=cfg.exclude_recent_hours)
    prior = [p.price for p in history if p.ts < cutoff_recent]
    if not prior:
        return 0.0, False

    ref = median(prior)

    inflate_cut = now - timedelta(days=cfg.inflate_lookback_days)
    recent_block = [p.price for p in history if inflate_cut <= p.ts < cutoff_recent]
    older_block = [p.price for p in history if p.ts < inflate_cut]

    guard = False
    if recent_block and older_block:
        recent_med = median(recent_block)
        older_med = median(older_block)
        if older_med > 0 and (recent_med - older_med) / older_med > cfg.inflate_tol_pct / 100:
            ref = min(ref, older_med)
            guard = True

    return ref, guard


def score_deal(discount_pct: float, percentile: float, expected_commission: float,
               cfg: Thresholds) -> float:
    """
    คะแนนไว้จัดอันดับตอนมีหลายดีลพร้อมกัน (ไม่ใช่เกณฑ์ผ่าน/ไม่ผ่าน)

      50% ส่วนลดลึกแค่ไหน
      30% หายากแค่ไหน (ราคานี้แทบไม่เคยเห็น)
      20% จ่ายเราเท่าไร — ดีลดีที่ค่าคอม 2% ไม่คุ้มพื้นที่ในช่อง
    """
    depth = min(discount_pct, 60.0) / 60.0 * 100
    rarity = 100.0 - percentile
    payout = min(expected_commission / max(cfg.min_commission, 1.0), 5.0) / 5.0 * 100
    return 0.5 * depth + 0.3 * rarity + 0.2 * payout


def evaluate(product: Product, history: list[PricePoint], cfg: Thresholds,
             last_alert: AlertRecord | None = None) -> Deal | Skip:
    """
    ตัดสินสินค้าหนึ่งชิ้น — คืน Deal ถ้าผ่านทุกด่าน ไม่งั้นคืน Skip พร้อมเหตุผล

    ด่านทั้งหมดต้องผ่านหมด (AND ไม่ใช่ OR) เหมือน trend filter ใน alert_bot
    """
    if not history:
        return Skip(product.id, "no_data")

    latest = history[-1]
    now, price = latest.ts, latest.price

    if price <= 0:
        return Skip(product.id, "bad_price", f"{price}")
    if not latest.in_stock:
        return Skip(product.id, "out_of_stock")

    # --- ด่าน 0: ข้อมูลพอหรือยัง ---------------------------------------
    span_days = (history[-1].ts - history[0].ts).days
    if len(history) < cfg.min_observations or span_days < cfg.min_history_days:
        return Skip(product.id, "warming_up",
                    f"{len(history)} จุด / {span_days} วัน "
                    f"(ต้องการ {cfg.min_observations} จุด / {cfg.min_history_days} วัน)")

    # --- ด่าน 1: ราคาอ้างอิงจากประวัติของเราเอง -------------------------
    ref, guard = reference_price(history, now, cfg)
    if ref <= 0:
        return Skip(product.id, "no_reference")

    discount_pct = (ref - price) / ref * 100
    if discount_pct < cfg.min_discount_pct:
        return Skip(product.id, "shallow_discount", f"{discount_pct:.1f}%")

    # --- ด่าน 2: ต่ำจริงเมื่อเทียบกับตัวเอง ------------------------------
    pct = percentile_rank(price, [p.price for p in history])
    if pct > cfg.max_percentile:
        return Skip(product.id, "not_a_low", f"percentile {pct:.0f}")

    # --- ด่าน 3: ประหยัดเป็นเงินจริง ------------------------------------
    saving = ref - price
    if saving < cfg.min_saving:
        return Skip(product.id, "small_saving", f"{saving:.0f} บาท")

    # --- ด่าน 4: จ่ายเราคุ้มพื้นที่ในช่องไหม ------------------------------
    expected_commission = price * product.commission_pct / 100
    if expected_commission < cfg.min_commission:
        return Skip(product.id, "low_commission", f"{expected_commission:.0f} บาท")

    # --- ด่าน 5: เคยยิงไปแล้วหรือยัง (กันสแปม) ---------------------------
    if last_alert:
        age_days = (now - last_alert.ts).days
        improve = (last_alert.price - price) / last_alert.price * 100 if last_alert.price else 0
        if age_days < cfg.cooldown_days and improve < cfg.re_alert_improve_pct:
            return Skip(product.id, "cooldown",
                        f"ยิงไปเมื่อ {age_days} วันก่อน, ถูกลงอีกแค่ {improve:.1f}%")

    return Deal(
        product=product,
        price=price,
        ref_price=ref,
        discount_pct=discount_pct,
        percentile=pct,
        low_days=days_since_this_cheap(history, price, now),
        expected_commission=expected_commission,
        score=score_deal(discount_pct, pct, expected_commission, cfg),
        inflate_guard=guard,
        history_days=span_days,
    )
