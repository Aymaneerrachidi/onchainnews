from __future__ import annotations

import os
from pathlib import Path

import httpx

from brief.config import Settings


class TelegramDeliveryError(RuntimeError):
    pass


class EmailDeliveryError(RuntimeError):
    pass


async def send_telegram(messages: list[str]) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise TelegramDeliveryError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        for message in messages:
            response = await client.post(
                url,
                json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            )
            if response.status_code >= 400:
                raise TelegramDeliveryError(f"Telegram HTTP {response.status_code}: {response.text[:200]}")


async def send_email(settings: Settings, subject: str, html: str, *, transport=None) -> int:
    """Send the rendered report to every address in [delivery].email_to.

    Supports Resend and Brevo. A mid-loop failure aborts the remaining
    recipients and propagates as EmailDeliveryError.
    """
    provider = str(settings.get("delivery", "email_provider", "resend") or "resend").strip().lower()
    sender = str(settings.get("delivery", "email_from", "") or "")
    recipients = list(settings.get("delivery", "email_to", []) or [])
    if not sender or not recipients:
        raise EmailDeliveryError("delivery.email_from and delivery.email_to must be configured")
    if provider == "brevo":
        return await _send_brevo(settings, sender, recipients, subject, html, transport=transport)
    if provider == "resend":
        return await _send_resend(settings, sender, recipients, subject, html, transport=transport)
    raise EmailDeliveryError(f"Unsupported delivery.email_provider: {provider}")


async def _send_resend(
    settings: Settings,
    sender: str,
    recipients: list[str],
    subject: str,
    html: str,
    *,
    transport=None,
) -> int:
    key = os.getenv("RESEND_API_KEY")
    if not key:
        raise EmailDeliveryError("RESEND_API_KEY is required")
    url = "https://api.resend.com/emails"
    timeout = float(settings.get("delivery", "email_timeout_seconds", 15.0))
    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for recipient in recipients:
            response = await client.post(
                url,
                headers=headers,
                json={"from": sender, "to": [recipient], "subject": subject, "html": html},
            )
            if response.status_code >= 400:
                raise EmailDeliveryError(f"Resend HTTP {response.status_code}: {response.text[:200]}")
    return len(recipients)


async def _send_brevo(
    settings: Settings,
    sender: str,
    recipients: list[str],
    subject: str,
    html: str,
    *,
    transport=None,
) -> int:
    key = os.getenv("BREVO_API_KEY")
    if not key:
        raise EmailDeliveryError("BREVO_API_KEY is required")
    sender_name = str(settings.get("delivery", "email_from_name", "fomo onchain") or "fomo onchain")
    url = str(settings.get("delivery", "brevo_send_url", "https://api.brevo.com/v3/smtp/email"))
    timeout = float(settings.get("delivery", "email_timeout_seconds", 15.0))
    headers = {"api-key": key, "Content-Type": "application/json"}
    payload = {
        "sender": {"name": sender_name, "email": sender},
        "to": [{"email": recipient} for recipient in recipients],
        "subject": subject,
        "htmlContent": html,
    }
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        response = await client.post(url, headers=headers, json=payload)
    if response.status_code >= 400:
        raise EmailDeliveryError(f"Brevo HTTP {response.status_code}: {response.text[:200]}")
    return len(recipients)


def write_html(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
