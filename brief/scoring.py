from __future__ import annotations

from brief.config import Settings
from brief.models import Candidate


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _scale(value: float, target: float, *, floor: float = 0.0) -> float:
    if target <= 0:
        return floor
    return _clamp(value / target * 100.0, floor)


def _buy_ratio_quality(value: float | None) -> float:
    if value is None:
        return 45.0
    # Healthy hot markets are two-sided. A book with almost no sells is not
    # organic; a book with almost no buys is fading.
    distance = abs(value - 0.56)
    return _clamp(100.0 - distance / 0.34 * 100.0)


def _recent_volume_share(candidate: Candidate) -> float | None:
    if candidate.token.volume_24h <= 0:
        return None
    # Dexscreener has no h6 bucket on the EVM chains; a missing window is not
    # a share of zero, and scoring it as one buried real runners' alerts.
    if candidate.token.volume_6h <= 0:
        return None
    return candidate.token.volume_6h / candidate.token.volume_24h


def _holder_growth_score(candidate: Candidate) -> float:
    growth = candidate.enrichment.holder_change_24h
    holders = candidate.safety.holder_count
    if growth is None or holders is None or holders <= 0:
        return 45.0
    prior = max(1, holders - growth)
    pct = growth / prior
    if growth <= 0:
        return 10.0
    return _clamp(35.0 + pct * 130.0)


def _distribution_score(candidate: Candidate) -> float:
    top10 = candidate.safety.top10_pct
    if top10 is None:
        return 40.0
    # 10% top10 is excellent, 25% is acceptable, 50% is nearly bundled.
    return _clamp(110.0 - top10 * 2.8)


def _holder_quality_score(candidate: Candidate, settings: Settings) -> float:
    min_holders = float(settings.get("journal", "min_holders", 1000) or 1000)
    holder_count = float(candidate.safety.holder_count or 0)
    count_score = _scale(holder_count, max(min_holders * 3.0, 1.0), floor=15.0 if holder_count else 0.0)
    distribution = _distribution_score(candidate)
    return _clamp(count_score * 0.45 + distribution * 0.55)


def _liquidity_score(candidate: Candidate) -> float:
    token = candidate.token
    liquidity_depth = token.liquidity_usd / token.market_cap if token.market_cap else 0.0
    depth_score = _scale(liquidity_depth, 0.18)
    absolute_score = _scale(token.liquidity_usd, 250_000.0, floor=10.0 if token.liquidity_usd else 0.0)
    ratio = token.volume_24h / token.liquidity_usd if token.liquidity_usd else 999.0
    ratio_score = 100.0 if 2.0 <= ratio <= 35.0 else _clamp(100.0 - max(0.0, ratio - 35.0) * 1.8)
    return _clamp(depth_score * 0.40 + absolute_score * 0.35 + ratio_score * 0.25)


def _price_structure_score(candidate: Candidate) -> float:
    token = candidate.token
    score = 55.0
    score += _clamp(token.price_change_6h / 4.0, -20.0, 25.0)
    if token.price_change_1h < 0:
        score += max(-35.0, token.price_change_1h)
    else:
        score += min(15.0, token.price_change_1h / 2.0)
    share = _recent_volume_share(candidate)
    if share is None:
        score -= 10.0
    elif share < 0.08:
        score -= 35.0
    elif share < 0.15:
        score -= 15.0
    elif share > 0.55:
        score += 10.0
    return _clamp(score)


def _buyer_diversity_score(candidate: Candidate) -> float:
    trades = candidate.token.txns_24h.total or candidate.token.txns_6h.total
    holders = candidate.safety.holder_count or 0
    if not trades or not holders:
        return 40.0
    trade_holder_ratio = trades / max(holders, 1)
    ratio_score = 100.0 if 0.4 <= trade_holder_ratio <= 8.0 else _clamp(100.0 - max(0.0, trade_holder_ratio - 8.0) * 4.0)
    return _clamp(ratio_score * 0.55 + _buy_ratio_quality(candidate.signals.buy_imbalance_6h) * 0.45)


def _organic_volume_score(candidate: Candidate) -> float:
    ratio = candidate.token.volume_24h / candidate.token.liquidity_usd if candidate.token.liquidity_usd else 999.0
    turnover = candidate.signals.turnover
    recent = _recent_volume_share(candidate)
    turnover_score = _scale(turnover, 1.2, floor=10.0 if turnover else 0.0)
    ratio_score = 100.0 if 3.0 <= ratio <= 45.0 else _clamp(100.0 - max(0.0, ratio - 45.0) * 2.0)
    recent_score = 45.0 if recent is None else _scale(recent, 0.25)
    return _clamp(turnover_score * 0.35 + ratio_score * 0.35 + recent_score * 0.30)


