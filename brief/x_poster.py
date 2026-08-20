from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from brief.config import Settings


class XPostError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.getenv("X_USER_ACCESS_TOKEN") or _oauth1_credentials())


def _encode(value: object) -> str:
    return quote(str(value), safe="~-._")


def _oauth1_credentials() -> tuple[str, str, str, str] | None:
    values = (
        os.getenv("X_API_KEY"),
        os.getenv("X_API_SECRET"),
        os.getenv("X_ACCESS_TOKEN"),
        os.getenv("X_ACCESS_TOKEN_SECRET"),
    )
    if all(values):
        return tuple(str(value) for value in values)  # type: ignore[return-value]
    return None


def oauth1_header(method: str, url: str, extra_params: dict[str, str] | None = None) -> str:
    """OAuth 1.0a user-context signature for X posting.

    OAuth2 user access tokens expire quickly unless refresh-token rotation is
    managed. The access-token/access-token-secret pair from X's OAuth 1.0a
    "Read and write" app settings is stable enough for scheduled bot posts.
    """
    creds = _oauth1_credentials()
    if not creds:
        raise XPostError("X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN and X_ACCESS_TOKEN_SECRET are required")
    api_key, api_secret, access_token, access_secret = creds
    oauth_params = {
        "oauth_consumer_key": api_key,
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": access_token,
        "oauth_version": "1.0",
    }
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    signature_params = {**oauth_params, **(extra_params or {})}
    parameter_string = "&".join(
        f"{_encode(key)}={_encode(value)}"
        for key, value in sorted(signature_params.items())
    )
    base_string = "&".join([method.upper(), _encode(base_url), _encode(parameter_string)])
    signing_key = f"{_encode(api_secret)}&{_encode(access_secret)}"
    digest = hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    oauth_params["oauth_signature"] = base64.b64encode(digest).decode("ascii")
    return "OAuth " + ", ".join(
        f'{_encode(key)}="{_encode(value)}"'
        for key, value in sorted(oauth_params.items())
    )


async def upload_image(settings: Settings, image_path: Path, *, transport=None) -> str:
    """Upload a generated PNG/JPEG to X and return the media id string."""
    token = os.getenv("X_USER_ACCESS_TOKEN")
    timeout = float(settings.get("pulse", "x_timeout_seconds", 30.0))
    use_oauth1 = not token and _oauth1_credentials()
    url = str(settings.get(
        "pulse",
        "x_media_upload_url",
        "https://upload.twitter.com/1.1/media/upload.json" if use_oauth1 else "https://api.x.com/2/media/upload",
    ))
    content_type = "image/jpeg" if image_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    headers = {"Authorization": f"Bearer {token}"} if token else {"Authorization": oauth1_header("POST", url)}
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        with image_path.open("rb") as handle:
            response = await client.post(
                url,
                headers=headers,
                data={"media_category": "tweet_image" if not use_oauth1 else "TWEET_IMAGE"},
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
    token = os.getenv("X_USER_ACCESS_TOKEN")
    timeout = float(settings.get("pulse", "x_timeout_seconds", 30.0))
    url = str(settings.get("pulse", "x_create_post_url", "https://api.x.com/2/tweets"))
    body: dict[str, Any] = {"text": text}
    if media_id:
        body["media"] = {"media_ids": [media_id]}
    headers = (
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if token else
        {"Authorization": oauth1_header("POST", url), "Content-Type": "application/json"}
    )
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        response = await client.post(
            url,
            headers=headers,
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
