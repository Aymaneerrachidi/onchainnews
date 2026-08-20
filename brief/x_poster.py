from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from brief.config import Settings


class XPostError(RuntimeError):
    pass


def configured() -> bool:
    import os

    return bool(os.getenv("X_USER_ACCESS_TOKEN"))


async def upload_image(settings: Settings, image_path: Path, *, transport=None) -> str:
    """Upload a generated PNG/JPEG to X and return the media id string."""
    import os

    token = os.getenv("X_USER_ACCESS_TOKEN")
    if not token:
        raise XPostError("X_USER_ACCESS_TOKEN is required for posting images")
    timeout = float(settings.get("pulse", "x_timeout_seconds", 30.0))
    url = str(settings.get("pulse", "x_media_upload_url", "https://api.x.com/2/media/upload"))
    content_type = "image/jpeg" if image_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        with image_path.open("rb") as handle:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                data={"media_category": "tweet_image"},
                files={"media": (image_path.name, handle, content_type)},
            )
    if response.status_code >= 400:
        raise XPostError(f"X media upload HTTP {response.status_code}: {response.text[:240]}")
    payload = response.json()
    data: dict[str, Any] = payload.get("data") or payload
    media_id = data.get("id") or data.get("media_id_string") or data.get("media_id")
    if not media_id:
        raise XPostError("X media upload did not return a media id")
    return str(media_id)


async def create_post(settings: Settings, text: str, media_id: str | None = None, *, transport=None) -> str:
    import os

    token = os.getenv("X_USER_ACCESS_TOKEN")
    if not token:
        raise XPostError("X_USER_ACCESS_TOKEN is required for posting")
    timeout = float(settings.get("pulse", "x_timeout_seconds", 30.0))
    url = str(settings.get("pulse", "x_create_post_url", "https://api.x.com/2/tweets"))
    body: dict[str, Any] = {"text": text}
    if media_id:
        body["media"] = {"media_ids": [media_id]}
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
        )
    if response.status_code >= 400:
        raise XPostError(f"X post HTTP {response.status_code}: {response.text[:240]}")
    payload = response.json()
    post_id = ((payload.get("data") or {}).get("id") if isinstance(payload, dict) else None)
    if not post_id:
        raise XPostError("X post response did not return an id")
    return str(post_id)


async def post_image(settings: Settings, text: str, image_path: Path, *, transport=None) -> str:
    media_id = await upload_image(settings, image_path, transport=transport)
    return await create_post(settings, text, media_id, transport=transport)
