"""Fail-closed verification for anything leaving the private scanner.

Screening decides which tokens are interesting. This module answers the more
important delivery question: do we have complete evidence for every token the
reader is about to see? Missing data is a failure here, never an implicit pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable

from brief.config import Settings
from brief.journal import rug_or_bundle, runner_universe_reasons
from brief.models import Brief, Candidate


class DeliveryPreflightError(RuntimeError):
    """Raised before any outbound channel when one token is not fully vetted."""


@dataclass(frozen=True, slots=True)
class DeliveryProof:
    candidate_count: int
    mint_digest: str


def delivery_candidates(brief: Brief) -> list[Candidate]:
    """Every coin a recap renderer is allowed to mention, without duplicates."""
    found: list[Candidate] = []
    seen: set[str] = set()
    # The interactive Discord browser is public output too. Audit the complete
    # filter universe, not only the 10-15 names visible in the first message.
    for candidate in [
        *(brief.runner_universe or brief.runners),
        *brief.headline_tape,
    ]:
        if candidate.token.mint in seen:
            continue
        seen.add(candidate.token.mint)
        found.append(candidate)
    return found


def _exact_kol_traders(candidate: Candidate) -> tuple[bool, int]:
    """Return whether exact-mint history ran and how many clean KOLs traded."""
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    rows = gmgn.get("renownedTraders")
    if not isinstance(rows, list):
        return False, 0
    identities: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("suspicious"):
            continue
        if float(row.get("buyUsd") or 0) <= 0 and float(row.get("sellUsd") or 0) <= 0:
            continue
        identity = str(row.get("address") or row.get("name") or "").strip()
        if identity:
            identities.add(identity)
    return True, len(identities)


def candidate_preflight_reasons(candidate: Candidate, settings: Settings) -> list[str]:
    """List every reason this exact contract cannot be delivered yet."""
    reasons: list[str] = []
    report = candidate.safety
    enrichment = candidate.enrichment
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    section = settings.section("delivery")

    exact_checked, exact_kols = _exact_kol_traders(candidate)
    min_kols = int(section.get(
        "preflight_min_exact_kol_trades",
        settings.get("journal", "min_kol_trades_for_publish", 1),
    ) or 1)
    if not exact_checked:
        reasons.append("exact-mint KOL trader history unavailable")
    elif exact_kols < min_kols:
        reasons.append(f"only {exact_kols}/{min_kols} confirmed non-suspicious KOL traders")

    if not report.source or report.source == "unavailable":
        reasons.append("contract-security provider unavailable")
    if report.rugged:
        reasons.append("security provider marks the token as rugged/honeypot")

    mint_safe = (
        report.mint_authority_renounced is True
        or enrichment.mint_authority_renounced is True
    )
    freeze_safe = (
        report.freeze_authority_disabled is True
        or enrichment.freeze_authority_disabled is True
    )
    if not mint_safe:
        reasons.append("mint authority/contract mintability not confirmed disabled")
    if not freeze_safe:
        reasons.append("freeze/pause/blacklist powers not confirmed disabled")

    holder_count = enrichment.holder_count or report.holder_count
    if holder_count is None or int(holder_count) <= 0:
        reasons.append("holder count unavailable")
    if report.top10_pct is None:
        reasons.append("top-10 concentration unavailable")

    gmgn_burned = (
        str(gmgn.get("burnStatus") or "").lower() == "yes"
        or float(gmgn.get("burnRatio") or 0) >= 0.90
    )
    lp_pct = report.lp_locked_or_burned_pct
    if not gmgn_burned and (lp_pct is None or float(lp_pct) <= 0):
        reasons.append("LP lock/burn status unavailable or zero")

    if bool(section.get("preflight_block_any_security_flag", True)):
        reasons.extend(f"security flag: {flag}" for flag in report.risk_flags)
    reasons.extend(rug_or_bundle(candidate, settings))
    return list(dict.fromkeys(reasons))


def audit_candidates(candidates: Iterable[Candidate], settings: Settings) -> DeliveryProof:
    """Validate a complete outbound set or raise one actionable error."""
    unique: dict[str, Candidate] = {}
    for candidate in candidates:
        unique.setdefault(candidate.token.mint, candidate)

    failures: list[str] = []
    for candidate in unique.values():
        reasons = candidate_preflight_reasons(candidate, settings)
        if reasons:
            failures.append(
                f"${candidate.token.symbol} ({candidate.token.chain_id}:{candidate.token.mint}): "
                + "; ".join(reasons)
            )
    if failures:
        detail = "\n".join(f"- {line}" for line in failures[:20])
        more = len(failures) - 20
        if more > 0:
            detail += f"\n- and {more} more token(s)"
        raise DeliveryPreflightError(
            f"delivery blocked: {len(failures)}/{len(unique)} token(s) failed preflight\n{detail}"
        )

    mints = "\n".join(sorted(unique)).encode("utf-8")
    return DeliveryProof(len(unique), sha256(mints).hexdigest())


def audit_brief(brief: Brief, settings: Settings) -> DeliveryProof:
    # The concise public recap remains fully audited (including exact KOL and
    # complete provider evidence). The wider interactive browser deliberately
    # has a different promise: every measured runner is retained unless a
    # confirmed danger signal fails the broad universe gate.
    visible: list[Candidate] = []
    visible_seen: set[str] = set()
    for candidate in [*brief.runners, *brief.headline_tape]:
        if candidate.token.mint not in visible_seen:
            visible_seen.add(candidate.token.mint)
            visible.append(candidate)
    audit_candidates(visible, settings)

    failures: list[str] = []
    for candidate in brief.runner_universe or brief.runners:
        reasons = runner_universe_reasons(candidate, settings)
        if reasons:
            failures.append(
                f"${candidate.token.symbol} ({candidate.token.chain_id}:{candidate.token.mint}): "
                + "; ".join(reasons)
            )
    if failures:
        detail = "\n".join(f"- {line}" for line in failures[:20])
        more = len(failures) - 20
        if more > 0:
            detail += f"\n- and {more} more token(s)"
        raise DeliveryPreflightError(
            f"delivery blocked: {len(failures)} filtered runner(s) failed the confirmed-danger gate\n{detail}"
        )

    delivered = delivery_candidates(brief)
    mints = "\n".join(sorted(candidate.token.mint for candidate in delivered)).encode("utf-8")
    return DeliveryProof(len(delivered), sha256(mints).hexdigest())
