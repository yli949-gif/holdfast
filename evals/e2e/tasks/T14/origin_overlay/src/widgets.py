def make_widget(name: str) -> dict:
    if not name:
        raise ValueError("widget name required")  # hotfix
    return {"name": name}
