"""Statistics helpers."""


def mean(xs):
    return sum(xs) / len(xs)


def median(xs):
    s = sorted(xs)
    return s[len(s) // 2]


def variance(xs):
    """Population variance."""
    m = mean(xs)
    return sum(x - m for x in xs) / len(xs)
