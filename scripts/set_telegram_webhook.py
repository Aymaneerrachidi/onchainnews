"""Register the hosted Telegram callback endpoint once after deployment."""
from __future__ import annotations

import argparse
import asyncio
import os

import httpx


async def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="full https://.../api/telegram-interactions URL")
    args = parser.parse_args()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if not token or not secret:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET are required")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={
                "url": args.url,
                "secret_token": secret,
                "allowed_updates": ["callback_query"],
                "drop_pending_updates": False,
            },
        )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise SystemExit(f"Telegram rejected webhook registration: {result.get('description', 'unknown error')}")
    print("Telegram callback webhook registered.")


if __name__ == "__main__":
    asyncio.run(run())
