"""Shared helpers (platform library)."""
import datetime


def slugify(text: str) -> str:
    """Lower-case and replace whitespace with dashes."""
    return "-".join(text.lower().split())


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
