#!/usr/bin/env python3
"""
Parse Discord messages exported by DiscordChatExporter (JSON format).
Extracts trading signals and results posted by "bot signal".

Usage:
    python parse_signals.py <exported_file.json> [--output signals.csv]
"""

import re
import json
import csv
import argparse
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional


@dataclass
class Signal:
    timestamp: str
    message_id: str
    direction: str          # LONG / SHORT
    symbol: str             # e.g. BTCUSDT
    timeframe: str          # e.g. 30
    entry: float
    sl: Optional[float]
    tp: Optional[float]
    sl_pct: Optional[float]
    tp_pct: Optional[float]
    rr: Optional[float]
    win_rate_pct: Optional[float]
    profit_factor: Optional[float]
    wins: Optional[int]
    losses: Optional[int]
    balance: Optional[float]
    return_pct: Optional[float]
    trade_size: Optional[float]
    signal_type: str = "SIGNAL"   # SIGNAL / SL_HIT / TP_HIT
    pnl: Optional[float] = None
    exit_price: Optional[float] = None
    raw: str = field(default="", repr=False)


def _float(s: str) -> Optional[float]:
    try:
        return float(re.sub(r"[,$%]", "", s.strip()))
    except (ValueError, AttributeError):
        return None


def _int(s: str) -> Optional[int]:
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return None


def parse_message(msg_id: str, timestamp: str, content: str) -> Optional[Signal]:
    """Parse a single bot signal message into a Signal object."""
    text = content.strip()

    # ---- Detect message kind ----
    is_sl_hit = bool(re.search(r"SL\s+HIT", text, re.IGNORECASE))
    is_tp_hit = bool(re.search(r"TP\s+HIT", text, re.IGNORECASE))
    is_signal = bool(re.search(r"(LONG|SHORT)\s+SIGNAL", text, re.IGNORECASE))

    if not (is_sl_hit or is_tp_hit or is_signal):
        return None

    # ---- Direction ----
    dir_match = re.search(r"\b(LONG|SHORT)\b", text, re.IGNORECASE)
    direction = dir_match.group(1).upper() if dir_match else "UNKNOWN"

    # ---- Symbol & Timeframe ----
    sym_match = re.search(r"([A-Z]{3,10}USDT?)\s*\|[^\d]*(\d+)", text)
    symbol    = sym_match.group(1) if sym_match else "UNKNOWN"
    timeframe = sym_match.group(2) if sym_match else None

    # ---- Entry ----
    entry_match = re.search(r"Entry[:\s]+([0-9,\.]+)", text)
    entry = _float(entry_match.group(1)) if entry_match else None

    # ---- SL / TP (signal messages) ----
    sl = sl_pct = tp = tp_pct = rr = None
    if is_signal:
        sl_match  = re.search(r"SL[:\s]+([0-9,\.]+)\s*\(([+-]?[0-9\.]+)%\)", text)
        tp_match  = re.search(r"TP[:\s]+([0-9,\.]+)\s*\(([+-]?[0-9\.]+)%\)", text)
        rr_match  = re.search(r"RR[:\s]+([0-9\.]+)", text)
        sl     = _float(sl_match.group(1))  if sl_match  else None
        sl_pct = _float(sl_match.group(2))  if sl_match  else None
        tp     = _float(tp_match.group(1))  if tp_match  else None
        tp_pct = _float(tp_match.group(2))  if tp_match  else None
        rr     = _float(rr_match.group(1))  if rr_match  else None

    # ---- Win rate / PF / W-L count (signal messages) ----
    wr = pf = wins = losses = None
    wr_match  = re.search(r"WR[:\s]+([0-9\.]+)%", text)
    pf_match  = re.search(r"PF[:\s]+([0-9\.]+)", text)
    wl_match  = re.search(r"✅\s*(\d+)\s*[\|]?\s*❌\s*(\d+)", text)
    wr    = _float(wr_match.group(1)) if wr_match else None
    pf    = _float(pf_match.group(1)) if pf_match else None
    wins  = _int(wl_match.group(1))   if wl_match else None
    losses= _int(wl_match.group(2))   if wl_match else None

    # ---- Balance / Return / Trade Size ----
    bal_match  = re.search(r"Balance[:\s]+\$?([0-9,\.]+)", text)
    ret_match  = re.search(r"Return[:\s]+([0-9\.]+)%", text)
    size_match = re.search(r"Trade\s+Size[:\s]+\$?([0-9,\.]+)", text)
    balance    = _float(bal_match.group(1))  if bal_match  else None
    ret_pct    = _float(ret_match.group(1))  if ret_match  else None
    trade_size = _float(size_match.group(1)) if size_match else None

    # ---- Result messages (SL/TP HIT) ----
    pnl = exit_price = None
    if is_sl_hit or is_tp_hit:
        exit_match = re.search(r"Exit[:\s]+([0-9,\.]+)", text)
        pnl_match  = re.search(r"P/L[:\s]+\$?([+-]?[0-9,\.]+)", text)
        exit_price = _float(exit_match.group(1)) if exit_match else None
        pnl        = _float(pnl_match.group(1))  if pnl_match  else None

    signal_type = "SL_HIT" if is_sl_hit else ("TP_HIT" if is_tp_hit else "SIGNAL")

    return Signal(
        timestamp=timestamp,
        message_id=msg_id,
        direction=direction,
        symbol=symbol,
        timeframe=timeframe,
        entry=entry,
        sl=sl, tp=tp,
        sl_pct=sl_pct, tp_pct=tp_pct,
        rr=rr,
        win_rate_pct=wr,
        profit_factor=pf,
        wins=wins,
        losses=losses,
        balance=balance,
        return_pct=ret_pct,
        trade_size=trade_size,
        signal_type=signal_type,
        pnl=pnl,
        exit_price=exit_price,
        raw=content,
    )


def load_export(path: str) -> list[Signal]:
    """Load a DiscordChatExporter JSON file and return parsed signals."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    messages = data.get("messages", [])
    signals = []
    for msg in messages:
        author = msg.get("author", {})
        # Filter only messages from the bot (isBot=True or name contains "bot signal")
        name = (author.get("name") or "").lower()
        is_bot = author.get("isBot", False)
        if not (is_bot or "signal" in name):
            continue

        content   = msg.get("content", "")
        msg_id    = msg.get("id", "")
        timestamp = msg.get("timestamp", "")

        sig = parse_message(msg_id, timestamp, content)
        if sig:
            signals.append(sig)

    return signals


def save_csv(signals: list[Signal], path: str):
    if not signals:
        print("No signals to save.")
        return
    rows = [asdict(s) for s in signals]
    # Remove raw field from CSV (too verbose)
    for r in rows:
        r.pop("raw", None)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved {len(signals)} rows → {path}")


def main():
    parser = argparse.ArgumentParser(description="Parse DiscordChatExporter JSON for trading signals")
    parser.add_argument("input", help="Path to the exported JSON file")
    parser.add_argument("--output", default="signals.csv", help="Output CSV path (default: signals.csv)")
    args = parser.parse_args()

    signals = load_export(args.input)
    print(f"Parsed {len(signals)} signal messages from {args.input}")

    new_signals = [s for s in signals if s.signal_type == "SIGNAL"]
    sl_hits     = [s for s in signals if s.signal_type == "SL_HIT"]
    tp_hits     = [s for s in signals if s.signal_type == "TP_HIT"]
    print(f"  SIGNAL:  {len(new_signals)}")
    print(f"  SL_HIT:  {len(sl_hits)}")
    print(f"  TP_HIT:  {len(tp_hits)}")

    save_csv(signals, args.output)


if __name__ == "__main__":
    main()
