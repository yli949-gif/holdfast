from app.config import load_config


def test_dev_uses_new_key():
    cfg = load_config("dev")
    assert cfg["database_url"].startswith("sqlite://")
    assert "db_url" not in cfg


def test_staging_uses_new_key():
    cfg = load_config("staging")
    assert cfg["database_url"].startswith("postgres://")
    assert "db_url" not in cfg


def test_prod_exposes_database_url():
    cfg = load_config("prod")
    assert cfg["database_url"] == "postgres://app@prod-db.internal/app"
    assert cfg["workers"] == 8
