"""Service configuration: read and write config.toml."""
import tomllib


def load_config(path: str = "config.toml") -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def save_config(cfg: dict, path: str = "config.toml") -> None:
    """Write ``cfg`` to ``path`` as TOML so that load_config(path) == cfg."""
    # TODO: serialize with tomli_w (see requirements.txt)
    raise NotImplementedError("save_config")
