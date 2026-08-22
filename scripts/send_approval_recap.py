from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from brief.config import load_settings
from brief.delivery import send_email


INK = "#111322"
MUTED = "#6d7488"
PAPER = "#f4f1ea"
CARD = "#fffdf8"
LINE = "#ded8ca"
ACCENT = "#4b63ff"
GREEN = "#0d9f6e"
AMBER = "#a36513"


def money(value: object) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0
    if number >= 1_000_000:
        return f"${number / 1_000_000:.1f}m"
    if number >= 1_000:
        return f"${number / 1_000:.0f}k"
    return f"${number:.0f}"


def short_ca(mint: str) -> str:
    return f"{mint[:5]}…{mint[-5:]}" if len(mint) > 12 else mint


def pct(value: object) -> str:
    try:
        return f"{float(value):+.0f}%"
    except (TypeError, ValueError):
        return "n/a"


def runner_line(item: dict[str, Any], index: int) -> str:
    flags = ", ".join(item.get("riskLabels") or [])
    if not flags:
        flags = "clean tape"
    why = []
    if item.get("volume24h"):
        why.append(f"{money(item['volume24h'])} volume")
    if item.get("turnover"):
        why.append(f"{float(item['turnover']):.1f}x turnover")
    if item.get("holders"):
        why.append(f"{int(item['holders']):,} holders")
    if item.get("top10Pct") is not None:
        why.append(f"top10 {float(item['top10Pct']):.1f}%")
    if item.get("lpLockedPct") is not None:
        why.append(f"LP {float(item['lpLockedPct']):.0f}% locked/burned")
    why_text = ", ".join(why)
    ca = str(item.get("mint") or "")
    return f"""
      <tr>
        <td style="padding:18px 0;border-top:1px solid {LINE};vertical-align:top;width:42px;color:{MUTED};font:700 13px Arial">{index:02d}</td>
        <td style="padding:18px 0;border-top:1px solid {LINE}">
          <div style="font:800 22px Arial;color:{INK};letter-spacing:-.02em">${item.get('symbol','?')}</div>
          <div style="font:500 14px Arial;color:{MUTED};margin-top:3px">{item.get('name','')}</div>
          <div style="font:500 15px/1.55 Arial;color:{INK};margin-top:12px">
            hit {money(item.get('marketCap'))} mcap · {pct(item.get('change24h'))} on the day · {why_text}
          </div>
          <div style="font:700 13px/1.5 Arial;color:{AMBER};margin-top:9px">{flags}</div>
          <div style="font:500 12px Arial;color:{MUTED};margin-top:10px">CA: {ca}</div>
          <a href="{item.get('url','')}" style="font:700 13px Arial;color:{ACCENT};text-decoration:none">Open chart ↗</a>
        </td>
      </tr>
    """


def kol_line(item: dict[str, Any], index: int) -> str:
    mint = str(item.get("mint") or "")
    url = f"https://dexscreener.com/solana/{mint}"
    return f"""
      <tr>
        <td style="padding:18px 0;border-top:1px solid {LINE};vertical-align:top;width:42px;color:{MUTED};font:700 13px Arial">{index:02d}</td>
        <td style="padding:18px 0;border-top:1px solid {LINE}">
          <div style="font:800 22px Arial;color:{INK};letter-spacing:-.02em">${item.get('symbol','?')}</div>
          <div style="font:500 15px/1.55 Arial;color:{INK};margin-top:10px">
            tracked wallets realised {float(item.get('realisedSol') or 0):+.1f} SOL across {item.get('traders', 0)} wallet(s)
          </div>
          <div style="font:700 13px/1.5 Arial;color:{GREEN};margin-top:9px">KOL/wallet favorite — included as flow evidence, not a clean runner</div>
          <div style="font:500 12px Arial;color:{MUTED};margin-top:10px">CA: {mint}</div>
          <a href="{url}" style="font:700 13px Arial;color:{ACCENT};text-decoration:none">Open chart ↗</a>
        </td>
      </tr>
    """


def html(data: dict[str, Any]) -> str:
    runners = list(data.get("runners") or [])
    runners_by_symbol = {item.get("symbol"): item for item in runners}
    chosen_runner_symbols = ["LIZARD", "CUPSINA", "KERMIT", "NENEM"]
    chosen_runners = [runners_by_symbol[s] for s in chosen_runner_symbols if s in runners_by_symbol]
    kol = list(data.get("kolProfit") or [])
    kol_by_symbol = {item.get("symbol"): item for item in kol}
    chosen_kol = [kol_by_symbol[s] for s in ["OMO", "CONK"] if s in kol_by_symbol]
    now = datetime.now()
    date_text = f"{now.strftime('%B')} {now.day}"
    runner_rows = "".join(runner_line(item, index + 1) for index, item in enumerate(chosen_runners))
    kol_rows = "".join(kol_line(item, len(chosen_runners) + index + 1) for index, item in enumerate(chosen_kol))
    rejected = list(data.get("blockedRunners") or [])
    rejected_bits = "".join(
        f"<li><b>${item.get('symbol')}</b> — {'; '.join(item.get('riskLabels') or [])}</li>"
        for item in rejected[:4]
    )
    return f"""<!doctype html>
<html><body style="margin:0;background:{PAPER};padding:28px 0;font-family:Arial,Helvetica,sans-serif;color:{INK}">
  <div style="max-width:680px;margin:0 auto;background:{CARD};border:1px solid {LINE};border-radius:24px;overflow:hidden">
    <div style="padding:34px 34px 22px;background:#111322;color:white">
      <div style="font:700 12px Arial;letter-spacing:.14em;text-transform:uppercase;color:#b9bfd6">Fomo Onchain approval draft</div>
      <h1 style="font:900 34px/1.02 Arial;margin:12px 0 0;letter-spacing:-.05em">Daily Memecoin Recap — {date_text}</h1>
      <p style="font:500 15px/1.6 Arial;color:#d7dbea;margin:16px 0 0">Six-name cut from today’s scan: clean/caveated runners first, then wallet-flow favorites. This is the version for your approval before Bryan / Alan / Jacob get anything.</p>
    </div>
    <div style="padding:28px 34px 8px">
      <h2 style="font:900 21px Arial;margin:0 0 8px;letter-spacing:-.03em">Runners and wallet-favorites</h2>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{runner_rows}{kol_rows}</table>
    </div>
    <div style="padding:24px 34px 34px">
      <h2 style="font:900 19px Arial;margin:0 0 10px;letter-spacing:-.03em">Rejected tape</h2>
      <ul style="font:500 14px/1.65 Arial;color:{MUTED};padding-left:18px;margin:0">{rejected_bits}</ul>
      <p style="font:500 12px/1.5 Arial;color:{MUTED};margin:24px 0 0">Data, not advice. Approval-only test send.</p>
    </div>
  </div>
</body></html>"""


async def main() -> int:
    settings = load_settings("config.toml")
    settings.values.setdefault("delivery", {})["email_to"] = ["ue06prog@gmail.com"]
    data = json.loads(Path("web/data/latest.json").read_text(encoding="utf-8-sig"))
    subject = "Fomo Onchain approval draft | 6-name daily recap"
    count = await send_email(settings, subject, html(data))
    print(f"sent={count} recipient=ue06prog@gmail.com")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
