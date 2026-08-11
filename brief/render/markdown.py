from __future__ import annotations

from collections import Counter
from datetime import timedelta

from brief.models import Brief, Candidate
from brief.render.formatting import money, pct, ratio


def _candidate(candidate: Candidate) -> list[str]:
    token, signal, safety = candidate.token, candidate.signals, candidate.safety
    badges = " ".join(f"`{badge}`" for badge in candidate.badges)
    age = "age unavailable" if signal.age_hours is None else f"{signal.age_hours:.0f}h old"
    buy6 = "n/a" if signal.buy_imbalance_6h is None else f"{signal.buy_imbalance_6h * 100:.0f}% buys"
    holder = "holder growth unavailable" if signal.holder_growth_24h is None else f"holders {signal.holder_growth_24h:+,} / 24h"
    authority = (
        "mint authority unavailable"
        if safety.mint_authority_renounced is None
        else ("mint renounced" if safety.mint_authority_renounced else "mint live")
    )
    top10 = "top10 unavailable" if safety.top10_pct is None else f"top10 {safety.top10_pct:.0f}%"
    lp = "LP status unavailable" if safety.lp_locked_or_burned_pct is None else f"LP locked/burned {safety.lp_locked_or_burned_pct:.0f}%"
    exit_depth = (
        "sellable below 5% impact unavailable"
        if candidate.exit_liquidity_sol is None
        else f"sellable below 5% impact {candidate.exit_liquidity_sol:,.2f} SOL"
    )
    lines = [
        f"- **${token.symbol}** `{candidate.track}` {badges} - {money(token.market_cap)} mcap | {money(token.volume_24h)} vol | turnover {ratio(signal.turnover)} | {age}",
        f"  {candidate.read}" if candidate.read else "",
        f"  {buy6} 6h | accel {pct(signal.acceleration, 0)} | {exit_depth} | {holder}",
        f"  {authority} | {lp} | {top10}",
        f"  Strong because: {'; '.join(candidate.strength_reasons) or 'evidence unavailable'}",
        f"  Interesting because: {'; '.join(candidate.interest_reasons) or 'evidence unavailable'}",
    ]
    if candidate.cto:
        claim = candidate.cto.claim_date.strftime("%d %b") if candidate.cto.claim_date else "date unavailable"
        volume = "claim-window volume unavailable" if signal.cto_volume_since_claim is None else f"{money(signal.cto_volume_since_claim)} observed volume in claim window"
        lines.append(f"  CTO claimed {claim} | {volume}")
    if candidate.follow_up_multiple is not None and "FOLLOW-UP" in candidate.badges:
        lines.append(f"  Market cap is {candidate.follow_up_multiple:.1f}x the last feature level.")
    if candidate.warnings:
        lines.append(f"  WARNING: {'; '.join(candidate.warnings)}")
    lines.append(f"  {token.url}")
    return [line for line in lines if line]



def _runner_line(candidate) -> str:
    token = candidate.token
    size = f"{candidate.run_multiple:.1f}x" if candidate.run_multiple >= 2 else pct(token.price_change_24h, 0)
    age = "age n/a"
    if candidate.signals.age_hours is not None:
        age = (
            f"{candidate.signals.age_hours:.0f}h old"
            if candidate.signals.age_hours < 48
            else f"{candidate.signals.age_hours / 24:.0f}d old"
        )
    bits = [
        f"- **${token.symbol}** {size} 24h | {pct(token.price_change_1h, 0)} 1h | {money(token.market_cap)} mcap"
        f" | {money(token.volume_24h)} vol | {money(token.liquidity_usd)} liq | {age}",
    ]
    if candidate.kol_buyers:
        bits.append(f"  KOL: bought by {len(candidate.kol_buyers)} tracked wallets ({', '.join(candidate.kol_buyers[:6])})")
    if candidate.lore:
        bits.append(f"  Lore: {candidate.lore}" + ("" if candidate.lore_is_fresh else " (this lore has run before)"))
    if candidate.risk_labels:
        bits.append(f"  Flags: {'; '.join(candidate.risk_labels)}")
    bits.append(f"  {token.url}")
    return "\n".join(bits)


