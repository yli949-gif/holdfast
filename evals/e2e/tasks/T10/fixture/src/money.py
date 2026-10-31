"""Money helpers. Amounts are floats in the currency's major unit (e.g. 12.5 == 12.50 EUR)."""


def round_money(value: float) -> float:
    """Round to cents."""
    return round(value, 2)


def format_amount(value: float) -> str:
    """Human-readable amount with two decimals."""
    return f"{value:.2f}"


def split_evenly(total: float, parts: int) -> list[float]:
    """Split ``total`` into ``parts`` shares that add up to ``total``."""
    share = round_money(total / parts)
    return [share] * parts
