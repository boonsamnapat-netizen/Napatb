# DC55v35 Health Check

อัปเดต: 2026-08-05 08:32 UTC

## ผลรวม: HEALTHY

| รายการตรวจ | ผล |
|---|---|
| OKX API / Data download | OK |
| Strategy load (DC55v35) | OK |
| Signal generation (60 วัน, 10 pairs) | 69 trades |
| Backtest profit | -9.84% |

## วิเคราะห์
Strategy ทำงานปกติ — generate 69 trades ใน 60 วัน

## แนวทาง
Dry-run live อาจยังไม่เจอ breakout — รอ cron รอบถัดไป

## หมายเหตุ
- ตรวจสอบ 10 pairs: BTC ETH SOL AXS LDO APE GRT TRX MANA SAND
- ช่วงเวลา: 60 วันล่าสุด (timerange 20260805)
- หาก HEALTHY แต่ live dry-run ไม่มี trade = ปกติ (bot รัน 25 นาที/ครั้ง รอ breakout candle 4H)
- หาก NO SIGNAL นาน >14 วัน = ควรทบทวน ADX/Vol threshold
