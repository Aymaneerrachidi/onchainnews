from __future__ import annotations

import copy
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from brief.config import Settings
from brief.delivery import TelegramDeliveryError, send_telegram, write_html
from brief.engine import build_brief
from brief.ledger import Ledger
from brief.models import Brief, Candidate
from brief.openai_images import OpenAIImageError, configured as openai_image_configured, generate_runner_background
from brief.render.formatting import money, pct
from brief.render.payload import build_payload
from brief.x_poster import XPostError, configured as x_posting_configured, post_image

log = logging.getLogger("brief.pulse")


@dataclass(slots=True)
class PulseTrigger:
    candidate: Candidate
    passes: list[dict[str, Any]]
    image_path: Path | None = None
    x_post_id: str | None = None
    error: str | None = None


@dataclass(slots=True)
class PulseResult:
    checked: int
    triggers: list[PulseTrigger]
    state_path: Path
    latest_written: Path | None = None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _state_path(settings: Settings) -> Path:
    raw = str(settings.get("pulse", "state_path", "web/data/pulse-state.json"))
    path = Path(raw)
    return path if path.is_absolute() else settings.root / path


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "passes": {}, "posted": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "passes": {}, "posted": {}}
    if not isinstance(data, dict):
        return {"version": 1, "passes": {}, "posted": {}}
    data.setdefault("version", 1)
    data.setdefault("passes", {})
    data.setdefault("posted", {})
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_html(path, json.dumps(state, indent=1, sort_keys=True))


def _pass_entry(candidate: Candidate, now: datetime) -> dict[str, Any]:
    token = candidate.token
    txns_24h = getattr(token, "txns_24h", None)
    trades_24h = getattr(txns_24h, "total", 0) if txns_24h is not None else 0
    txns_6h = getattr(token, "txns_6h", None)
    trades_6h = getattr(txns_6h, "total", 0) if txns_6h is not None else 0
    signals = getattr(candidate, "signals", None)
    safety = getattr(candidate, "safety", None)
    return {
        "takenAt": _iso(now),
        "symbol": token.symbol,
        "name": token.name,
        "mint": token.mint,
        "chain": token.chain_id,
        "url": token.url,
        "marketCap": token.market_cap,
        "liquidity": token.liquidity_usd,
        "volume24h": token.volume_24h,
        "volume6h": getattr(token, "volume_6h", token.volume_24h),
        "trades24h": trades_24h,
        "trades6h": trades_6h,
        "buys6h": getattr(txns_6h, "buys", 0) if txns_6h is not None else 0,
        "sells6h": getattr(txns_6h, "sells", 0) if txns_6h is not None else 0,
        "change24h": token.price_change_24h,
        "change6h": getattr(token, "price_change_6h", token.price_change_24h),
        "change1h": token.price_change_1h,
        "runMultiple": candidate.run_multiple,
        "turnover": getattr(signals, "turnover", token.volume_24h / token.market_cap if token.market_cap else 0),
        "ageHours": getattr(signals, "age_hours", None),
        "buyRatio6h": getattr(signals, "buy_imbalance_6h", None),
        "holders": getattr(safety, "holder_count", None),
        "top10Pct": getattr(safety, "top10_pct", None),
        "lpLockedPct": getattr(safety, "lp_locked_or_burned_pct", None),
        "kolBuyers": candidate.kol_buyers,
        "riskLabels": candidate.risk_labels,
        "scores": getattr(candidate, "scores", {}),
        "scoreComponents": getattr(candidate, "score_components", {}),
        "classification": getattr(candidate, "classification", ""),
        "read": candidate.read,
    }


def record_runner_passes(
    state: dict[str, Any],
    runners: list[Candidate],
    now: datetime,
    *,
    window_hours: float,
    required_passes: int,
    repost_after_hours: float,
    min_gap_minutes: float,
) -> list[tuple[Candidate, list[dict[str, Any]]]]:
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=window_hours)
    posted_cutoff = now.astimezone(timezone.utc) - timedelta(hours=repost_after_hours)
    passes = state.setdefault("passes", {})
    posted = state.setdefault("posted", {})
    triggered: list[tuple[Candidate, list[dict[str, Any]]]] = []

    active_mints = {candidate.token.mint for candidate in runners}
    for mint in list(passes):
        kept = [
            entry for entry in passes.get(mint, [])
            if (stamp := _parse_time(entry.get("takenAt"))) and stamp >= cutoff
        ]
        if kept or mint in active_mints:
            passes[mint] = kept
        else:
            passes.pop(mint, None)

    for candidate in runners:
        mint = candidate.token.mint
        entries = list(passes.get(mint, []))
        last_seen = _parse_time(entries[-1].get("takenAt")) if entries else None
        if last_seen is None or now.astimezone(timezone.utc) - last_seen >= timedelta(minutes=min_gap_minutes):
            entries.append(_pass_entry(candidate, now))
        entries = [
            entry for entry in entries
            if (stamp := _parse_time(entry.get("takenAt"))) and stamp >= cutoff
        ]
        passes[mint] = entries

        last_posted = _parse_time(posted.get(mint))
        already_posted = last_posted is not None and last_posted >= posted_cutoff
        if len(entries) >= required_passes and not already_posted:
            triggered.append((candidate, entries))
            posted[mint] = _iso(now)

    state["updatedAt"] = _iso(now)
    return triggered


