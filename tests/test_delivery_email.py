from __future__ import annotations

import json

import httpx
import pytest

from brief.delivery import EmailDeliveryError, send_email
from tests.conftest import build_settings


def test_shipped_test_recipient_is_only_ue06prog():
    import tomllib
    from pathlib import Path

    config = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))
    assert config["delivery"]["email_to"] == ["ue06prog@gmail.com"]


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


@pytest.mark.asyncio
async def test_brevo_success_posts_one_request_for_all_recipients(tmp_path, monkeypatch):
    monkeypatch.setenv("BREVO_API_KEY", "xkeysib-test")
    settings = build_settings(
        tmp_path,
        extra=(
            '\nemail_provider = "brevo"\n'
            'email_from = "brief@example.com"\n'
            'email_from_name = "fomo onchain"\n'
            'email_to = ["me@example.com", "you@example.com"]\n'
        ),
    )
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "headers": request.headers, "body": request.read().decode()})
        return httpx.Response(201, json={"messageId": "brevo-msg-1"})

    count = await send_email(settings, "subject", "<p>body</p>", transport=httpx.MockTransport(handler))

    assert count == 2
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://api.brevo.com/v3/smtp/email"
    assert call["headers"]["api-key"] == "xkeysib-test"
    body = json.loads(call["body"])
    assert body["sender"] == {"name": "fomo onchain", "email": "brief@example.com"}
    assert body["to"] == [{"email": "me@example.com"}, {"email": "you@example.com"}]
    assert body["htmlContent"] == "<p>body</p>"


@pytest.mark.asyncio
async def test_brevo_missing_key_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    settings = build_settings(
        tmp_path,
        extra=(
            '\nemail_provider = "brevo"\n'
            'email_from = "brief@example.com"\n'
            'email_to = ["me@example.com"]\n'
        ),
    )

    with pytest.raises(EmailDeliveryError, match="BREVO_API_KEY"):
        await send_email(settings, "subject", "<p>body</p>")
