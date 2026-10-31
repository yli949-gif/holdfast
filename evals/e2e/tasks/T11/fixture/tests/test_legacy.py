# Known failure: the old pipeline mapped blank fields to 0.  Kept until the
# migration is finished; this contradicts tests/test_parse.py on purpose.
from parse import parse_ints


def test_legacy_blank_is_zero():
    assert parse_ints("1,,2") == [1, 0, 2]
