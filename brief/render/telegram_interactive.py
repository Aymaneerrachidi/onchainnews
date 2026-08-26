from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from math import ceil
from typing import Any


PAGE_SIZE = 8
CHAINS = (("All", "all"), ("Solana", "solana"), ("BNB", "bsc"), ("Base", "base"), ("ETH", "ethereum"), ("Robinhood", "robinhood"))
BANDS = (("All caps", "all"), ("$250K–$500K", "250k-500k"), ("$500K–$1M", "500k-1m"), ("$1M–$10M", "1m-10m"), ("$10M+", "10m-plus"))


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _money(value: Any) -> str:
    number = _number(value)
    absolute = abs(number)
    if absolute >= 1_000_000_000:
        return f"${number / 1_000_000_000:.1f}B"
    if absolute >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    if absolute >= 1_000:
        return f"${number / 1_000:.0f}K"
    return f"${number:.0f}"


def _peak(row: dict[str, Any]) -> float:
    return max(_number(row.get(key)) for key in ("observedPeakMarketCap", "peakMarketCap", "marketCap"))


def _in_band(peak: float, band: str) -> bool:
    if band == "250k-500k":
        return 250_000 <= peak < 500_000
    if band == "500k-1m":
        return 500_000 <= peak < 1_000_000
    if band == "1m-10m":
        return 1_000_000 <= peak < 10_000_000
    if band == "10m-plus":
        return peak >= 10_000_000
    # "All caps" must mean the complete validated runner universe. Named cap
    # bands retain their explicit floors.
    return True


def filter_rows(snapshot: dict[str, Any], chain: str = "all", band: str = "all") -> list[dict[str, Any]]:
    source = snapshot.get("runnerUniverse") or snapshot.get("runners") or []
    unique: dict[str, dict[str, Any]] = {}
    for row in source:
        row_chain = str(row.get("chain") or "").lower()
        if chain != "all" and row_chain != chain:
            continue
        if not _in_band(_peak(row), band):
            continue
        key = f"{row_chain}:{str(row.get('mint') or '').lower()}"
        if key not in unique or _peak(row) > _peak(unique[key]):
            unique[key] = row
    return sorted(unique.values(), key=lambda row: (_peak(row), _number(row.get("volume24h"))), reverse=True)


def _fomo_url(row: dict[str, Any]) -> str:
    chain = "bnb" if str(row.get("chain")).lower() == "bsc" else str(row.get("chain") or "")
    return f"https://fomo.family/tokens/{chain}/{row.get('mint', '')}"


def _story(row: dict[str, Any]) -> tuple[str, str]:
    for item in row.get("xInteractions") or []:
        if item.get("summary") and str(item.get("handle") or "").lower() != "mellometrics":
            return str(item["summary"]), str(item.get("url") or "")
    why = (row.get("providerEvidence") or {}).get("why") or {}
    choices = (
        (why.get("cause"), why.get("sourceUrl")),
        (row.get("lore"), ""),
        (row.get("catalyst"), ""),
    )
    for text, url in choices:
        if text:
            clean = " ".join(str(text).split())
            return (clean[:237] + "…" if len(clean) > 240 else clean), str(url or "")
    return "Measured 24-hour runner; no stronger attributable story was available.", ""


def _callback(chain: str, band: str, date: str, page: int, action: str = "filter") -> str:
    return f"tg|{action}|{chain}|{band}|{date}|{page}"


def render_snapshot_message(
    snapshot: dict[str, Any],
    *,
    chain: str = "all",
    band: str = "all",
    page: int = 0,
    report_url: str = "https://onchainnews-rho.vercel.app",
) -> dict[str, Any]:
    rows = filter_rows(snapshot, chain, band)
    pages = max(1, ceil(len(rows) / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    visible = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    generated = str(snapshot.get("generatedAt") or "latest")
    date = "".join(character for character in generated[:10] if character.isdigit()) or "latest"
    chain_label = dict(CHAINS).get(chain, chain)
    band_label = dict(BANDS).get(band, band)
    coin_lines: list[str] = []
    for row in visible:
        holders = "holders unknown" if row.get("holders") is None else f"{int(_number(row['holders'])):,} holders"
        top10 = "top10 unknown" if row.get("top10Pct") is None else f"top10 {_number(row['top10Pct']):.1f}%"
        story, source = _story(row)
        source_link = f' · <a href="{escape(source, quote=True)}">source</a>' if source.startswith("http") else ""
        coin_lines.append(
            f'<a href="{escape(_fomo_url(row), quote=True)}"><b>${escape(str(row.get("symbol") or "?").upper())}</b></a>'
            f' — <b>now {_money(row.get("marketCap"))}</b> · high {_money(_peak(row))} · '
            f'liq {_money(row.get("liquidity"))} · {holders} · {top10}\n{escape(story)}{source_link}'
        )
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    text = "\n\n".join((
        "🟣 <b>FOMO ONCHAIN · DAILY RUNNERS</b>",
        f"<b>{escape(chain_label)} · {escape(band_label)}</b> · full 24h tape",
        "\n\n".join(coin_lines) or "No qualified runners match this filter.",
        f"<i>{len(rows)} screened runners · page {page + 1}/{pages} · MC {stamp} UTC</i>",
    ))

    def button(label: str, next_chain: str, next_band: str, next_page: int = 0) -> dict[str, str]:
        active = "● " if next_chain == chain and next_band == band else ""
        total = len(filter_rows(snapshot, next_chain, next_band))
        return {"text": f"{active}{label} {total}", "callback_data": _callback(next_chain, next_band, date, next_page)}

    chain_buttons = [button(label, value, band) for label, value in CHAINS]
    band_buttons = [button(label, chain, value) for label, value in BANDS]
    keyboard = [chain_buttons[:3], chain_buttons[3:], band_buttons[:3], band_buttons[3:]]
    keyboard.append([
        {"text": "‹ Prev", "callback_data": _callback(chain, band, date, max(0, page - 1))},
        {"text": f"{page + 1}/{pages}", "callback_data": _callback(chain, band, date, page)},
        {"text": "Next ›", "callback_data": _callback(chain, band, date, min(pages - 1, page + 1))},
        {"text": "↻ MC", "callback_data": _callback(chain, band, date, page, "refresh")},
    ])
    if report_url:
        keyboard.append([{"text": "Open full website", "url": report_url}])
    return {
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": keyboard},
    }
