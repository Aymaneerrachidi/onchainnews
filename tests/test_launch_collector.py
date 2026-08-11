from __future__ import annotations

import hashlib

from brief.launch_collector import PUMP_PROGRAM, parse_pump_creates


def _b58encode(value: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + (encoded or "")


def test_parse_only_official_pump_create_instruction() -> None:
    mint = "Mint111111111111111111111111111111111111111"
    discriminator = hashlib.sha256(b"global:create").digest()[:8]
    transaction = {
        "transaction": {
            "message": {
                "accountKeys": [mint, PUMP_PROGRAM, "User111111111111111111111111111111111111111"],
                "instructions": [
                    {"programId": PUMP_PROGRAM, "accounts": [mint, "User111111111111111111111111111111111111111"], "data": _b58encode(discriminator + b"payload")},
                    {"programId": PUMP_PROGRAM, "accounts": [mint], "data": _b58encode(b"notcreate")},
                ],
            }
        },
        "meta": {},
    }
    assert parse_pump_creates(transaction) == [(mint, None)]
