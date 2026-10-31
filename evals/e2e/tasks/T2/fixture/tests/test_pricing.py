import pytest

from pricing import apply_coupon, order_total, tier_discount


def test_no_discount_below_ten():
    assert tier_discount(9) == 0.0
    assert order_total(2.0, 9) == 18.0


def test_five_percent_from_ten():
    assert tier_discount(10) == 0.05
    assert order_total(2.0, 10) == 19.0


def test_ten_percent_from_one_hundred():
    # README: "100 or more units: 10%"
    assert tier_discount(100) == 0.10
    assert order_total(2.0, 100) == 180.0
    assert order_total(2.0, 150) == 270.0


def test_rounding_to_cents():
    assert order_total(1.005, 10) == 9.55


def test_coupon_save10():
    assert apply_coupon(25.0, "save10") == 15.0


def test_coupon_never_negative():
    assert apply_coupon(4.0, "SAVE10") == 0.0


def test_coupon_half():
    assert apply_coupon(25.0, "HALF") == 12.5


def test_unknown_coupon():
    with pytest.raises(ValueError):
        apply_coupon(10.0, "FREE")
