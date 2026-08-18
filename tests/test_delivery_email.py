from __future__ import annotations

import json

import httpx
import pytest

from brief.delivery import EmailDeliveryError, send_email
from tests.conftest import build_settings


def _email_settings(tmp_path):
    return build_settings(
        tmp_path,
        extra=(
            '\nemail_from = "onboarding@resend.dev"\n'
            'email_to = ["me@example.com", "you@example.com"]\n'
            'email_subject_prefix = "Test Brief"\n'
        ),
    )


@pytest.mark.asyncio
async def test_missing_key_raises(tmp_path, monkeypatch):
    settings = _email_settings(tmp_path)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with pytest.raises(EmailDeliveryError, match="RESEND_API_KEY"):
        await send_email(settings, "subject", "<p>body</p>")


@pytest.mark.asyncio
async def test_missing_recipients_raise(tmp_path, monkeypatch):
    settings = build_settings(tmp_path)  # no email_from/email_to in [delivery]
    monkeypatch.setenv("RESEND_API_KEY", "re_test_abc")
    with pytest.raises(EmailDeliveryError, match="email_from|email_to"):
        await send_email(settings, "subject", "<p>body</p>")


@pytest.mark.asyncio
async def test_success_posts_one_request_per_recipient_with_bearer_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_abc")
    settings = _email_settings(tmp_path)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "headers": request.headers, "body": request.read().decode()})
        return httpx.Response(200, json={"id": f"msg-{len(calls)}"})

    transport = httpx.MockTransport(handler)
    count = await send_email(settings, "Test Brief — 06 Aug 2026", "<p>body</p>", transport=transport)

    assert count == 2
    assert len(calls) == 2
    for call in calls:
        assert call["url"] == "https://api.resend.com/emails"
        assert call["headers"]["authorization"] == "Bearer re_test_abc"
        assert json.loads(call["body"])["from"] == "onboarding@resend.dev"
        assert json.loads(call["body"])["subject"] == "Test Brief — 06 Aug 2026"
        assert json.loads(call["body"])["html"] == "<p>body</p>"
    assert [json.loads(call["body"])["to"] for call in calls] == [["me@example.com"], ["you@example.com"]]


@pytest.mark.asyncio
async def test_http_error_raises_and_aborts(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_abc")
    settings = _email_settings(tmp_path)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": {"type": "rate_limited"}})

    transport = httpx.MockTransport(handler)
    with pytest.raises(EmailDeliveryError, match="429"):
        await send_email(settings, "subject", "<p>body</p>", transport=transport)
    assert calls == 1, "failure on the first recipient must abort the rest"