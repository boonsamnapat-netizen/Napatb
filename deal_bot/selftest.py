#!/usr/bin/env python3
"""
Self-test ของ detector — รันได้โดยไม่ต้องมีเน็ต ไม่ต้องมี API key

  python -m deal_bot.selftest

ทำไมต้องมี: ตรรกะจับดีลคือส่วนเดียวที่ผิดแล้วเสียหายจริง (ยิงดีลปลอมใส่คนอ่าน
= เสียความเชื่อถือ ซึ่งเรียกกลับไม่ได้) เคสสำคัญคือ "ขึ้นราคาแล้วลด" ที่หน้าเว็บ
มองเป็นส่วนลด 30% แต่จริง ๆ ราคาเท่าเดิม — เทสต์ตัวที่ 3 คุมเคสนั้นไว้
"""

import sys
from datetime import datetime, timedelta, timezone

from deal_bot.config import Product, Thresholds
from deal_bot.detector import Deal, Skip, evaluate
from deal_bot.store import AlertRecord, PricePoint

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)

PRODUCT = Product(
    id="test-item",
    name="สินค้าทดสอบ",
    url="https://example.com/p/1",
    category="test",
    commission_pct=10.0,
)


def series(prices: list[float], start_days_ago: int = 60) -> list[PricePoint]:
    """แปลง list ราคาเป็นจุดราคารายวัน จบที่ NOW"""
    step = start_days_ago / max(len(prices) - 1, 1)
    return [
        PricePoint(ts=NOW - timedelta(days=start_days_ago - i * step), price=p)
        for i, p in enumerate(prices)
    ]


CASES = []


def case(name):
    def wrap(fn):
        CASES.append((name, fn))
        return fn
    return wrap


@case("ราคานิ่งมานาน แล้วลดจริง 25% → ต้องเจอดีล")
def test_real_deal():
    prices = [1000.0] * 40 + [750.0]
    result = evaluate(PRODUCT, series(prices), Thresholds())
    assert isinstance(result, Deal), f"คาดว่าเจอดีล แต่ได้ {result}"
    assert 24 < result.discount_pct < 26, result.discount_pct
    assert not result.inflate_guard
    return f"ลด {result.discount_pct:.0f}%, score {result.score:.0f}"


@case("ลดแค่ 8% → ตื้นเกินเกณฑ์ ต้องไม่ยิง")
def test_shallow():
    prices = [1000.0] * 40 + [920.0]
    result = evaluate(PRODUCT, series(prices), Thresholds())
    assert isinstance(result, Skip) and result.reason == "shallow_discount", result
    return result.detail


@case("ขึ้นราคา 7 วันก่อนแล้วลดกลับที่เดิม → ต้องจับได้ว่าไม่ใช่ดีล")
def test_fake_markdown():
    # ราคาปกติ 1000 มาตลอด แล้ว 7 วันล่าสุดดันขึ้นเป็น 1400
    # จากนั้น "ลด" เหลือ 1050 — หน้าเว็บจะโชว์ -25% แต่จริง ๆ แพงกว่าเดิมด้วยซ้ำ
    prices = [1000.0] * 35 + [1400.0] * 7 + [1050.0]
    result = evaluate(PRODUCT, series(prices), Thresholds())
    assert isinstance(result, Skip), f"ดีลปลอมหลุดผ่าน: {result}"
    return f"ตัดทิ้งด้วยเหตุผล {result.reason}"


@case("ขึ้นราคาก่อน แต่ลดลึกจนถูกกว่าราคาเก่าจริง → ยิงได้ แต่ต้องติดธง guard")
def test_inflate_but_genuine():
    prices = [1000.0] * 35 + [1400.0] * 7 + [700.0]
    result = evaluate(PRODUCT, series(prices), Thresholds())
    assert isinstance(result, Deal), result
    assert result.inflate_guard, "ควรติดธงว่าตรวจพบการดันราคา"
    # ส่วนลดต้องคิดจากฐาน 1000 ไม่ใช่ 1400
    assert 29 < result.discount_pct < 31, result.discount_pct
    return f"ลด {result.discount_pct:.0f}% (คิดจากฐานเก่า ไม่ใช่ราคาที่ถูกดัน)"


@case("ข้อมูลแค่ 5 วัน → ต้องบอกว่ายังอุ่นเครื่องอยู่")
def test_warming_up():
    prices = [1000.0] * 5 + [700.0]
    result = evaluate(PRODUCT, series(prices, start_days_ago=5), Thresholds())
    assert isinstance(result, Skip) and result.reason == "warming_up", result
    return result.detail


@case("ค่าคอม 2% ของสินค้าถูก → ไม่คุ้มพื้นที่ในช่อง")
def test_low_commission():
    cheap = Product(**{**PRODUCT.__dict__, "commission_pct": 2.0})
    prices = [900.0] * 40 + [650.0]
    result = evaluate(cheap, series(prices), Thresholds())
    assert isinstance(result, Skip) and result.reason == "low_commission", result
    return result.detail


@case("เพิ่งยิงไป 3 วันก่อน ราคาลดอีกนิดเดียว → ต้องไม่ยิงซ้ำ")
def test_cooldown():
    prices = [1000.0] * 40 + [740.0]
    last = AlertRecord(product_id=PRODUCT.id, ts=NOW - timedelta(days=3), price=750.0, tag="x")
    result = evaluate(PRODUCT, series(prices), Thresholds(), last_alert=last)
    assert isinstance(result, Skip) and result.reason == "cooldown", result
    return result.detail


@case("ราคาแกว่งขึ้นลงเป็นประจำ ตอนนี้อยู่ช่วงล่างปกติ → ไม่ใช่ดีลพิเศษ")
def test_normal_oscillation():
    prices = ([1000.0, 800.0] * 20) + [800.0]
    result = evaluate(PRODUCT, series(prices), Thresholds())
    assert isinstance(result, Skip), f"ราคาปกติไม่ควรถูกนับเป็นดีล: {result}"
    return f"ตัดทิ้งด้วยเหตุผล {result.reason}"


def main() -> int:
    print("=" * 70)
    print("deal_bot detector self-test")
    print("=" * 70)

    failed = 0
    for name, fn in CASES:
        try:
            detail = fn()
            print(f"✅ {name}\n     → {detail}")
        except AssertionError as e:
            failed += 1
            print(f"❌ {name}\n     → {e}")
        except Exception as e:
            failed += 1
            print(f"💥 {name}\n     → {type(e).__name__}: {e}")

    print("=" * 70)
    print(f"{len(CASES) - failed}/{len(CASES)} ผ่าน")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
