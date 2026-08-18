# Newsletter Email Delivery — Design

Date: 2026-08-18
Status: Approved by operator (design parts 1 & 2)

## Purpose

Deliver the daily Solana Brief report by email as a fourth delivery channel,
alongside the HTML file, the web snapshot, and the Telegram digest. Phase 1 is
private: the report is emailed to a fixed recipient list in config. A public
subscribe flow (form, audiences, archive page) is explicitly out of scope and
follows in a later phase.

## Approach

Email is a channel inside the existing daily `run` command, mirroring the
Telegram pattern: config-gated, credential-gated, warn-and-skip on missing
credentials, never fails the run, never blocks the rendered report or web
publishing.

Provider: Resend (REST API, free tier 3,000 emails/month). Testing sender
`onboarding@resend.dev` until a domain is verified; then `brief@<domain>`.

## Components

### 1. Config (`config.toml`, `[delivery]` section)

New keys, all additive — nothing existing changes:

```toml
email_enabled = false
# Resend testing sender; swap for brief@yourdomain.com once a domain is verified.
email_from = "onboarding@resend.dev"
email_to = []
email_subject_prefix = "Solana Brief"
```

`.env.example` gains `RESEND_API_KEY=` with a comment (free tier, create at
resend.com/api-keys). Missing key is not an error; email delivery is skipped
with a warning.

### 2. Email renderer (`brief/render/email.py`, new file)

`render_email(brief: Brief, settings: Settings) -> str` — pure function,
`Brief` in, standalone HTML email string out. No I/O. Unit-testable without
network.

- Flat, light-theme, inline-styled HTML (email-client-safe: no `<details>`,
  no `<summary>`, no external stylesheet, no dark background).
- Renders the same `Brief` model the site snapshot renders from, so the email
  can never disagree with the site.
- Content: date header, the read / picks (anchored to `render_html`'s
  `picks = [*new_and_moving, *movers, *ctos]` so the email and site can never
  disagree by construction), each candidate's dossier always-expanded (badges,
  read line, metrics grid, warnings), the journal table, footer with
  `report_url` link and a "not financial advice" line.
- Subject built as `"{prefix} — {day} {Mon} {YYYY}"` (e.g. `Solana Brief — 18
  Aug 2026`).

### 3. Delivery (`brief/delivery.py`, extend)

```python
class EmailDeliveryError(RuntimeError): ...

async def send_email(settings, subject: str, html: str) -> None
```

- Reads `RESEND_API_KEY` from env and `email_from`/`email_to` from
  `settings.get("delivery", ...)`; missing key → `EmailDeliveryError`.
- `POST https://api.resend.com/emails` via httpx (existing dependency) with
  `timeout=15`, body `{"from": <email_from>, "to": [...], "subject": ...,
  "html": ...}`, `Authorization: Bearer <key>`.
- Status >= 400 → raise `EmailDeliveryError` with response body excerpt
  (mirrors `send_telegram`).
- `send_email` loops internally over `email_to`, one API call per recipient
  (private use; no batch API complexity in phase 1). A mid-loop failure
  aborts the remaining recipients and propagates as `EmailDeliveryError`.

### 4. Run flow (`brief/main.py`, `run()`)

New block after the Telegram block, same guard pattern:

1. `email_enabled` false → dim note, skip.
2. `email_enabled` true but `RESEND_API_KEY` missing or `email_to` empty →
   yellow warn, skip, run still succeeds (missing credential never loses the
   rendered report).
3. Otherwise: `render_email` → `send_email` per recipient → print
   `Email digest delivered (N recipient(s)).`
4. On `EmailDeliveryError`: log, yellow warn, exit code stays 0; web
   publishing unaffected.

Order inside `run`: HTML write → JSON snapshot → Telegram → email. All
independent.

## Testing

New test files, following existing `tests/` conventions and fixtures:

- `test_render_email.py` — render from a fixture brief: contains date header,
  picks, expanded dossiers, no `<details>`/`<summary>`, footer link, escaped
  HTML, correct subject format.
- `test_delivery_email.py` — httpx `MockTransport` (ships with httpx; no new
  dependency): success posts to `api.resend.com/emails` with bearer auth; HTTP
  4xx raises `EmailDeliveryError`; missing env key raises. Must
  `monkeypatch.delenv("RESEND_API_KEY")` itself — the conftest autouse
  fixture only clears the Helius/Birdeye/X keys.

## Documentation

- README: "Email delivery" paragraph under Delivery and scheduling — setup,
  testing-sender caveat, when to add a verified domain.
- `.env.example`: `RESEND_API_KEY=` entry.

## Out of scope (phase 2)

- Public subscribe form on `web/`
- Resend audiences / subscriber management
- Archive page / issue permalinks
- Multi-tenant or dynamic recipient lists
