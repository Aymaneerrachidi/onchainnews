from __future__ import annotations

from brief.x_poster import configured, oauth1_header


def test_oauth1_posting_credentials_are_detected(monkeypatch):
    monkeypatch.setenv("X_API_KEY", "key")
    monkeypatch.setenv("X_API_SECRET", "secret")
    monkeypatch.setenv("X_ACCESS_TOKEN", "token")
    monkeypatch.setenv("X_ACCESS_TOKEN_SECRET", "token-secret")

    assert configured()


def test_oauth1_header_contains_signed_user_context(monkeypatch):
    monkeypatch.setenv("X_API_KEY", "key")
    monkeypatch.setenv("X_API_SECRET", "secret")
    monkeypatch.setenv("X_ACCESS_TOKEN", "token")
    monkeypatch.setenv("X_ACCESS_TOKEN_SECRET", "token-secret")

    header = oauth1_header("POST", "https://api.x.com/2/tweets")

    assert header.startswith("OAuth ")
    assert 'oauth_consumer_key="key"' in header
    assert 'oauth_token="token"' in header
    assert "oauth_signature=" in header
