from api.handlers import create_post, get_post_url


def test_create_post_basic():
    p = create_post("Hello World")
    assert p["slug"] == "hello-world"
    assert get_post_url(p) == "/posts/hello-world"
