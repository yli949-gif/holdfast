from widgets import make_widget


def test_make_widget():
    assert make_widget("a")["name"] == "a"
