"""Load flat `key: value` YAML-style config files from config/<env>.yaml."""
from __future__ import annotations

from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


def _coerce(value: str):
    v = value.strip()
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v)
    except ValueError:
        return v


def parse(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = _coerce(value)
    return out


def load_config(env: str) -> dict:
    path = CONFIG_DIR / f"{env}.yaml"
    cfg = parse(path.read_text())
    return cfg
