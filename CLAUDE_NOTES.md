# Claude Session Notes

บันทึก context การคุยกับ Claude Code เพื่อให้ข้อมูลไม่หายระหว่าง session

---

## ข้อมูลผู้ใช้

- Email: boonsamnapat@gmail.com
- Repository: boonsamnapat-netizen/napatb
- Branch หลักที่ทำงาน: `claude/claude-code-cloud-github-hokzc8`

---

## บริบทที่คุยกัน

### 2026-06-08 — Session เริ่มต้น

- ผู้ใช้ถามว่ารัน Claude Code บน cloud GitHub หรือเปล่า → ยืนยันว่าใช่ เป็น Remote Execution Environment
- ผู้ใช้ขอคำอธิบายข้อจำกัดแบบง่ายๆ → อธิบายว่าเหมือนคอมเช่าชั่วคราว ต้อง commit + push ทุกครั้ง
- ผู้ใช้ขอให้เก็บข้อมูลการคุยไว้ → สร้างไฟล์นี้ขึ้นมา

### 2026-08-05 — สำรวจตลาด affiliate

- ผู้ใช้ขอให้สำรวจตลาด affiliate ว่า automate จุดไหนได้ และจุดไหนคุ้มสำหรับเรา
- ผลการสำรวจอยู่ใน `AFFILIATE_MARKET_RESEARCH.md` (branch `claude/affiliate-market-automation-y043jn`)
- ข้อสรุป: จุดที่คนแห่ไป automate (ผลิตคอนเทนต์ / โพสต์อัตโนมัติ) ผลตอบแทนเป็นศูนย์แล้ว
  จุดที่ยังว่างคืองานข้อมูล — จับดีล/ประวัติราคา, เทียบเรตค่าคอม, วัด EPC, ตรวจลิงก์ตาย
- ที่แนะนำ: deal engine + Telegram (reuse โครง `alert_bot` ได้ ~70%) คู่กับระบบวัด EPC
  (มองว่าเป็น `backtest.py` เวอร์ชัน affiliate) — ยังไม่ได้เริ่มเขียนโค้ด

---

## หมายเหตุ

- ไฟล์นี้จะอัปเดตทุกครั้งที่มีข้อมูลสำคัญจากการคุย
- เมื่อเริ่ม session ใหม่ให้ Claude อ่านไฟล์นี้ก่อนเสมอ

### 2026-08-10 — เลือกหมวด electronics + อัปเกรด detector

- ผู้ใช้เลือกหมวด **Electronics (ไอที)** สำหรับ deal_bot
- เจอ 2 เรื่องที่ทำให้ต้องแก้สมมติฐานเดิมในรายงานสำรวจตลาด:
  1. กติกา "ค่าคอม >= 8%" ใช้กับ electronics ไม่ได้ (ทั้งหมวดจ่าย 1-3%)
     → เปลี่ยนเกณฑ์เป็น **บาทต่อออเดอร์** (min_commission 20 → 100 บาท)
     → หมวดนี้คุ้มเฉพาะของราคาสูงเกิน ~5,000 บาท
  2. ของไอทีราคาไหลลงเองเดือนละ 2-5% ตามอายุรุ่น ถ้าเทียบ median เฉย ๆ
     บอทจะเห็นเป็น "ส่วนลด" ตลอดเวลา → เพิ่มด่านเส้นแนวโน้ม
     (Theil-Sen fit ใน log space) ถามว่า "ถูกกว่าที่ควรจะเป็นวันนี้ไหม"
- selftest 8 → 13 เคส (เพิ่มเคส electronics 5 เคส) ผ่านหมด
- watchlist ใส่ URL จริงจาก advice.co.th 8 รายการ (จอ 27" 5 + SSD 1TB 3)
- **ยังไม่ได้ verify ว่าดึงราคาได้จริง** — sandbox โดน network policy บล็อกเว็บไทย
  ต้องกด workflow "Deal Bot — Probe URLs" บน GitHub ก่อนเป็นอย่างแรก