def _kol_score(candidate: Candidate, settings: Settings) -> float:
    gmgn = candidate.provider_evidence.get("gmgn", {})
    gmgn_buyers = len((gmgn.get("walletFlow") or {}).get("kolBuyers", []))
    gmgn_count = int(gmgn.get("kolCount") or 0)
    if not candidate.kol_wallets_scanned and not gmgn_buyers and not gmgn_count:
        return 0.0
    buyers = len(set(candidate.kol_buyers))
    holders = len(set(candidate.kol_holders))
    sellers = len(set(candidate.kol_sellers))
    min_flag = int(settings.get("kol", "min_buyers_to_flag", 2) or 2)
    buyer_score = _scale(max(buyers, gmgn_buyers, gmgn_count), max(min_flag * 3, 1))
    retention = holders / buyers if buyers else 0.0
    retention_score = _clamp(retention * 100.0)
    realised_score = _scale(max(candidate.kol_realised_sol, 0.0), 25.0)
    participation_score = _scale(buyers + sellers, 8.0)
    return _clamp(buyer_score * 0.45 + retention_score * 0.25 + realised_score * 0.15 + participation_score * 0.15)


def _smart_money_score(candidate: Candidate) -> float:
    gmgn = candidate.provider_evidence.get("gmgn", {})
    flow = gmgn.get("walletFlow") or {}
    buyers = len(flow.get("smartMoneyBuyers", []))
    sellers = len(flow.get("smartMoneySellers", []))
    holders = int(gmgn.get("smartMoneyCount") or 0)
    if not buyers and not sellers and not holders:
        return 0.0
    convergence = _scale(buyers, 5.0)
    breadth = _scale(holders, 12.0)
    balance = _clamp(65.0 + (buyers - sellers) * 10.0)
    return _clamp(convergence * 0.45 + breadth * 0.35 + balance * 0.20)


def _manipulation_score(candidate: Candidate, settings: Settings) -> float:
    token = candidate.token
    score = 0.0
    ratio = token.volume_24h / token.liquidity_usd if token.liquidity_usd else 999.0
    top10 = candidate.safety.top10_pct
    if ratio > float(settings.get("journal", "thin_liquidity_ratio", 60) or 60):
        score += min(35.0, (ratio - 60.0) * 0.7)
    if candidate.signals.turnover > float(settings.get("journal", "max_turnover", 30) or 30):
        score += 25.0
    if top10 is not None and top10 > 25:
        score += min(35.0, (top10 - 25.0) * 1.4)
    if token.active_boosts:
        score += 5.0
    if not token.socials:
        score += 12.0
    if candidate.recycled_label_count:
        score += min(18.0, 6.0 * candidate.recycled_label_count)
    if candidate.signals.buy_imbalance_6h is not None and candidate.signals.buy_imbalance_6h >= 0.82:
        score += 18.0
    if candidate.run_multiple >= float(settings.get("journal", "extreme_multiple", 10) or 10):
        extreme_volume = float(settings.get("journal", "extreme_min_volume_24h", 0) or 0)
        if extreme_volume and token.volume_24h < extreme_volume:
            score += 15.0
        extreme_holders = int(settings.get("journal", "extreme_min_holders", 0) or 0)
        if extreme_holders and candidate.safety.holder_count is not None and candidate.safety.holder_count < extreme_holders:
            score += 15.0
    gmgn = candidate.provider_evidence.get("gmgn", {})
    if gmgn.get("washTrading") is True:
        score += 45.0
    rug_ratio = gmgn.get("rugRatio")
    if rug_ratio is not None:
        score += _clamp((float(rug_ratio) - 0.15) * 55.0, 0.0, 35.0)
    for field, weight in (("bundlerRate", 45.0), ("insiderRate", 35.0), ("freshWalletRate", 20.0)):
        value = gmgn.get(field)
        if value is not None:
            score += _clamp((float(value) - 0.15) * weight, 0.0, weight * 0.6)
    return _clamp(score)


def _classify(scores: dict[str, float], candidate: Candidate) -> str:
    if scores["manipulation"] >= 70:
        return "HIGH MANIPULATION RISK"
    if scores["holderQuality"] < 35:
        return "LOW-HOLDER-QUALITY RUNNER"
    if scores["liquidity"] < 35:
        return "LOW-LIQUIDITY RUNNER"
    if scores["kol"] >= 65 and scores["organic"] >= 70:
        return "HIGH-CONVICTION ORGANIC RUNNER"
    if scores["kol"] >= 60:
        return "KOL-DRIVEN RUNNER"
    if scores["organic"] >= 80:
        return "ORGANIC RUNNER"
    if candidate.run_multiple >= 10 and scores["organic"] < 55:
        return "HIGH MOMENTUM / LOW ORGANICITY"
    return "MIXED RUNNER"


