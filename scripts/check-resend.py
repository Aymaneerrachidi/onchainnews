from __future__ import annotations

"""Ad-hoc: list recent Resend sends and their delivery events."""

import os
from pathlib import Path

import httpx

# Load RESEND_API_KEY from .env without clobbering an existing env value.
for raw in Path(__file__).resolve().parents[1].joinpath(".env").read_text().splitlines():
    line = raw.strip()
    if line.startswith("RESEND_API_KEY="):
        os.environ.setdefault("RESEND_API_KEY", line.split("=", 1)[1].strip().strip('"').strip("'"))

key = os.environ.get("RESEND_API_KEY", "")
headers = {"Authorization": f"Bearer {key}"}

r = httpx.get("https://api.resend.com/emails", headers=headers, timeout=20)
print("list endpoint HTTP", r.status_code)
if r.status_code != 200:
    print(r.text[:500])
    raise SystemExit(0)

payload = r.json()
items = payload if isinstance(payload, list) else (payload.get("data") or [])
print(f"{len(items)} emails returned")
for e in items[:8]:
    events = e.get("last_event") or e.get("status")
    to = e.get("to")
    if isinstance(to, list):
        to = ",".join(to)
    print(
        e.get("created_at", "?"),
        "|", to, "|", e.get("subject", "?"), "|", events,
    )
    # Optional: pull full detail via the id endpoint for delivery status.