"""Load catalog records from JSON files under a data directory."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

REQUIRED = ("id", "name", "price")


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class Record:
    id: str
    name: str
    price: float


def _validate(raw: dict, source: Path) -> Record:
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise SchemaError(f"{source}: record missing {missing}")
    return Record(id=str(raw["id"]), name=str(raw["name"]), price=float(raw["price"]))


def load_records(data_dir: str | Path) -> list[Record]:
    """Return every record found in any *.json file under ``data_dir`` (recursively)."""
    data_dir = Path(data_dir)
    out: list[Record] = []
    for path in sorted(data_dir.rglob("*.json")):
        with open(path) as fh:
            payload = json.load(fh)
        for raw in payload["records"]:
            out.append(_validate(raw, path))
    return out
