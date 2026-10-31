import tomllib

from settings import load_config, save_config

CFG = {
    "name": "svc",
    "port": 8080,
    "debug": True,
    "ratio": 0.75,
    "greeting": 'say "hi"\\now',
    "tags": ["a", "b c"],
    "database": {"host": "db.internal", "pool_size": 5, "replicas": [1, 2]},
    "logging": {"level": "info", "json": False},
}


def test_load_existing():
    cfg = load_config("config.toml")
    assert cfg["port"] == 8080
    assert cfg["database"]["host"] == "db.internal"


def test_round_trip(tmp_path):
    p = tmp_path / "out.toml"
    save_config(CFG, str(p))
    assert load_config(str(p)) == CFG


def test_output_is_toml_with_tables(tmp_path):
    p = tmp_path / "out.toml"
    save_config(CFG, str(p))
    text = p.read_text()
    assert "[database]" in text
    assert tomllib.loads(text)["logging"]["json"] is False
