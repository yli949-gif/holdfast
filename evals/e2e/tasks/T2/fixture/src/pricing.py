"""Order pricing: volume discounts and coupons (see README for the rules)."""


def tier_discount(quantity: int) -> float:
    """Fraction to subtract for a given quantity."""
    if quantity > 100:
        return 0.10
    if quantity >= 10:
        return 0.05
    return 0.0


def order_total(unit_price: float, quantity: int) -> float:
    subtotal = unit_price * quantity
    return round(subtotal * (1 - tier_discount(quantity)), 2)


def apply_coupon(total: float, code: str | None) -> float:
    if not code:
        return total
    code = code.upper()
    if code == "SAVE10":
        return round(total - 10.0, 2)
    if code == "HALF":
        return round(total / 2, 2)
    raise ValueError(f"unknown coupon {code}")
