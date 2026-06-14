# DC55v24 Health Check

**อัปเดต:** 2026-06-14 17:28 UTC

## ผลรวม: ❌ ERROR

| รายการตรวจ | ผล |
|---|---|
| OKX API / Data download | ❌ FAIL |
| Strategy load (DC55v24) | ❌ FAIL |
| Signal generation (60 วัน, 10 pairs) | 0 trades |

## วิเคราะห์
Strategy หรือ data pipeline มีปัญหา — ดู log ด้านล่าง

## แนวทาง
ต้องตรวจสอบ error ใน backtest output

## หมายเหตุ
- ตรวจสอบ 10 pairs: BTC ETH SOL AXS LDO APE GRT TRX MANA SAND
- ช่วงเวลา: 60 วันล่าสุด (timerange 20260614)
- หาก HEALTHY แต่ live dry-run ไม่มี trade = ปกติ (bot รัน 25 นาที/ครั้ง รอ breakout candle 4H)
- หาก NO SIGNAL นาน >14 วัน = ควรทบทวน ADX/Vol threshold
