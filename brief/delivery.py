from __future__ import annotations

import os
from pathlib import Path

import httpx


class TelegramDeliveryError(RuntimeError):
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


def write_html(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)

