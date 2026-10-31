"""Text helpers."""
import re


def slugify(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return text.strip("-")


def word_count(text: str) -> int:
    return len(text.split())
