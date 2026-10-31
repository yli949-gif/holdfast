"""Parse comma separated integer lists coming from CSV cells."""


def parse_ints(text: str) -> list[int]:
    """'1, 2,3' -> [1, 2, 3].  Blank fields (e.g. a trailing comma) are skipped."""
    return [int(part) for part in text.split(",")]