def score_candidate(candidate: Candidate, settings: Settings) -> None:
    """Attach three independent scores plus explicit data confidence.

    Strength answers how large/fast the observed run was. Organic quality asks
    whether the participation looks broad and durable. Manipulation risk asks
    whether the tape/holders look manufactured. None is algebraically derived
    from another, so a huge suspicious run remains visible as exactly that.
    """
    holder_growth = _holder_growth_score(candidate)
    buyer_diversity = _buyer_diversity_score(candidate)
    organic_volume = _organic_volume_score(candidate)
    holder_quality = _holder_quality_score(candidate, settings)
    liquidity = _liquidity_score(candidate)
    price_structure = _price_structure_score(candidate)
    wallet_independence = 50.0  # unavailable without a completed cluster snapshot
    kol = _kol_score(candidate, settings)
    smart_money = _smart_money_score(candidate)
    manipulation = _manipulation_score(candidate, settings)
    peak_multiple = float(candidate.peak_multiple or candidate.run_multiple or 1.0)
    multiple_strength = _scale(max(0.0, peak_multiple - 1.0), 9.0)
    peak_strength = _scale(float(candidate.peak_market_cap or candidate.token.market_cap), 1_000_000.0)
    volume_strength = _scale(candidate.token.volume_24h, 2_000_000.0)
    trade_strength = _scale(float(candidate.token.txns_24h.total), 10_000.0)
    attention_strength = _scale(
        float((candidate.provider_evidence.get("gmgn", {}) or {}).get("searchHeat") or 0), 500.0
    )
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    # This flag means the token independently cleared GMGN's 24h participation,
    # liquidity, holder-distribution, and chain-native safety filters. Treat it
    # as corroboration, not a gate: the capped/rate-limited lane can miss a real
    # runner, while direct adverse evidence is handled by manipulation scoring.
    gmgn_organic = 100.0 if gmgn.get("organicQualified") is True else 0.0

    organic = _clamp(
        holder_growth * 0.20
        + buyer_diversity * 0.15
        + organic_volume * 0.15
        + holder_quality * 0.15
        + liquidity * 0.10
        + price_structure * 0.10
        + wallet_independence * 0.10
        + smart_money * 0.05
        + gmgn_organic * 0.05
    )
    # Deliberately independent: strength is not reduced because a run looks
    # manipulated. That risk appears in the separate manipulation score.
    runner = _clamp(
        multiple_strength * 0.25
        + peak_strength * 0.20
        + volume_strength * 0.20
        + trade_strength * 0.15
        + kol * 0.08
        + smart_money * 0.08
        + attention_strength * 0.04
        + gmgn_organic * 0.06
    )

    scores = {
        "runner": round(runner, 1),
        "organic": round(organic, 1),
        "smartMoney": round(smart_money, 1),
        "kol": round(kol, 1),
        "liquidity": round(liquidity, 1),
        "holderQuality": round(holder_quality, 1),
        "priceStructure": round(price_structure, 1),
        "manipulation": round(manipulation, 1),
    }
    candidate.scores = scores
    candidate.score_components = {
        "organic": {
            "holderGrowth": round(holder_growth, 1),
            "buyerDiversity": round(buyer_diversity, 1),
            "organicVolume": round(organic_volume, 1),
            "distribution": round(_distribution_score(candidate), 1),
            "liquidity": round(liquidity, 1),
            "priceStructure": round(price_structure, 1),
            "walletIndependence": "unavailable",
            "smartMoney": round(smart_money, 1),
            "gmgnOrganicLane": round(gmgn_organic, 1),
        },
        "runner": {
            "peakMultiple": round(multiple_strength, 1),
            "peakMarketCap": round(peak_strength, 1),
            "volume": round(volume_strength, 1),
            "trades": round(trade_strength, 1),
            "attention": round(attention_strength, 1),
            "gmgnOrganicLane": round(gmgn_organic, 1),
        },
        "manipulation": {
            "score": round(manipulation, 1),
            "gmgnRiskFieldsAvailable": sum(
                candidate.provider_evidence.get("gmgn", {}).get(key) is not None
                for key in ("rugRatio", "washTrading", "bundlerRate", "insiderRate", "freshWalletRate")
            ),
        },
    }
    organic_known = sum(
        value is not None
        for value in (
            candidate.enrichment.holder_change_24h,
            candidate.safety.top10_pct,
            candidate.safety.holder_count,
            candidate.signals.buy_imbalance_6h,
            candidate.token.liquidity_usd if candidate.token.liquidity_usd else None,
            candidate.token.volume_24h if candidate.token.volume_24h else None,
        )
    )
    risk_known = sum(
        candidate.provider_evidence.get("gmgn", {}).get(key) is not None
        for key in ("rugRatio", "washTrading", "bundlerRate", "insiderRate", "freshWalletRate")
    ) + int(candidate.safety.top10_pct is not None)
    candidate.score_confidence = {
        "runner": round(min(1.0, 0.45 + 0.11 * sum(bool(v) for v in (
            candidate.peak_market_cap, candidate.start_market_cap, candidate.token.volume_24h,
            candidate.token.txns_24h.total, candidate.first_seen_at,
        ))), 2),
        "organic": round(organic_known / 6.0, 2),
        "manipulation": round(min(1.0, risk_known / 6.0), 2),
    }
    candidate.classification = _classify(scores, candidate)


def score_candidates(candidates: list[Candidate], settings: Settings) -> None:
    for candidate in candidates:
        score_candidate(candidate, settings)
