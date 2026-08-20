from __future__ import annotations

import copy
import json
import logging
import os
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
        "change24h": token.price_change_24h,
        "change1h": token.price_change_1h,
        "runMultiple": candidate.run_multiple,
        "holders": candidate.safety.holder_count,
        "kolBuyers": candidate.kol_buyers,
        "riskLabels": candidate.risk_labels,
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
    if bool(pulse.get("disable_x_scan", True)):
        values.setdefault("x", {})["enabled"] = False
    return Settings(root=settings.root, values=values)


def _image_url(candidate: Candidate) -> str:
    info = (candidate.token.raw or {}).get("info") or {}
    return str(info.get("imageUrl") or info.get("image") or "")


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    names = (
        ("C:/Windows/Fonts/calibrib.ttf", "C:/Windows/Fonts/calibri.ttf")
        if bold else
        ("C:/Windows/Fonts/calibri.ttf", "C:/Windows/Fonts/arial.ttf")
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "..."


async def _logo(candidate: Candidate, size: int):
    from PIL import Image, ImageDraw

    url = _image_url(candidate)
    if url:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(url)
            if response.status_code < 400:
                image = Image.open(BytesIO(response.content)).convert("RGBA").resize((size, size))
                mask = Image.new("L", (size, size), 0)
                draw = ImageDraw.Draw(mask)
                draw.ellipse((0, 0, size - 1, size - 1), fill=255)
                image.putalpha(mask)
                return image
        except Exception:
            pass
    image = Image.new("RGBA", (size, size), "#EEF1FF")
    draw = ImageDraw.Draw(image)
    draw.ellipse((0, 0, size - 1, size - 1), fill="#EEF1FF", outline="#D6DAEA", width=2)
    initials = _fit(candidate.token.symbol.strip("$").upper(), 4)
    font = _font(34, True)
    bbox = draw.textbbox((0, 0), initials, font=font)
    draw.text(((size - (bbox[2] - bbox[0])) / 2, (size - (bbox[3] - bbox[1])) / 2 - 4), initials, fill="#17152B", font=font)
    return image


async def render_signal_image(candidate: Candidate, passes: list[dict[str, Any]], settings: Settings, now: datetime) -> Path:
    from PIL import Image, ImageDraw

    raw_dir = str(settings.get("pulse", "image_dir", "output/pulse-images"))
    image_dir = Path(raw_dir)
    if not image_dir.is_absolute():
        image_dir = settings.root / image_dir
    image_dir.mkdir(parents=True, exist_ok=True)
    path = image_dir / f"{candidate.token.symbol}-{candidate.token.mint[:8]}-{int(now.timestamp())}.png"

    template_raw = str(settings.get("pulse", "image_template_path", "") or "")
    template = Path(template_raw) if template_raw else None
    if template and not template.is_absolute():
        template = settings.root / template
    width, height = 960, 1200
    if template and template.exists():
        canvas = Image.open(template).convert("RGB").resize((width, height))
    else:
        canvas = Image.new("RGB", (width, height), "#EEF1FF")
    draw = ImageDraw.Draw(canvas)

    ink = "#17152B"
    muted = "#666A7A"
    blue = "#3657E3"
    line = "#DDE1EC"
    if not (template and template.exists()):
        draw.rectangle((0, 0, width, height), fill="#EEF1FF")
        draw.rectangle((0, 0, width, 470), fill="#4D5BF6")
        draw.rectangle((0, 470, width, height), fill="#F8F9FF")
    draw.rectangle((44, 48, width - 44, height - 48), outline="#FFFFFF", width=3)
    draw.text((76, 74), "fomo onchain", fill="#FFFFFF" if not template else ink, font=_font(34, True))

    logo = await _logo(candidate, 132)
    canvas.paste(logo, (76, 170), logo)
    draw.text((232, 168), f"${candidate.token.symbol}", fill="#FFFFFF" if not template else ink, font=_font(58, True))
    draw.text((236, 236), _fit(candidate.token.name, 34), fill="#DDE3FF" if not template else muted, font=_font(26))
    draw.text((76, 330), f"{candidate.run_multiple:.1f}x", fill="#FFFFFF" if not template else blue, font=_font(118, True))
    draw.text((82, 438), "24h runner, sustained screen pass", fill="#DDE3FF" if not template else muted, font=_font(28))

    stats = [
        ("mcap", money(candidate.token.market_cap)),
        ("vol", money(candidate.token.volume_24h)),
        ("liq", money(candidate.token.liquidity_usd)),
        ("1h", pct(candidate.token.price_change_1h, 0)),
        ("passes", f"{len(passes)} / {settings.get('pulse', 'required_passes', 3)}"),
    ]
    y = 540
    for index, (label, value) in enumerate(stats):
        col = index % 2
        row = index // 2
        x = 76 + col * 410
        yy = y + row * 112
        draw.rounded_rectangle((x, yy, x + 360, yy + 86), radius=14, fill="#FFFFFF", outline=line, width=1)
        draw.text((x + 22, yy + 16), value, fill=ink, font=_font(32, True))
        draw.text((x + 24, yy + 54), label, fill=muted, font=_font(19))

    y = 900
    lines = [candidate.read, *(candidate.dex_evidence[:1] if candidate.dex_evidence else [])]
    if candidate.kol_buyers:
        lines.append(f"Tracked wallets: {', '.join(candidate.kol_buyers[:5])}")
    if candidate.risk_labels:
        lines.append(f"Flags: {'; '.join(candidate.risk_labels[:2])}")
    for index, line in enumerate(lines[:4]):
        draw.text((76, y), _fit(line, 64), fill=ink if index == 0 else muted, font=_font(24 if index == 0 else 21))
        y += 42

    draw.text((76, 1100), f"CA: {candidate.token.mint}", fill=muted, font=_font(18))
    draw.text((730, 1100), now.strftime("%d %b %H:%M"), fill=muted, font=_font(18))
    canvas.save(path, format="PNG", optimize=True)
    return path


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
            trigger.image_path = await render_signal_image(candidate, passes, settings, now)
            if bool(settings.get("pulse", "x_post_enabled", True)):
                if x_posting_configured():
                    trigger.x_post_id = await post_image(settings, post_text(candidate, passes, settings), trigger.image_path)
                else:
                    trigger.error = "X posting credentials unset; image generated but X post skipped"
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
