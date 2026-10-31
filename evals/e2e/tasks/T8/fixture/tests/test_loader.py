from pathlib import Path

from catalog import load_records, Record

DATA = Path(__file__).resolve().parent.parent / "data"


def test_loads_live_catalog():
    records = load_records(DATA)
    ids = {r.id for r in records}
    assert {"p-100", "p-101", "p-102", "c-1", "c-2"} <= ids


def test_every_record_is_valid():
    for r in load_records(DATA):
        assert isinstance(r, Record)
        assert r.id and r.name
        assert r.price >= 0
