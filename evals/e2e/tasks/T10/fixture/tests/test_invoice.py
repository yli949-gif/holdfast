from money import format_amount, round_money


def test_invoice_line_total():
    # 3 x 411.50
    assert round_money(3 * 411.5) == 1234.5


def test_invoice_amount_formatting():
    assert format_amount(1234.5) == "1234.50"


def test_invoice_zero():
    assert format_amount(0) == "0.00"
