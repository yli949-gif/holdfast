"""HTTP JSON client."""


class FetchError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def get_json(url: str, timeout: float = 5) -> dict:
    """GET ``url`` and return the decoded JSON body.

    Raises FetchError(status) for non-2xx responses.
    """
    raise NotImplementedError("TODO")
