from __future__ import annotations


def money(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if absolute >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if absolute >= 1_000:
        return f"${value / 1_000:.0f}k"
    return f"${value:,.0f}"


def pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}%"


def ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}x"


def yes_no(value: bool | None, yes: str, no: str) -> str:
    if value is None:
        return "unavailable"
    return yes if value else no

