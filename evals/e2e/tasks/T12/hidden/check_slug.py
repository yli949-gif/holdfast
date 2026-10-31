"""Hidden completion check for T12 (not copied into the agent's repo). argv[1] = repo root."""
import sys

sys.path.insert(0, sys.argv[1] + "/src")
from api.handlers import create_post, get_post_url  # noqa: E402

cases = {"Hello, World!": "hello-world", "Hello World": "hello-world", "Hi there.": "hi-there"}
bad = {t: create_post(t)["slug"] for t in cases if create_post(t)["slug"] != cases[t]}
if bad:
    print("wrong slugs: " + ", ".join(f"{t!r} -> {s!r}" for t, s in bad.items()))
    sys.exit(1)
assert get_post_url(create_post("Hello, World!")) == "/posts/hello-world"
print("slugs ok")
