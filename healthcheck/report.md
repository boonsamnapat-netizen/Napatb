# DC55v24 Health Check

อัปเดต: 2026-06-14 23:16 UTC

## ผลรวม: NO SIGNAL

| รายการตรวจ | ผล |
|---|---|
| OKX API / Data download | OK |
| Strategy load (DC55v24) | OK |
| Signal generation (60 วัน, 10 pairs) | 0 trades |

## วิเคราะห์
Strategy ทำงานได้ แต่ไม่มี signal ใน 60 วันล่าสุด (อาจเป็นสภาพตลาด)

## แนวทาง
ถ้าเกิน 14 วัน ควรทบทวน ADX/Vol threshold

## หมายเหตุ
- ตรวจสอบ 10 pairs: BTC ETH SOL AXS LDO APE GRT TRX MANA SAND
- ช่วงเวลา: 60 วันล่าสุด (timerange 20260614)
- หาก HEALTHY แต่ live dry-run ไม่มี trade = ปกติ (bot รัน 25 นาที/ครั้ง รอ breakout candle 4H)
- หาก NO SIGNAL นาน >14 วัน = ควรทบทวน ADX/Vol threshold