def _pulse_settings(settings: Settings) -> Settings:
    values = copy.deepcopy(settings.values)
    pulse = values.setdefault("pulse", {})
    if bool(pulse.get("solana_only", True)):
        values.setdefault("thresholds", {})["chains"] = ["solana"]
    max_ranked = int(pulse.get("max_ranked_tokens", 250))
    if max_ranked > 0:
        values.setdefault("birdeye", {})["max_tokens"] = max_ranked
    if bool(pulse.get("disable_holder_snapshots", True)):
        values.setdefault("holders", {})["enabled"] = False
    if bool(pulse.get("disable_kol_scan", True)):
        values.setdefault("kol", {})["enabled"] = False
        # The hourly job is the tape recorder. It must remember market runners
        # even though it deliberately skips the expensive wallet scan. The
        # morning job intersects these passes with real KOL trades and safety.
        journal = values.setdefault("journal", {})
        journal["require_kol_trade_for_publish"] = False
        journal["require_wallet_touch_for_publish"] = False
        journal["wallet_touch_required_above_multiple"] = 0.0
        # Capture a wide but still safety-screened tape. These are observations,
        # not morning endorsements; the daily build applies wallet consensus.
        journal["target_min_runners"] = int(pulse.get("capture_target", 12) or 12)
        journal["fill_with_caveated_runners"] = True
        journal["fill_min_runner_score"] = float(pulse.get("capture_min_runner_score", 15.0) or 15.0)
        journal["fill_min_organic_score"] = float(pulse.get("capture_min_organic_score", 25.0) or 25.0)
        journal["fill_max_manipulation"] = float(pulse.get("capture_max_manipulation", 65.0) or 65.0)
        hard_terms = [
            str(term) for term in (journal.get("fill_hard_block_terms", []) or [])
            if not any(
                marker in str(term).casefold()
                for marker in (
                    "tracked kol wallet",
                    "move with no linked social context",
                    "move on only 0.",
                )
            )
        ]
        journal["fill_hard_block_terms"] = hard_terms
    if bool(pulse.get("disable_x_scan", True)):
        values.setdefault("x", {})["enabled"] = False
    return Settings(root=settings.root, values=values)


