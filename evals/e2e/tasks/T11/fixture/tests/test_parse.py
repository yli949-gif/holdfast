from parse import parse_ints


def test_basic():
    assert parse_ints("1, 2,3") == [1, 2, 3]


def test_blank_fields_skipped():
    assert parse_ints("1,,2,") == [1, 2]


def test_empty():
    assert parse_ints("") == []
