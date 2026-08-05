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
