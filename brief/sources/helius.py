from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from brief.models import AcquisitionTrace, Enrichment, HolderBalance, WalletTrace, number
from brief.sources.http import CachedHttpClient, SourceError


UTC = timezone.utc


def _disabled(value: Any) -> bool | None:
    if value is None:
        return None
    return str(value).lower() in {"", "none", "null", "false", "disabled"}


def _result(payload: Any) -> Any:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise SourceError("Helius returned a non-object response")
    if payload.get("error"):
        error = payload["error"]
        raise SourceError(f"Helius RPC {error.get('code')}: {error.get('message')}")
    return payload.get("result", payload)


def _account_keys(transaction: dict[str, Any]) -> list[str]:
    keys = (((transaction.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
    return [str(item.get("pubkey")) if isinstance(item, dict) else str(item) for item in keys]


def _transaction_time(transaction: dict[str, Any]) -> datetime | None:
    value = transaction.get("blockTime") or transaction.get("timestamp")
    try:
        return datetime.fromtimestamp(float(value), tz=UTC) if value else None
    except (TypeError, ValueError, OSError):
        return None


def parse_wallet_trace(owner: str, transactions: list[dict[str, Any]]) -> WalletTrace:
    if not transactions:
        return WalletTrace(owner, None, None, None, complete=True)
    ordered = sorted(transactions, key=lambda item: number(item.get("blockTime") or item.get("timestamp")))
    created_at = _transaction_time(ordered[0])
    funder: str | None = None
    funded_at: datetime | None = None
    for transaction in ordered:
        keys = _account_keys(transaction)
        meta = transaction.get("meta") or {}
        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        if owner not in keys or len(pre) != len(post) or len(keys) < len(pre):
            continue
        owner_index = keys.index(owner)
        if number(post[owner_index]) <= number(pre[owner_index]):
            continue
        donors = [
            (number(pre[index]) - number(post[index]), keys[index])
            for index in range(len(pre))
            if index != owner_index and number(pre[index]) > number(post[index])
        ]
        if donors:
            _, funder = max(donors)
            funded_at = _transaction_time(transaction)
            break
    return WalletTrace(owner, funder, funded_at, created_at, complete=True)


def parse_acquisition(mint: str, owner: str, transactions: list[dict[str, Any]]) -> AcquisitionTrace:
    if not transactions:
        return AcquisitionTrace(mint, owner, None, None)
    transaction = min(transactions, key=lambda item: number(item.get("blockTime") or item.get("timestamp")))
    meta = transaction.get("meta") or {}

    def token_total(rows: list[dict[str, Any]]) -> float:
        total = 0.0
        for row in rows:
            if row.get("mint") != mint or row.get("owner") != owner:
                continue
            ui = row.get("uiTokenAmount") or {}
            total += number(ui.get("amount") if ui.get("amount") is not None else ui.get("uiAmount"))
        return total

    before = token_total(meta.get("preTokenBalances") or [])
    after = token_total(meta.get("postTokenBalances") or [])
    initial = max(0.0, after - before)
    return AcquisitionTrace(mint, owner, _transaction_time(transaction), initial or None)


class HeliusSource:
    def __init__(
        self,
        http: CachedHttpClient,
        base_url: str,
        api_key: str | None,
        ttl: int,
        *,
        requests_per_minute: int = 100,
        holder_page_limit: int = 1000,
        max_holder_pages: int = 100,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.ttl = ttl
        self.requests_per_minute = requests_per_minute
        self.holder_page_limit = holder_page_limit
        self.max_holder_pages = max_holder_pages
        self.rate_limited = False
        # Per-owner diagnostics for the KOL scan. Each owner is scanned by one
        # coroutine, so these counters remain safe while wallets run in parallel.
        self.wallet_history_pages: dict[str, int] = {}

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def _rpc(
        self, method: str, params: Any, *, ttl: int, family: str = "helius",
        requests_per_minute: int | None = None,
    ) -> Any:
        if not self.api_key:
            raise SourceError("Helius is not configured")
        if self.rate_limited:
            raise SourceError("Helius rate-limit circuit open; metrics unavailable for the rest of this run")
        try:
            payload = await self.http.post_json(
                self.base_url,
                params={"api-key": self.api_key},
                family=family,
                limit=requests_per_minute or self.requests_per_minute,
                ttl=ttl,
                json_body={"jsonrpc": "2.0", "id": f"brief:{method}", "method": method, "params": params},
            )
        except SourceError as exc:
            if "HTTP 429" in str(exc):
                self.rate_limited = True
            raise
        return _result(payload)

    async def enrich(self, mint: str) -> Enrichment:
        if not self.api_key:
            return Enrichment(source="unavailable")
        result = await self._rpc("getAsset", {"id": mint}, ttl=self.ttl)
        return self._parse_enrichment(result)

    async def transaction(self, signature: str, *, ttl: int = 30) -> dict[str, Any] | None:
        result = await self._rpc(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
            ttl=ttl,
            family="helius-launches",
        )
        return result if isinstance(result, dict) else None

    async def recent_program_transactions(
        self, program: str, *, since_unix: int, limit: int = 1000, ttl: int = 30
    ) -> list[dict[str, Any]]:
        result = await self._rpc(
            "getTransactionsForAddress",
            [program, {
                "transactionDetails": "full",
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "sortOrder": "desc",
                "limit": min(1000, limit),
                "filters": {"blockTime": {"gte": since_unix}, "status": "succeeded"},
            }],
            ttl=ttl,
            family="helius-launches",
        )
        return [item for item in (result.get("data") or []) if isinstance(item, dict)]

    @staticmethod
    def _parse_enrichment(result: dict[str, Any]) -> Enrichment:
        token_info = result.get("token_info") or {}
        authorities = result.get("authorities") or []
        mint_authority = token_info.get("mint_authority")
        freeze_authority = token_info.get("freeze_authority")
        if mint_authority is None:
            matching = [a for a in authorities if str(a.get("scope", "")).lower() == "mint"]
            mint_authority = matching[0].get("address") if matching else None
        return Enrichment(
            holder_count=token_info.get("holder_count"),
            mint_authority_renounced=_disabled(mint_authority),
            freeze_authority_disabled=_disabled(freeze_authority),
            supply_raw=number(token_info.get("supply")) if token_info.get("supply") is not None else None,
            decimals=int(token_info.get("decimals")) if token_info.get("decimals") is not None else None,
            source="helius",
        )

    async def token_account_balances(self, addresses: list[str], *, ttl: int = 30) -> dict[str, float]:
        if not addresses:
            return {}
        balances: dict[str, float] = {}
        for index in range(0, len(addresses), 100):
            chunk = addresses[index:index + 100]
            result = await self._rpc(
                "getMultipleAccounts",
                [chunk, {"encoding": "jsonParsed", "commitment": "confirmed"}],
                ttl=ttl,
                family="helius",
            )
            values = result.get("value") or []
            for address, value in zip(chunk, values):
                info = (((value or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                token_amount = info.get("tokenAmount") or {}
                balances[address] = number(token_amount.get("amount"))
        return balances

    async def enrich_batch(self, mints: list[str]) -> dict[str, Enrichment]:
        enriched: dict[str, Enrichment] = {}
        for index in range(0, len(mints), 1000):
            chunk = mints[index:index + 1000]
            results = await self._rpc("getAssetBatch", {"ids": chunk}, ttl=self.ttl)
            for mint, result in zip(chunk, results if isinstance(results, list) else []):
                if isinstance(result, dict):
                    enriched[mint] = self._parse_enrichment(result)
        return enriched

    async def mint_authorities_batch(self, mints: list[str]) -> dict[str, Enrichment]:
        """Read the SPL mint accounts directly and distinguish null from absent.

        DAS getAsset often omits authority fields entirely. The parsed SPL mint
        account includes both keys and returns JSON null when an authority was
        actually revoked, which is the explicit proof a fail-closed audit needs.
        """
        enriched: dict[str, Enrichment] = {}
        for index in range(0, len(mints), 100):
            chunk = mints[index:index + 100]
            result = await self._rpc(
                "getMultipleAccounts",
                [chunk, {"encoding": "jsonParsed", "commitment": "confirmed"}],
                ttl=self.ttl,
            )
            values = result.get("value") or []
            for mint, value in zip(chunk, values):
                info = (((value or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                if not isinstance(info, dict):
                    continue
                mint_safe = info.get("mintAuthority") is None if "mintAuthority" in info else None
                freeze_safe = info.get("freezeAuthority") is None if "freezeAuthority" in info else None
                enriched[mint] = Enrichment(
                    mint_authority_renounced=mint_safe,
                    freeze_authority_disabled=freeze_safe,
                    supply_raw=number(info.get("supply")) if info.get("supply") is not None else None,
                    decimals=int(info.get("decimals")) if info.get("decimals") is not None else None,
                    source="helius-rpc",
                )
        return enriched

    async def token_holders(
        self,
        mint: str,
        *,
        excluded_accounts: set[str] | None = None,
        excluded_owners: set[str] | None = None,
        ttl: int = 60,
    ) -> tuple[list[HolderBalance], int]:
        excluded_accounts = excluded_accounts or set()
        excluded_owners = excluded_owners or set()
        by_owner: dict[str, float] = {}
        owner_accounts: dict[str, list[str]] = {}
        cursor: str | None = None
        seen_cursors: set[str] = set()
        excluded_count = 0
        for _ in range(self.max_holder_pages):
            params: dict[str, Any] = {
                "mint": mint,
                "limit": self.holder_page_limit,
                "options": {"showZeroBalance": False},
            }
            if cursor:
                params["cursor"] = cursor
            result = await self._rpc("getTokenAccounts", params, ttl=ttl, family="helius")
            accounts = result.get("token_accounts") or result.get("tokenAccounts") or []
            for account in accounts:
                address = str(account.get("address") or "")
                owner = str(account.get("owner") or "")
                amount = number(account.get("amount"))
                if not owner or amount <= 0:
                    continue
                if address in excluded_accounts or owner in excluded_owners:
                    excluded_count += 1
                    continue
                by_owner[owner] = by_owner.get(owner, 0.0) + amount
                owner_accounts.setdefault(owner, []).append(address)
            cursor = result.get("cursor")
            if not cursor or cursor in seen_cursors or len(accounts) < self.holder_page_limit:
                break
            seen_cursors.add(cursor)
        else:
            raise SourceError(f"holder pagination exceeded {self.max_holder_pages} pages for {mint}")
        balances = [
            HolderBalance(owner, amount, tuple(owner_accounts.get(owner, [])))
            for owner, amount in by_owner.items()
        ]
        balances.sort(key=lambda item: item.amount, reverse=True)
        return balances, excluded_count

    async def wallet_transactions(
        self,
        owner: str,
        *,
        limit: int = 60,
        max_pages: int = 100,
        ttl: int = 300,
        requests_per_minute: int | None = None,
        since_unix: int | None = None,
    ) -> list[dict[str, Any]]:
        """Most recent full transactions for a wallet, newest first.

        Returned with full details so token balance deltas can be read directly
        rather than inferring intent from swap instructions.
        """
        filters: dict[str, Any] = {
            "status": "succeeded",
            # SPL buys/sells usually move balances on associated token accounts
            # owned by the wallet. Helius defaults this to "none", which misses
            # exactly the token deltas the KOL scanner is trying to read.
            "tokenAccounts": "balanceChanged",
        }
        if since_unix is not None:
            filters["blockTime"] = {"gte": int(since_unix)}
        page_size = max(1, min(100, int(limit)))
        transactions: list[dict[str, Any]] = []
        pagination_token: str | None = None
        seen_tokens: set[str] = set()
        pages = 0
        while True:
            options: dict[str, Any] = {
                "transactionDetails": "full",
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "sortOrder": "desc",
                "limit": page_size,
                "filters": filters,
            }
            if pagination_token:
                options["paginationToken"] = pagination_token
            result = await self._rpc(
                "getTransactionsForAddress",
                [owner, options],
                ttl=ttl,
                family="helius-kol",
                requests_per_minute=requests_per_minute,
            )
            pages += 1
            data = (result or {}).get("data") if isinstance(result, dict) else None
            transactions.extend(item for item in (data or []) if isinstance(item, dict))
            next_token = str((result or {}).get("paginationToken") or "") if isinstance(result, dict) else ""
            if not next_token:
                self.wallet_history_pages[owner] = pages
                return transactions
            if next_token in seen_tokens:
                raise SourceError(f"wallet transaction pagination repeated for {owner}")
            seen_tokens.add(next_token)
            if max_pages > 0 and pages >= max_pages:
                raise SourceError(
                    f"wallet transaction pagination exceeded {max_pages} pages for {owner}"
                )
            pagination_token = next_token

    async def trace_wallet(self, owner: str, *, ttl: int) -> WalletTrace:
        result = await self._rpc(
            "getTransactionsForAddress",
            [owner, {
                "transactionDetails": "full",
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "sortOrder": "asc",
                "limit": 10,
                "filters": {"status": "succeeded"},
            }],
            ttl=ttl,
            family="helius",
        )
        return parse_wallet_trace(owner, result.get("data") or [])

    async def trace_acquisition(self, mint: str, owner: str, *, ttl: int) -> AcquisitionTrace:
        result = await self._rpc(
            "getTransactionsForAddress",
            [owner, {
                "transactionDetails": "full",
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "sortOrder": "asc",
                "limit": 1,
                "filters": {
                    "status": "succeeded",
                    "tokenAccounts": "balanceChanged",
                    "tokenTransfer": {"direction": "in", "mint": mint},
                },
            }],
            ttl=ttl,
            family="helius",
        )
        return parse_acquisition(mint, owner, result.get("data") or [])
