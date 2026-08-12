"""
Affiliate link generation + attribution tag

จุดสำคัญที่สุดในไฟล์นี้ไม่ใช่ตัวลิงก์ แต่คือ **tag** ที่ฝังไปกับทุกลิงก์
tag ทำให้ตอบได้ว่าเงินที่เข้ามา มาจากดีลไหน หมวดไหน วันไหน
ถ้าไม่มี tag เราจะได้แค่ยอดรวม แล้วก็ปรับปรุงอะไรไม่ได้เลย
นี่คือขาที่ต่อกับระบบวัด EPC (งานเฟสถัดไป)

หมายเหตุเรื่อง Involve Asia API:
หน้า docs ของเขา (involve.asia/partners/api-overview) ต้องล็อกอินถึงจะเปิดได้
โค้ดนี้จึงทำ endpoint/field ให้ override ผ่าน env ได้ทั้งหมด และถ้าเรียกไม่สำเร็จ
จะ fallback ไปใช้ลิงก์ตรง + tag เสมอ (ไม่มีการยิง alert ที่ลิงก์พัง)
ก่อนใช้จริง: เทียบ field name กับ docs ในบัญชี partner แล้วปรับ env ตามนั้น
"""

import hashlib
import os
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import requests

from deal_bot.config import Product

API_BASE = os.environ.get("INVOLVE_API_BASE", "https://api.involve.asia/api")
API_KEY = os.environ.get("INVOLVE_API_KEY", "")
API_SECRET = os.environ.get("INVOLVE_API_SECRET", "")
TIMEOUT = int(os.environ.get("DEAL_HTTP_TIMEOUT", "20"))

# ชื่อ query param ที่ใช้ส่ง sub-id ต่างกันไปตาม network — Involve ใช้ aff_sub
SUBID_PARAM = os.environ.get("DEAL_SUBID_PARAM", "aff_sub")


CHANNEL = os.environ.get("DEAL_CHANNEL", "fbreel")


def make_tag(product: Product, when: datetime | None = None,
             channel: str | None = None) -> str:
    """
    tag สั้น ๆ ที่ unique พอ และอ่านออกด้วยตาเวลา debug
    รูปแบบ: <category>-<channel>-<yymmdd>-<hash6>
    เช่น  monitor-fbreel-260810-a3f9c1

    channel อยู่ใน tag ตั้งแต่วันแรกเพราะการเติมทีหลังแปลว่าข้อมูลเก่าใช้ไม่ได้
    ตอนนี้มีช่องทางเดียว (FB Reels) แต่ถ้าวันหนึ่งเพิ่มช่องทาง เราจะตอบได้ทันที
    ว่าช่องไหนทำเงิน โดยไม่ต้องเริ่มนับใหม่
    """
    when = when or datetime.now(timezone.utc)
    channel = (channel or CHANNEL).lower()
    digest = hashlib.sha1(f"{product.id}{when.isoformat()}".encode()).hexdigest()[:6]
    category = "".join(c for c in product.category.lower() if c.isalnum())[:12] or "misc"
    channel = "".join(c for c in channel if c.isalnum())[:10] or "direct"
    return f"{category}-{channel}-{when.strftime('%y%m%d')}-{digest}"


def _append_param(url: str, key: str, value: str) -> str:
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query))
    query[key] = value
    return urlunparse(parts._replace(query=urlencode(query)))


def _authenticate(session: requests.Session) -> str | None:
    if not (API_KEY and API_SECRET):
        return None
    try:
        resp = session.post(
            f"{API_BASE}/authenticate",
            data={"key": API_KEY, "secret": API_SECRET},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[affiliate] authenticate failed: {e}")
        return None

    token = (payload.get("data") or {}).get("token") or payload.get("token")
    if not token:
        print(f"[affiliate] ไม่พบ token ใน response — ตรวจ field name กับ docs อีกที")
    return token


def build_link(product: Product, tag: str, session: requests.Session | None = None) -> str:
    """
    คืน affiliate deeplink พร้อม sub-id
    ถ้าไม่มี credential หรือ API ล้ม → คืนลิงก์ตรงที่ติด tag ไว้ (ยังคลิกได้ปกติ
    แค่ไม่ได้ค่าคอม) เจตนาคือ **ไม่ปล่อยลิงก์เสียออกไปหาคนอ่าน** เด็ดขาด
    """
    fallback = _append_param(product.url, SUBID_PARAM, tag)

    if not (API_KEY and API_SECRET):
        print("[affiliate] ไม่ได้ตั้ง INVOLVE_API_KEY/SECRET — ใช้ลิงก์ตรง (ไม่ได้ค่าคอม)")
        return fallback

    session = session or requests.Session()
    token = _authenticate(session)
    if not token:
        return fallback

    try:
        resp = session.post(
            f"{API_BASE}/deeplink/generate",
            headers={"Authorization": f"Bearer {token}"},
            data={"url": product.url, SUBID_PARAM: tag},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[affiliate] deeplink failed ({e}) — fallback ลิงก์ตรง")
        return fallback

    data = payload.get("data") or {}
    link = data.get("tracking_link") or data.get("deeplink") or data.get("url")
    if not link:
        print("[affiliate] response ไม่มีลิงก์ — fallback ลิงก์ตรง")
        return fallback

    return _append_param(link, SUBID_PARAM, tag)