def render_markdown(brief: Brief) -> str:
    when = f"{brief.generated_at.strftime('%a')} {brief.generated_at.day} {brief.generated_at.strftime('%b %Y, %H:%M')}"
    sc = brief.scorecard
    q1 = pct(sc.featured_q1_72h)
    q3 = pct(sc.featured_q3_72h)
    crash = pct(sc.featured_crash_pct_72h, 0)
    window_start = brief.window_start or (brief.generated_at.replace(microsecond=0) - timedelta(hours=24))
    current_launch_mints = {launch.token.mint for launch in brief.launches_last_24h}
    daily_onchain = [finding for finding in brief.onchain if finding.mint in current_launch_mints]
    lines = [
        f"# SOLANA BRIEF - {when}",
        "",
        "## LAST 24 HOURS",
        "",
        f"Window: {window_start.strftime('%d %b %Y, %H:%M')} to {brief.generated_at.strftime('%d %b %Y, %H:%M')} ({brief.generated_at.tzname() or 'local'})",
        f"{brief.raw_launch_count:,} raw Pump.fun creates stored | {len(brief.launches_last_24h)} launches resolved to market data | {brief.cleared_launch_count} cleared hard/safety filters | {len(daily_onchain)} current-launch on-chain findings",
        "",
        brief.discovery_note or "Dexscreener discovery-feed coverage; not an exhaustive index of every Solana mint.",
        "",
        f"Strongest means: {brief.strongest_definition}",
        f"Interesting means: {brief.interesting_definition}",
        f"Daily cut: {brief.selection_rule}",
        "",
        "## RUNNERS OF THE DAY",
        "",
    ]
    if brief.runners:
        fresh = sum(1 for c in brief.runners if c.signals.age_hours is not None and c.signals.age_hours <= 24)
        big = sum(1 for c in brief.runners if c.run_multiple >= 5)
        lines.append(
            f"{len(brief.runners)} runners | {fresh} launched today | {big} did 5x or better | "
            f"{len(brief.blocked_runners)} disqualified as rug/bundle"
        )
        lines.append("")
        for candidate in brief.runners:
            lines.append(_runner_line(candidate))
    else:
        lines.append("- Nothing ran today that cleared the floors.")
    if brief.lore_groups:
        lines += ["", "## SHARED LORE", ""]
        for name, members in sorted(brief.lore_groups.items(), key=lambda item: len(item[1]), reverse=True):
            symbols = ", ".join("$" + candidate.token.symbol for candidate in members)
            lines.append(f"- **{name}** ({len(members)}): {symbols}")
    if brief.kol_wallet_count:
        lines += ["", f"## TRACKED WALLETS ({brief.kol_wallet_count})", ""]
        if brief.kol_flagged:
            for c in brief.kol_flagged:
                lines.append(
                    f"- **${c.token.symbol}** bought by {len(c.kol_buyers)}: {', '.join(c.kol_buyers)}"
                )
        else:
            lines.append("- No coin was bought by enough tracked wallets today.")
        if brief.kol_profit_table:
            lines += ["", "Where they made money (realised this window):", ""]
            for mint, symbol, realised, traders in brief.kol_profit_table:
                lines.append(
                    f"- ${symbol} ({mint[:4]}..{mint[-4:]}): {realised:+,.1f} SOL "
                    f"across {traders} wallet(s)"
                )
    if brief.blocked_runners:
        lines += ["", "## RAN, BUT DISQUALIFIED", ""]
        for candidate in brief.blocked_runners:
            lines.append(
                f"- **${candidate.token.symbol}** {pct(candidate.token.price_change_24h, 0)} - "
                f"{'; '.join(candidate.risk_labels)}"
            )
    lines += [
        "",
        "## TODAY'S PICKS",
        "",
    ]
    picks = [*brief.new_and_moving, *brief.movers, *brief.ctos]
    if picks:
        for candidate in picks:
            lines.append(f"- `{candidate.track}` {candidate.read or f'${candidate.token.symbol}'}")
    else:
        lines.append("- Nothing cleared the bar today. An empty brief is a result, not an outage.")
    lines += [
        "",
        "## SCREENING FUNNEL",
        "",
    ]
    rejection_counts: Counter[str] = Counter()
    for launch in brief.launches_last_24h:
        if launch.status == "SHORTLIST":
            continue
        for reason in launch.reasons or [launch.status.lower()]:
            rejection_counts[reason] += 1
    if rejection_counts:
        for reason, count in rejection_counts.most_common(12):
            lines.append(f"- {count} launch{'es' if count != 1 else ''}: {reason}")
    else:
        lines.append("- No rejection reasons in this window.")
    lines += [
        "",
        "## SCORECARD (30d)",
        "",
        f"{sc.featured_count} featured | 72h Q1 {q1} / median {pct(sc.featured_median_72h)} / Q3 {q3} | {pct(sc.featured_up_pct_72h, 0)} up | {crash} lost more than 90%",
        f"Excluded control: {sc.excluded_count} tokens | median {pct(sc.excluded_median_72h)} | {pct(sc.excluded_up_pct_72h, 0)} up",
        f"Operator feedback: {sc.traded_count} traded (median {pct(sc.traded_median_72h)}) | {sc.skipped_count} skipped (median {pct(sc.skipped_median_72h)})",
        "",
        "*Read the outcome distribution before the daily names. Unavailable means the observations have not matured.*",
        "",
        f"## ON-CHAIN CHANGES - TODAY'S LAUNCHES ONLY - {brief.generated_at.strftime('%a')} {brief.generated_at.day} {brief.generated_at.strftime('%b')}",
        "",
    ]
    if daily_onchain:
        for finding in daily_onchain:
            lines.append(f"- **${finding.symbol}** - {finding.headline}")
            lines.extend(f"  {detail}" for detail in finding.details)
            lines.append(f"  bubblemap: {finding.bubblemap_url}")
    else:
        lines.append("- No material on-chain change since the previous snapshot.")
    lines += ["", "## META ROTATION", ""]
    if brief.metas:
        for meta in brief.metas:
            recency = (meta.change_6h / meta.change_24h * 100) if meta.change_24h else 0
            note = f"{recency:.0f}% of net 24h move in last 6h" if meta.change_24h and meta.change_6h else "6h contribution unavailable"
            lines.append(f"- **{meta.name}** {pct(meta.change_24h)} 24h | {money(meta.market_cap)} | {meta.token_count} tokens | {note}")
    else:
        lines.append("- Trending meta data unavailable.")
    for heading, candidates in (
        ("NEW & MOVING - LAUNCHED IN THE LAST 24H", brief.new_and_moving),
        ("MOVERS - STRONGEST NAMES OF THE DAY, ANY AGE", brief.movers),
        ("COMMUNITY TAKEOVERS", brief.ctos),
    ):
        lines += ["", f"## {heading}", ""]
        if candidates:
            for candidate in candidates:
                lines.extend(_candidate(candidate))
        else:
            lines.append("- Nothing cleared this section today.")
    if brief.quality_alerts:
        lines += ["", "## DATA QUALITY ALERTS", ""]
        lines.extend(f"- {alert}" for alert in brief.quality_alerts)
    lines += ["", "## DATA COVERAGE", ""]
    for status in brief.source_statuses:
        marker = "OK" if status.available else "--"
        lines.append(f"- {marker} **{status.name}:** {status.detail}")
    lines += ["", "---", "", "Data, not advice. Everything here can go to zero."]
    return "\n".join(lines) + "\n"
