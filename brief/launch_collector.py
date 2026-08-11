from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from websockets.asyncio.client import connect

from brief.config import Settings
from brief.ledger import Ledger, iso
from brief.sources.helius import HeliusSource
from brief.sources.http import CachedHttpClient, SourceError


UTC = timezone.utc
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
CREATE_DISCRIMINATORS = {
    hashlib.sha256(f"global:{name}".encode()).digest()[:8]
    for name in ("create", "create_v2")
}
log = logging.getLogger("brief.launch_collector")


def _b58decode(value: str) -> bytes:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = 0
    for character in value:
        number = number * 58 + alphabet.index(character)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(value) - len(value.lstrip("1"))) + raw


def _keys(transaction: dict[str, Any]) -> list[str]:
    message = ((transaction.get("transaction") or {}).get("message") or {})
    keys = [
        str(item.get("pubkey")) if isinstance(item, dict) else str(item)
        for item in message.get("accountKeys") or []
    ]
    loaded = ((transaction.get("meta") or {}).get("loadedAddresses") or {})
    keys.extend(str(item) for item in loaded.get("writable") or [])
    keys.extend(str(item) for item in loaded.get("readonly") or [])
    return keys


def parse_pump_creates(transaction: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Return (mint, creator) for official Pump create/create_v2 instructions."""
    keys = _keys(transaction)
    message = ((transaction.get("transaction") or {}).get("message") or {})
    groups = [message.get("instructions") or []]
    groups.extend(group.get("instructions") or [] for group in (transaction.get("meta") or {}).get("innerInstructions") or [])
    found: list[tuple[str, str | None]] = []
    for instructions in groups:
        for instruction in instructions:
            program = instruction.get("programId")
            if program is None and instruction.get("programIdIndex") is not None:
                index = int(instruction["programIdIndex"])
                program = keys[index] if index < len(keys) else None
            if str(program) != PUMP_PROGRAM:
                continue
            try:
                data = _b58decode(str(instruction.get("data") or ""))
            except (ValueError, IndexError):
                continue
            if data[:8] not in CREATE_DISCRIMINATORS:
                continue
            accounts = instruction.get("accounts") or []
            resolved = [
                keys[item] if isinstance(item, int) else str(item)
                for item in accounts
                if not (isinstance(item, int) and item >= len(keys))
            ]
            if not resolved:
                continue
            mint = resolved[0]
            # The current Pump create variants do not keep the creator at one
            # stable account offset. Preserve it as unknown rather than writing
            # a plausible-looking but incorrect account.
            creator = None
            if mint and all(existing[0] != mint for existing in found):
                found.append((mint, creator))
    return found


def _websocket_url(base_url: str, api_key: str) -> str:
    parts = urlsplit(base_url)
    return urlunsplit(("wss", parts.netloc, parts.path or "/", urlencode({"api-key": api_key}), ""))


def _transaction_signature(transaction: dict[str, Any]) -> str:
    signatures = (transaction.get("transaction") or {}).get("signatures") or []
    return str(signatures[0]) if signatures else ""


def _store_transaction(
    ledger: Ledger,
    transaction: dict[str, Any],
    *,
    signature: str | None = None,
) -> int:
    signature = signature or _transaction_signature(transaction)
    if not signature:
        return 0
    block_time = transaction.get("blockTime")
    created_at = datetime.fromtimestamp(float(block_time), UTC) if block_time else datetime.now(UTC)
    slot = int(transaction.get("slot")) if transaction.get("slot") is not None else None
    captured = 0
    for mint, creator in parse_pump_creates(transaction):
        if ledger.record_launch_event(mint, "pump.fun", signature, creator, created_at, slot):
            captured += 1
    return captured


async def run_launch_collector(
    settings: Settings,
    ledger: Ledger,
    *,
    max_events: int | None = None,
) -> None:
    api_key = os.getenv("HELIUS_API_KEY")
    if not api_key:
        raise SourceError("Helius is not configured")
    now = datetime.now(UTC)
    if not ledger.collector_state("started_at"):
        ledger.set_collector_state("started_at", iso(now))
    http = CachedHttpClient(ledger, timeout=float(settings.get("run", "request_timeout_seconds", 15)))
    helius = HeliusSource(
        http,
        str(settings.get("sources", "helius_base_url", "https://mainnet.helius-rpc.com")),
        api_key,
        ttl=30,
        requests_per_minute=int(settings.get("holders", "helius_requests_per_minute", 100)),
    )
    websocket_url = _websocket_url(helius.base_url, api_key)
    captured = 0
    delay = 1.0
    try:
        while max_events is None or captured < max_events:
            try:
                # Heal a short disconnect and seed a new installation from the
                # most recent 1,000 Pump transactions. The live stream remains
                # the authoritative path for complete forward coverage.
                backfill = await helius.recent_program_transactions(
                    PUMP_PROGRAM,
                    since_unix=int(datetime.now(UTC).timestamp()) - 900,
                    limit=1000,
                    ttl=30,
                )
                for transaction in reversed(backfill):
                    captured += _store_transaction(ledger, transaction)
                    if max_events is not None and captured >= max_events:
                        return
                if backfill:
                    log.info("launch_backfill transactions=%s captured_total=%s", len(backfill), captured)
                async with connect(websocket_url, ping_interval=20, ping_timeout=20, max_size=4_000_000) as socket:
                    await socket.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "confirmed"}],
                    }))
                    acknowledgement = json.loads(await socket.recv())
                    if acknowledgement.get("error"):
                        raise SourceError(f"Helius WebSocket subscription failed: {acknowledgement['error'].get('message', 'error')}")
                    ledger.set_collector_state("connected_at", iso(datetime.now(UTC)))
                    log.info("Pump launch stream connected")
                    delay = 1.0
                    async for raw in socket:
                        message = json.loads(raw)
                        result = (((message.get("params") or {}).get("result") or {}).get("value") or {})
                        logs = result.get("logs") or []
                        if result.get("err") is not None or not any("Instruction: Create" in line for line in logs):
                            continue
                        signature = str(result.get("signature") or "")
                        if not signature:
                            continue
                        transaction = await helius.transaction(signature)
                        if not transaction:
                            continue
                        new_events = _store_transaction(ledger, transaction, signature=signature)
                        captured += new_events
                        if new_events:
                            log.info("launch_captured count=%s total=%s", new_events, captured)
                        if max_events is not None and captured >= max_events:
                            return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("launch_stream_reconnecting error=%s delay=%.0fs", exc.__class__.__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
    finally:
        await http.close()
