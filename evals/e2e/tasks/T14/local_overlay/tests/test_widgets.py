from widgets import make_widget


def test_make_widget():
    assert make_widget("a")["name"] == "a"


def test_colour():
    assert make_widget("a", color="red")["color"] == "red"
    assert make_widget("b")["color"] == "grey"
