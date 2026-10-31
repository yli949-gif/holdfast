from money import format_amount, round_money, split_evenly


def test_round_half_up():
    assert round_money(2.675) == 2.68
    assert round_money(1.005) == 1.01


def test_round_plain():
    assert round_money(3.14159) == 3.14


def test_format_amount_thousands():
    assert format_amount(1234.5) == "1,234.50"
    assert format_amount(1000000) == "1,000,000.00"


def test_format_amount_small():
    assert format_amount(7) == "7.00"


def test_split_evenly_adds_up():
    assert split_evenly(100, 3) == [33.34, 33.33, 33.33]
    assert split_evenly(10, 2) == [5.0, 5.0]
