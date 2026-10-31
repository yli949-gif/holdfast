"""HTTP handlers for posts."""
from core.util import now_iso, slugify


def create_post(title: str, body: str = "") -> dict:
    return {"title": title, "slug": slugify(title), "body": body, "created_at": now_iso()}


def get_post_url(post: dict) -> str:
    return f"/posts/{post['slug']}"