def _image_url(candidate: Candidate) -> str:
    info = (candidate.token.raw or {}).get("info") or {}
    return str(info.get("imageUrl") or info.get("image") or "")


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    names = (
        ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/calibrib.ttf")
        if bold else
        ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf")
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _display_font(size: int):
    from PIL import ImageFont

    for name in (
        "C:/Windows/Fonts/impact.ttf",
        "C:/Windows/Fonts/ARIALNB.TTF",
        "C:/Windows/Fonts/bahnschrift.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return _font(size, True)


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "..."


def _fit_font(text: str, size: int, max_width: int, *, bold: bool = True, min_size: int = 28):
    font_size = size
    font = _font(font_size, bold)
    probe = ImageDrawProbe()
    while probe.width(text, font) > max_width and font_size > min_size:
        font_size -= 4
        font = _font(font_size, bold)
    return font


def _fit_display_font(text: str, size: int, max_width: int, *, min_size: int = 36):
    font_size = size
    font = _display_font(font_size)
    probe = ImageDrawProbe()
    while probe.width(text, font) > max_width and font_size > min_size:
        font_size -= 4
        font = _display_font(font_size)
    return font


class ImageDrawProbe:
    def __init__(self) -> None:
        from PIL import Image, ImageDraw

        self._draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    def width(self, text: str, font: Any) -> int:
        bbox = self._draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip(".-") or "token"


async def _dex_logo_url(candidate: Candidate) -> str:
    chain = str(candidate.token.chain_id or "solana").lower()
    mint = str(candidate.token.mint or "")
    if not chain or not mint:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"https://api.dexscreener.com/tokens/v1/{chain}/{mint}")
        if response.status_code >= 400:
            return ""
        payload = response.json()
        pairs = payload if isinstance(payload, list) else payload.get("pairs", []) if isinstance(payload, dict) else []
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            info = pair.get("info") or {}
            url = str(info.get("imageUrl") or info.get("image") or "")
            if url:
                return url
    except Exception:
        return ""
    return ""


async def _logo(candidate: Candidate, size: int):
    from PIL import Image, ImageDraw

    url = _image_url(candidate) or await _dex_logo_url(candidate)
    if url:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(url)
            if response.status_code < 400:
                image = Image.open(BytesIO(response.content)).convert("RGBA")
                image.thumbnail((size, size))
                fitted = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                fitted.alpha_composite(image, ((size - image.width) // 2, (size - image.height) // 2))
                mask = Image.new("L", (size, size), 0)
                draw = ImageDraw.Draw(mask)
                draw.ellipse((0, 0, size - 1, size - 1), fill=255)
                fitted.putalpha(mask)
                return fitted
        except Exception:
            pass
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for y in range(size):
        t = y / max(1, size - 1)
        color = (
            int(24 * (1 - t) + 92 * t),
            int(28 * (1 - t) + 73 * t),
            int(83 * (1 - t) + 255 * t),
            255,
        )
        draw.line((0, y, size, y), fill=color)
    draw.ellipse((0, 0, size - 1, size - 1), outline=(255, 255, 255, 225), width=max(2, size // 22))
    draw.ellipse((size * 0.12, size * 0.10, size * 0.88, size * 0.88), outline=(20, 241, 149, 150), width=max(2, size // 32))
    draw.ellipse((size * 0.18, size * 0.14, size * 0.52, size * 0.34), fill=(255, 255, 255, 28))
    initials = _fit(candidate.token.symbol.strip("$").upper(), 4)
    font_size = max(22, int(size * 0.28))
    font = _font(font_size, True)
    bbox = draw.textbbox((0, 0), initials, font=font)
    while (bbox[2] - bbox[0]) > size - 26 and font_size > 18:
        font_size -= 2
        font = _font(font_size, True)
        bbox = draw.textbbox((0, 0), initials, font=font)
    draw.text(
        ((size - (bbox[2] - bbox[0])) / 2 + 2, (size - (bbox[3] - bbox[1])) / 2 + 1),
        initials,
        fill=(0, 0, 0, 95),
        font=font,
    )
    draw.text(((size - (bbox[2] - bbox[0])) / 2, (size - (bbox[3] - bbox[1])) / 2 - 2), initials, fill="#FFFFFF", font=font)
    return image


def _draw_chain_badge(draw: Any, x: int, y: int) -> None:
    draw.rounded_rectangle((x, y, x + 136, y + 42), radius=21, fill="#111735", outline="#AEBBFF", width=1)
    draw.rounded_rectangle((x + 16, y + 10, x + 51, y + 15), radius=3, fill="#14F195")
    draw.rounded_rectangle((x + 20, y + 19, x + 55, y + 24), radius=3, fill="#9945FF")
    draw.rounded_rectangle((x + 16, y + 28, x + 51, y + 33), radius=3, fill="#14F195")
    draw.text((x + 66, y + 10), "SOLANA", fill="#F7F8FF", font=_font(17, True))


def _derived_start_market_cap(candidate: Candidate) -> float:
    multiple = max(float(candidate.run_multiple or 1.0), 1.0)
    return max(float(candidate.token.market_cap or 0.0) / multiple, 0.0)


def _draw_reference_background(canvas: Any, *, ai: bool, template: bool) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    width, height = canvas.size
    if not ai and not template:
        top = (98, 76, 255)
        bottom = (205, 192, 255)
        draw = ImageDraw.Draw(canvas)
        for y in range(height):
            t = y / height
            color = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
            draw.line((0, y, width, y), fill=color)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glow = ImageDraw.Draw(overlay)
    glow.ellipse((470, -120, 1140, 480), outline=(255, 255, 255, 80), width=6)
    glow.ellipse((535, 112, 950, 445), outline=(255, 255, 255, 55), width=4)
    glow.line((260, 1080, 420, height + 80), fill=(255, 255, 255, 120), width=2)
    glow.line((260, 1080, 175, height + 70), fill=(255, 255, 255, 70), width=1)
    glow.line((260, 1080, 515, height + 70), fill=(255, 255, 255, 70), width=1)
    blurred = overlay.filter(ImageFilter.GaussianBlur(5))
    canvas.alpha_composite(blurred)


def _reference_gradient(width: int, height: int):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (width, height), "#6650FF")
    draw = ImageDraw.Draw(image)
    top = (99, 75, 255)
    bottom = (208, 195, 255)
    for y in range(height):
        t = y / height
        color = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        draw.line((0, y, width, y), fill=color + (255,))
    return image


def _draw_fomo_lockup(draw: Any, x: int, y: int) -> None:
    draw.rounded_rectangle((x, y + 13, x + 42, y + 43), radius=15, fill="#FFFFFF")
    draw.rounded_rectangle((x + 28, y + 13, x + 70, y + 43), radius=15, fill="#FFFFFF")
    draw.ellipse((x + 21, y + 16, x + 49, y + 44), fill="#6657FF")
    draw.rectangle((x + 86, y + 4, x + 88, y + 54), fill=(255, 255, 255, 150))
    draw.text((x + 104, y + 2), "fomo", fill="#FFFFFF", font=_font(42, True))


def _draw_coin_medallion(canvas: Any, logo: Any, x: int, y: int, size: int) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.ellipse((x - 22, y - 18, x + size + 22, y + size + 26), fill=(28, 23, 120, 75))
    draw.ellipse((x - 16, y - 16, x + size + 16, y + size + 16), outline=(255, 255, 255, 215), width=7)
    draw.ellipse((x - 4, y - 4, x + size + 4, y + size + 4), outline=(91, 72, 255, 230), width=8)
    layer = layer.filter(ImageFilter.GaussianBlur(0.7))
    canvas.alpha_composite(layer)
    logo = logo.resize((size, size))
    canvas.alpha_composite(logo, (x, y))


def _draw_mock_trading_card(draw: Any, candidate: Candidate, passes: list[dict[str, Any]], x: int, y: int) -> None:
    card_w, card_h = 500, 388
    draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=26, fill="#090B18", outline="#7E76FF", width=3)
    draw.rounded_rectangle((x + 22, y + 22, x + 132, y + 58), radius=18, fill="#1F2447")
    draw.text((x + 38, y + 28), "Open", fill="#92A2FF", font=_font(18, True))
    draw.rounded_rectangle((x + card_w - 120, y + 20, x + card_w - 28, y + 60), radius=12, fill="#5266FF")
    draw.text((x + card_w - 100, y + 31), "Follow", fill="#FFFFFF", font=_font(18, True))
    draw.text((x + 34, y + 86), f"${_fit(candidate.token.symbol.upper(), 12)}", fill="#FFFFFF", font=_font(26, True))
    draw.text((x + card_w - 150, y + 84), money(candidate.token.market_cap), fill="#FFFFFF", font=_font(24, True))
    draw.text((x + card_w - 150, y + 113), "Market cap", fill="#BFC3D9", font=_font(18))

    chart = [
        (x + 36, y + 238),
        (x + 82, y + 218),
        (x + 128, y + 230),
        (x + 174, y + 186),
        (x + 220, y + 204),
        (x + 266, y + 176),
        (x + 312, y + 184),
        (x + 358, y + 148),
        (x + 404, y + 158),
        (x + 454, y + 126),
    ]
    for i in range(len(chart) - 1):
        draw.line((chart[i], chart[i + 1]), fill="#16E97C", width=4)
    for cx, cy in chart[1::2]:
        draw.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill="#FF6A31")

    pnl = candidate.token.market_cap - _derived_start_market_cap(candidate)
    draw.rounded_rectangle((x + 24, y + 270, x + card_w - 24, y + 352), radius=18, fill="#111421", outline="#22283F", width=1)
    draw.text((x + 44, y + 288), f"{money(pnl)}", fill="#27F28B", font=_font(30, True))
    draw.text((x + 44, y + 322), f"{candidate.run_multiple:.1f}x runner", fill="#D6D9EA", font=_font(18))
    draw.text((x + 276, y + 294), f"{len(passes)} passes", fill="#FFFFFF", font=_font(22, True))
    draw.text((x + 276, y + 322), f"{pct(candidate.token.price_change_1h, 0)} 1h", fill="#D6D9EA", font=_font(18))


async def _ohlcv_items(candidate: Candidate, settings: Settings, now: datetime) -> list[dict[str, float]]:
    key = os.environ.get("BIRDEYE_API_KEY", "").strip()
    if not key or str(candidate.token.chain_id).lower() != "solana":
        return []
    urls = settings.section("sources")
    base_url = str(urls.get("birdeye_base_url", "https://public-api.birdeye.so")).rstrip("/")
    age_hours = candidate.signals.age_hours if getattr(candidate, "signals", None) else None
    hours = max(3.0, min(24.0, float(age_hours or 24.0)))
    interval = "5m" if hours <= 8 else "15m"
    time_to = int(now.timestamp())
    time_from = time_to - int(hours * 3600)
    params = {
        "address": candidate.token.mint,
        "type": interval,
        "time_from": time_from,
        "time_to": time_to,
        "currency": "usd",
        "mode": "range",
        "padding": "false",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{base_url}/defi/v3/ohlcv",
                params=params,
                headers={"X-API-KEY": key, "x-chain": "solana"},
            )
        if response.status_code >= 400:
            log.warning("pulse_chart_ohlcv_failed mint=%s status=%s", candidate.token.mint, response.status_code)
            return []
        payload = response.json()
        items = ((payload.get("data") or {}).get("items") or []) if isinstance(payload, dict) else []
    except Exception as exc:
        log.warning("pulse_chart_ohlcv_failed mint=%s error=%s", candidate.token.mint, exc)
        return []

    parsed: list[dict[str, float]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            parsed.append({
                "o": float(item["o"]),
                "h": float(item["h"]),
                "l": float(item["l"]),
                "c": float(item["c"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return parsed[-80:]


def _draw_real_chart(draw: Any, candles: list[dict[str, float]], box: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = box
    if len(candles) < 2:
        return
    lows = [c["l"] for c in candles]
    highs = [c["h"] for c in candles]
    lo, hi = min(lows), max(highs)
    if hi <= lo:
        return
    width = right - left
    height = bottom - top

    def y_for(value: float) -> float:
        return bottom - ((value - lo) / (hi - lo)) * height

    step = width / max(1, len(candles) - 1)
    points: list[tuple[float, float]] = []
    for index, candle in enumerate(candles):
        x = left + index * step
        open_y = y_for(candle["o"])
        close_y = y_for(candle["c"])
        high_y = y_for(candle["h"])
        low_y = y_for(candle["l"])
        up = candle["c"] >= candle["o"]
        color = "#17F28A" if up else "#FF6A3D"
        draw.line((x, high_y, x, low_y), fill=color, width=1)
        if index % max(1, len(candles) // 26) == 0:
            draw.rectangle((x - 2, min(open_y, close_y), x + 2, max(open_y, close_y) + 1), fill=color)
        points.append((x, close_y))
    for start, end in zip(points, points[1:]):
        draw.line((start, end), fill="#18EA83", width=3)


async def _render_locked_fomo_template(
    candidate: Candidate,
    passes: list[dict[str, Any]],
    settings: Settings,
    template: Path,
    path: Path,
    now: datetime,
) -> Path:
    from PIL import Image, ImageDraw, ImageFilter, ImageOps

    width, height = 960, 1200
    canvas = ImageOps.fit(Image.open(template).convert("RGBA"), (width, height))
    draw = ImageDraw.Draw(canvas)

    # Replace the large mutable headline zone while preserving the original fomo
    # mark and right-side art from the source reference.
    clean = Image.new("RGBA", (710, 590), (0, 0, 0, 0))
    clean_draw = ImageDraw.Draw(clean)
    for y in range(clean.height):
        t = y / clean.height
        rgb = (
            int(88 * (1 - t) + 132 * t),
            int(78 * (1 - t) + 96 * t),
            255,
        )
        for x in range(clean.width):
            fade = 1.0 if x < 560 else max(0.0, 1.0 - ((x - 560) / 150))
            clean_draw.point((x, y), fill=rgb + (int(255 * fade),))
    clean = clean.filter(ImageFilter.GaussianBlur(0.4))
    canvas.alpha_composite(clean, (0, 116))
    draw = ImageDraw.Draw(canvas)

    start = money(_derived_start_market_cap(candidate))
    end = money(candidate.token.market_cap)
    ticker = _fit(candidate.token.symbol.upper(), 12)
    line_one = f"{start} TO"
    line_three = f"ON {ticker}"
    draw.text((30, 125), line_one, fill=(18, 14, 70, 55), font=_fit_display_font(line_one, 118, 585, min_size=62))
    draw.text((28, 120), line_one, fill="#FFFFFF", font=_fit_display_font(line_one, 118, 585, min_size=62))
    draw.text((30, 283), end, fill=(12, 12, 85, 58), font=_fit_display_font(end, 196, 610, min_size=90))
    draw.text((28, 276), end, fill="#1520E8", font=_fit_display_font(end, 196, 610, min_size=90))
    draw.text((34, 520), line_three, fill=(18, 14, 70, 58), font=_fit_display_font(line_three, 82, 390, min_size=38))
    draw.text((32, 516), line_three, fill="#FFFFFF", font=_fit_display_font(line_three, 82, 390, min_size=38))

    # Token medallion replaces the reference coin face.
    coin_cover = Image.new("RGBA", (310, 310), (0, 0, 0, 0))
    coin_draw = ImageDraw.Draw(coin_cover)
    coin_draw.ellipse((0, 0, 309, 309), fill=(94, 80, 255, 175), outline=(255, 255, 255, 220), width=8)
    coin_cover = coin_cover.filter(ImageFilter.GaussianBlur(0.7))
    canvas.alpha_composite(coin_cover, (54, 752))
    logo = await _logo(candidate, 244)
    _draw_coin_medallion(canvas, logo, 86, 784, 244)
    draw = ImageDraw.Draw(canvas)

    # Replace the tilted app screenshot area with a clean measured mini-card.
    card_x, card_y, card_w, card_h = 396, 584, 528, 536
    draw.rounded_rectangle((card_x, card_y, card_x + card_w, card_y + card_h), radius=28, fill="#080A16", outline="#7E76FF", width=3)
    draw.rounded_rectangle((card_x + 24, card_y + 24, card_x + 130, card_y + 60), radius=18, fill="#20254A")
    draw.text((card_x + 42, card_y + 31), "Open", fill="#96A4FF", font=_font(18, True))
    draw.rounded_rectangle((card_x + card_w - 124, card_y + 22, card_x + card_w - 26, card_y + 64), radius=12, fill="#5266FF")
    draw.text((card_x + card_w - 104, card_y + 34), "Follow", fill="#FFFFFF", font=_font(18, True))
    draw.text((card_x + 34, card_y + 84), f"${ticker}", fill="#FFFFFF", font=_font(30, True))
    draw.text((card_x + 34, card_y + 119), "runner signal", fill="#AEB4D3", font=_font(18))
    draw.text((card_x + card_w - 162, card_y + 82), money(candidate.token.market_cap), fill="#FFFFFF", font=_font(25, True))
    draw.text((card_x + card_w - 162, card_y + 112), "Market cap", fill="#C9CDE1", font=_font(18))

    chart_left, chart_top = card_x + 36, card_y + 166
    chart_box = (chart_left, chart_top + 4, chart_left + 462, chart_top + 122)
    candles = await _ohlcv_items(candidate, settings, now)
    _draw_real_chart(draw, candles, chart_box)
    if not candles:
        draw.text((chart_left + 116, chart_top + 50), "chart unavailable", fill="#6F7696", font=_font(18, True))

    pnl = candidate.token.market_cap - _derived_start_market_cap(candidate)
    draw.rounded_rectangle((card_x + 24, card_y + 306, card_x + card_w - 24, card_y + 392), radius=18, fill="#111421", outline="#262C44", width=1)
    draw.text((card_x + 42, card_y + 326), money(pnl), fill="#26F28B", font=_font(32, True))
    draw.text((card_x + 42, card_y + 363), f"{candidate.run_multiple:.1f}x runner", fill="#DADDEE", font=_font(18))
    draw.text((card_x + 292, card_y + 326), f"{len(passes)} passes", fill="#FFFFFF", font=_font(23, True))
    draw.text((card_x + 292, card_y + 363), f"{pct(candidate.token.price_change_1h, 0)} 1h", fill="#DADDEE", font=_font(18))

    status = "Fading" if candidate.token.price_change_1h < 0 else "Running"
    kol_count = len(candidate.kol_buyers)
    flags = "; ".join(candidate.risk_labels[:2]) if candidate.risk_labels else "no displayed flags"
    wallets = ", ".join(candidate.kol_buyers[:5]) if candidate.kol_buyers else "tracked wallets empty"
    draw.rounded_rectangle((card_x + 24, card_y + 406, card_x + card_w - 24, card_y + 492), radius=16, fill="#0D1021", outline="#272E49", width=1)
    draw.text((card_x + 42, card_y + 416), f"{status}  ·  {kol_count} KOL  ·  {money(candidate.token.volume_24h)} vol", fill="#FFFFFF", font=_font(20, True))
    draw.text((card_x + 42, card_y + 442), _fit(flags, 55), fill="#BFC4DC", font=_font(15))
    draw.text((card_x + 42, card_y + 466), _fit(wallets, 50), fill="#BFC4DC", font=_font(15))

    # Final readability pass: redraw the evidence block and footer using a
    # clean UI font so the tiny metadata is legible on X compression.
    draw.rounded_rectangle((card_x + 24, card_y + 406, card_x + card_w - 24, card_y + 492), radius=16, fill="#0D1021", outline="#272E49", width=1)
    draw.text((card_x + 42, card_y + 416), f"{status}  /  {kol_count} KOL  /  {money(candidate.token.volume_24h)} vol", fill="#FFFFFF", font=_font(20, True))
    draw.text((card_x + 42, card_y + 442), _fit(flags, 55), fill="#BFC4DC", font=_font(15))
    draw.text((card_x + 42, card_y + 466), _fit(wallets, 50), fill="#BFC4DC", font=_font(15))

    footer = (28, 1118, 932, 1186)
    draw.rounded_rectangle(footer, radius=18, fill=(8, 10, 30, 218), outline=(255, 255, 255, 78), width=1)
    draw.text((48, 1130), "TRACKED WALLETS", fill="#AEB6E9", font=_font(13, True))
    draw.text((48, 1150), _fit(wallets, 46), fill="#FFFFFF", font=_font(18, True))
    draw.text((386, 1130), "CONTRACT", fill="#AEB6E9", font=_font(13, True))
    draw.text((386, 1151), _fit(candidate.token.mint, 42), fill="#FFFFFF", font=_font(16))
    draw.text((778, 1130), "UPDATED", fill="#AEB6E9", font=_font(13, True))
    draw.text((778, 1151), now.strftime("%d %b %H:%M"), fill="#FFFFFF", font=_font(16, True))
    canvas.convert("RGB").save(path, format="PNG", optimize=True)
    return path


async def render_signal_image(
    candidate: Candidate,
    passes: list[dict[str, Any]],
    settings: Settings,
    now: datetime,
    *,
    background: bytes | None = None,
) -> Path:
    from PIL import Image, ImageDraw, ImageOps

    raw_dir = str(settings.get("pulse", "image_dir", "output/pulse-images"))
    image_dir = Path(raw_dir)
    if not image_dir.is_absolute():
        image_dir = settings.root / image_dir
    image_dir.mkdir(parents=True, exist_ok=True)
    symbol = _safe_filename(candidate.token.symbol.strip("$"))
    path = image_dir / f"{symbol}-{candidate.token.mint[:8]}-{int(now.timestamp())}.png"

    template_raw = str(settings.get("pulse", "image_template_path", "") or "")
    template = Path(template_raw) if template_raw else None
    if template and not template.is_absolute():
        template = settings.root / template
    if template and template.exists() and background is None:
        return await _render_locked_fomo_template(candidate, passes, settings, template, path, now)

    width, height = 960, 1200
    using_ai_background = bool(background)
    if background:
        ai_canvas = ImageOps.fit(Image.open(BytesIO(background)).convert("RGB"), (width, height)).convert("RGBA")
        canvas = Image.blend(_reference_gradient(width, height), ai_canvas, 0.38)
    elif template and template.exists():
        canvas = Image.open(template).convert("RGB").resize((width, height)).convert("RGBA")
    else:
        canvas = _reference_gradient(width, height)
    draw = ImageDraw.Draw(canvas)

    _draw_reference_background(canvas, ai=using_ai_background, template=bool(template and template.exists()))
    draw = ImageDraw.Draw(canvas)
    _draw_fomo_lockup(draw, 34, 34)

    start = money(_derived_start_market_cap(candidate)).replace("$", "$")
    end = money(candidate.token.market_cap).replace("$", "$")
    line_one = f"{start} TO"
    line_two = end
    line_three = f"ON {_fit(candidate.token.symbol.upper(), 12)}"
    draw.text((30, 130), line_one, fill="#FFFFFF", font=_fit_font(line_one, 108, 650, bold=True, min_size=58))
    draw.text((28, 274), line_two, fill="#1417DD", font=_fit_font(line_two, 190, 640, bold=True, min_size=82))
    draw.text((34, 512), line_three, fill="#FFFFFF", font=_fit_font(line_three, 82, 620, bold=True, min_size=48))

    chip_y = 624
    chips = [
        (f"{candidate.run_multiple:.1f}x", "24h"),
        (pct(candidate.token.price_change_1h, 0), "1h"),
        (money(candidate.token.volume_24h), "vol"),
        (money(candidate.token.liquidity_usd), "liq"),
    ]
    for index, (value, label) in enumerate(chips):
        x = 34 + (index % 2) * 188
        y = chip_y + (index // 2) * 70
        draw.rounded_rectangle((x, y, x + 164, y + 54), radius=16, fill=(10, 13, 38, 205), outline=(255, 255, 255, 130), width=1)
        draw.text((x + 14, y + 7), value, fill="#FFFFFF", font=_font(24, True))
        draw.text((x + 16, y + 32), label, fill="#E6E3FF", font=_font(15))

    logo = await _logo(candidate, 214)
    _draw_coin_medallion(canvas, logo, 76, 824, 214)
    draw = ImageDraw.Draw(canvas)
    _draw_mock_trading_card(draw, candidate, passes, 410, 645)

    summary_box = (410, 1048, 910, 1140)
    draw.rounded_rectangle(summary_box, radius=20, fill=(9, 11, 24, 218), outline=(255, 255, 255, 70), width=1)
    status = "Fading" if candidate.token.price_change_1h < 0 else "Running"
    kol_count = len(candidate.kol_buyers)
    draw.text((432, 1062), f"{status}  +  {kol_count} KOL  +  {len(passes)} passes", fill="#FFFFFF", font=_font(24, True))
    flags = "; ".join(candidate.risk_labels[:2]) if candidate.risk_labels else "no displayed flags"
    wallets = ", ".join(candidate.kol_buyers[:5]) if candidate.kol_buyers else "tracked wallets empty"
    draw.text((432, 1096), _fit(flags, 48), fill="#DADDF4", font=_font(18))
    draw.text((432, 1118), _fit(wallets, 48), fill="#DADDF4", font=_font(18))

    draw.text((34, 1150), f"CA: {candidate.token.mint}", fill="#FFFFFF", font=_font(17))
    draw.text((765, 1150), now.strftime("%d %b %H:%M"), fill="#FFFFFF", font=_font(17))
    canvas.convert("RGB").save(path, format="PNG", optimize=True)
    return path


async def render_openai_signal_image(
    candidate: Candidate,
    passes: list[dict[str, Any]],
    settings: Settings,
    now: datetime,
) -> Path:
    background = await generate_runner_background(candidate, passes, settings)
    return await render_signal_image(candidate, passes, settings, now, background=background)


def post_text(candidate: Candidate, passes: list[dict[str, Any]], settings: Settings) -> str:
    return "\n".join([
        f"${candidate.token.symbol} passed the runner screen {len(passes)} times in {settings.get('pulse', 'window_hours', 12)}h.",
        f"{money(candidate.token.market_cap)} mcap | {money(candidate.token.volume_24h)} vol | {money(candidate.token.liquidity_usd)} liq | {candidate.run_multiple:.1f}x",
        f"CA: {candidate.token.mint}",
        "Data, not advice.",
    ])[:275]


async def run_pulse(settings: Settings, ledger: Ledger, *, now: datetime | None = None) -> PulseResult:
    timezone_name = str(settings.get("run", "timezone", "UTC"))
    now = now or datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name))
    pulse_settings = _pulse_settings(settings)
    helius_key = None
    if bool(settings.get("pulse", "disable_helius_enrichment", True)):
        helius_key = os.environ.pop("HELIUS_API_KEY", None)
    try:
        brief = await build_brief(pulse_settings, ledger, commit=False, now=now)
    finally:
        if helius_key is not None:
            os.environ["HELIUS_API_KEY"] = helius_key

    latest_written = None
    if bool(settings.get("pulse", "write_latest_json", True)) and settings.get("run", "json_path"):
        latest_written = settings.path("run", "json_path")
        write_html(latest_written, json.dumps(build_payload(brief, settings), indent=1))

    state_path = _state_path(settings)
    state = load_state(state_path)
    manually_excluded_mints = {
        str(mint).strip()
        for mint in (settings.get("journal", "excluded_mints", []) or [])
        if str(mint).strip()
    }
    if manually_excluded_mints:
        brief.runners = [
            candidate for candidate in brief.runners
            if candidate.token.mint not in manually_excluded_mints
        ]
        state_passes = state.setdefault("passes", {})
        state_posted = state.setdefault("posted", {})
        for mint in manually_excluded_mints:
            state_passes.pop(mint, None)
            state_posted.pop(mint, None)
    allowed_chains = {
        str(chain).strip().lower()
        for chain in (pulse_settings.get("thresholds", "chains", ["solana"]) or ["solana"])
        if str(chain).strip()
    }
    if allowed_chains:
        brief.runners = [
            candidate for candidate in brief.runners
            if candidate.token.chain_id.lower() in allowed_chains
        ]
        passes = state.setdefault("passes", {})
        active = {candidate.token.mint for candidate in brief.runners}
        for mint, entries in list(passes.items()):
            known_chains = {
                str(entry.get("chain", "")).lower()
                for entry in entries
                if isinstance(entry, dict) and entry.get("chain")
            }
            if mint not in active and (mint.startswith("0x") or (known_chains and not known_chains <= allowed_chains)):
                passes.pop(mint, None)
    triggered = record_runner_passes(
        state,
        brief.runners,
        now,
        window_hours=float(settings.get("pulse", "window_hours", 12.0)),
        required_passes=int(settings.get("pulse", "required_passes", 3)),
        repost_after_hours=float(settings.get("pulse", "repost_after_hours", 72.0)),
        min_gap_minutes=float(settings.get("pulse", "min_gap_minutes", 45.0)),
    )

    triggers: list[PulseTrigger] = []
    telegram_lines: list[str] = []
    for candidate, passes in triggered[: int(settings.get("pulse", "max_posts_per_run", 3))]:
        trigger = PulseTrigger(candidate=candidate, passes=passes)
        try:
            if bool(settings.get("pulse", "openai_image_enabled", True)) and openai_image_configured():
                try:
                    trigger.image_path = await render_openai_signal_image(candidate, passes, settings, now)
                except OpenAIImageError as exc:
                    log.warning("pulse_openai_image_failed mint=%s error=%s", candidate.token.mint, exc)
                    trigger.image_path = await render_signal_image(candidate, passes, settings, now)
                    trigger.error = f"OpenAI image failed; fallback poster generated: {exc}"
            else:
                trigger.image_path = await render_signal_image(candidate, passes, settings, now)
            if bool(settings.get("pulse", "x_post_enabled", True)):
                if x_posting_configured():
                    trigger.x_post_id = await post_image(settings, post_text(candidate, passes, settings), trigger.image_path)
                else:
                    trigger.error = trigger.error or "X posting credentials unset; image generated but X post skipped"
        except (OSError, XPostError, httpx.HTTPError) as exc:
            trigger.error = str(exc)
            log.warning("pulse_trigger_failed mint=%s error=%s", candidate.token.mint, exc)
        triggers.append(trigger)
        telegram_lines.append(
            f"${candidate.token.symbol} passed {len(passes)} times in {settings.get('pulse', 'window_hours', 12)}h - "
            f"{candidate.run_multiple:.1f}x, {money(candidate.token.market_cap)} mcap"
        )
    save_state(state_path, state)

    if telegram_lines and bool(settings.get("pulse", "telegram_enabled", True)):
        try:
            await send_telegram(["RUNNER PULSE\n" + "\n".join(telegram_lines)])
        except TelegramDeliveryError as exc:
            log.warning("pulse_telegram_failed error=%s", exc)
    return PulseResult(len(brief.runners), triggers, state_path, latest_written)
