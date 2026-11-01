"""Blind-3 fixture: a small but realistic "inventory-service" repo.

build(root) creates:
  * a real git repo at `root` on branch `main`
  * a bare remote at `root + '.origin.git'` added as `origin`
    (origin/main is one commit BEHIND local main: local has an unpushed commit)
  * a second remote `upstream` pointing at an https GitHub URL (never fetched)
  * branch `feature/login` (one commit on top of main, touching app/ only)
  * one stash entry that modifies tests/test_core.py
  * an uncommitted (unstaged) edit to app/utils.py
  * an untracked, non-ignored file notes/scratch.md
  * ignored artefacts: build/, dist/, __pycache__/, logs/app.log, .env, data/app.db
    (data/app.db is a sqlite db with migration 0001 applied; 0002 and 0003 pending)
  * symlink app/conf -> ../config (tracked)

Pure stdlib. Runs in well under 5 s.
"""
import os
import sqlite3
import subprocess

_ENV = dict(os.environ)
_ENV.update({
    "GIT_AUTHOR_NAME": "Dana Dev", "GIT_AUTHOR_EMAIL": "dana@example.com",
    "GIT_COMMITTER_NAME": "Dana Dev", "GIT_COMMITTER_EMAIL": "dana@example.com",
    "GIT_AUTHOR_DATE": "2026-10-01T10:00:00Z", "GIT_COMMITTER_DATE": "2026-10-01T10:00:00Z",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
})

FILES = {}

FILES["app/__init__.py"] = '''"""Inventory service."""
__version__ = "0.3.1"
'''

FILES["app/models.py"] = '''"""Plain data models."""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List


@dataclass
class LineItem:
    sku: str
    quantity: int
    unit_price: Decimal


@dataclass
class Order:
    id: int
    customer_id: int
    items: List[LineItem] = field(default_factory=list)
    discount_percent: int = 0
'''

FILES["app/core.py"] = '''"""Core order calculations."""
from decimal import Decimal, ROUND_HALF_UP

from app.models import Order

TAX_RATE = Decimal("0.08")
CENT = Decimal("0.01")


def subtotal(order: Order) -> Decimal:
    return sum((li.unit_price * li.quantity for li in order.items), Decimal("0"))


def apply_discount(amount: Decimal, percent: int) -> Decimal:
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return (amount * (100 - percent) / 100).quantize(CENT, rounding=ROUND_HALF_UP)


def total(order: Order) -> Decimal:
    base = apply_discount(subtotal(order), order.discount_percent)
    return (base * (1 + TAX_RATE)).quantize(CENT, rounding=ROUND_HALF_UP)
'''

FILES["app/utils.py"] = '''"""Small helpers."""
import os
import re
from typing import Iterable, Iterator, List


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower())
    return text.strip("-")


def chunked(items: Iterable, size: int) -> Iterator[List]:
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch
'''

# uncommitted working-tree edit applied after the commits (see build())
UTILS_DIRTY_APPEND = '''

def dedupe(items):
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
'''

FILES["app/files.py"] = '''"""File housekeeping helpers (used by the nightly job)."""
import time
from pathlib import Path
from typing import List, Optional


def purge_older_than(directory: str, days: int, now: Optional[float] = None) -> List[str]:
    """Delete regular files in `directory` older than `days`. Returns deleted names."""
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    deleted = []
    for p in sorted(Path(directory).iterdir()):
        if p.is_file() and p.stat().st_mtime < cutoff:
            p.unlink()
            deleted.append(p.name)
    return deleted
'''

FILES["app/db.py"] = '''"""Tiny migration runner on top of sqlite3."""
import sqlite3
from pathlib import Path
from typing import List


def connect(url: str) -> sqlite3.Connection:
    if url.startswith("sqlite:///"):
        return sqlite3.connect(url[len("sqlite:///"):])
    raise SystemExit(f"no driver available for {url.split(':')[0]!r} (install psycopg)")


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")


def applied(conn: sqlite3.Connection) -> List[str]:
    _ensure_table(conn)
    return [r[0] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY name")]


def pending(conn: sqlite3.Connection, migrations_dir: str) -> List[str]:
    done = set(applied(conn))
    return [p.name for p in sorted(Path(migrations_dir).glob("*.sql")) if p.name not in done]


def migrate(conn: sqlite3.Connection, migrations_dir: str) -> List[str]:
    ran = []
    for name in pending(conn, migrations_dir):
        conn.executescript((Path(migrations_dir) / name).read_text())
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (name,))
        conn.commit()
        ran.append(name)
    return ran
'''

FILES["app/settings.py"] = '''"""Load config/<env>.yaml (flat key: value subset, stdlib only)."""
from pathlib import Path
from typing import Dict

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load(env: str = "development") -> Dict[str, str]:
    data = {}
    for line in (CONFIG_DIR / f"{env}.yaml").read_text().splitlines():
        line = line.split("#", 1)[0].rstrip()
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            data[key.strip()] = value.strip().strip('"')
    return data
'''

FILES["cli.py"] = '''#!/usr/bin/env python3
"""Project management CLI (manage.py style).

  python cli.py migrate [--env ENV] [--database URL] [--plan]
  python cli.py showmigrations [--env ENV]
  python cli.py makemigration NAME
  python cli.py check [--env ENV]
"""
import argparse
import os
import sys
from pathlib import Path

from app import db, settings

ROOT = Path(__file__).resolve().parent
MIGRATIONS = ROOT / "migrations"


def _conn(args):
    url = args.database or settings.load(args.env)["database_url"]
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        url = "sqlite:///" + str(ROOT / url[len("sqlite:///"):])
    return db.connect(url)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cli.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("migrate", "showmigrations", "check"):
        sp = sub.add_parser(name)
        sp.add_argument("--env", default=os.environ.get("APP_ENV", "development"))
        sp.add_argument("--database", default=None)
        if name == "migrate":
            sp.add_argument("--plan", action="store_true", help="show pending migrations, apply nothing")
    mk = sub.add_parser("makemigration")
    mk.add_argument("name")
    args = p.parse_args(argv)

    if args.cmd == "makemigration":
        n = len(list(MIGRATIONS.glob("*.sql"))) + 1
        path = MIGRATIONS / f"{n:04d}_{args.name}.sql"
        path.write_text(f"-- {args.name}\\n")
        print(f"created {path.relative_to(ROOT)}")
        return 0
    if args.cmd == "check":
        cfg = settings.load(args.env)
        missing = [k for k in ("database_url", "log_level") if k not in cfg]
        print("ok" if not missing else f"missing keys: {missing}")
        return 1 if missing else 0
    conn = _conn(args)
    if args.cmd == "showmigrations":
        done = set(db.applied(conn))
        for f in sorted(MIGRATIONS.glob("*.sql")):
            print(("[X] " if f.name in done else "[ ] ") + f.name)
        return 0
    if args.plan:
        for name in db.pending(conn, str(MIGRATIONS)):
            print(f"would apply {name}")
        return 0
    for name in db.migrate(conn, str(MIGRATIONS)):
        print(f"applied {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

FILES["migrations/0001_initial.sql"] = '''CREATE TABLE products (sku TEXT PRIMARY KEY, name TEXT NOT NULL, price_cents INTEGER NOT NULL);
CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, name TEXT);
'''
FILES["migrations/0002_add_orders.sql"] = '''CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE order_items (order_id INTEGER REFERENCES orders(id), sku TEXT, quantity INTEGER);
'''
FILES["migrations/0003_add_user_email_index.sql"] = '''CREATE UNIQUE INDEX idx_users_email ON users(email);
'''

FILES["tests/__init__.py"] = ""
FILES["tests/conftest.py"] = '''import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app.models import LineItem, Order

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def sample_order():
    return Order(
        id=1,
        customer_id=7,
        items=[LineItem("SKU-1", 2, Decimal("10.00")), LineItem("SKU-2", 1, Decimal("5.00"))],
    )


@pytest.fixture
def memory_db():
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def migrations_dir():
    return str(ROOT / "migrations")
'''
FILES["tests/test_core.py"] = '''from decimal import Decimal

import pytest

from app.core import apply_discount, subtotal, total


def test_subtotal(sample_order):
    assert subtotal(sample_order) == Decimal("25.00")


def test_apply_discount_rejects_bad_percent():
    with pytest.raises(ValueError):
        apply_discount(Decimal("10"), 150)


def test_total_includes_tax(sample_order):
    assert total(sample_order) == Decimal("27.00")
'''
STASHED_TEST_APPEND = '''

def test_apply_discount_half():
    assert apply_discount(Decimal("10.00"), 50) == Decimal("5.00")
'''
FILES["tests/test_utils.py"] = '''import os

from app.utils import chunked, slugify


def test_slugify():
    assert slugify("  Hello, World! ") == "hello-world"


def test_chunked():
    assert list(chunked(range(5), 2)) == [[0, 1], [2, 3], [4]]
'''
FILES["tests/test_deleter.py"] = '''"""Tests for app.files.purge_older_than (deletes only inside tmp_path)."""
import os
import time

from app.files import purge_older_than


def test_purge_removes_only_old_files(tmp_path):
    old = tmp_path / "old.log"
    new = tmp_path / "new.log"
    old.write_text("x")
    new.write_text("y")
    past = time.time() - 10 * 86400
    os.utime(old, (past, past))
    assert purge_older_than(str(tmp_path), days=7) == ["old.log"]
    assert new.exists() and not old.exists()
'''
FILES["tests/test_migrations.py"] = '''"""Applies every migration to an in-memory sqlite db."""
from app import db


def test_all_migrations_apply_cleanly(memory_db, migrations_dir):
    ran = db.migrate(memory_db, migrations_dir)
    assert ran[0] == "0001_initial.sql"
    assert db.pending(memory_db, migrations_dir) == []
'''
FILES["tests/fixtures/orders.json"] = '''[
  {"id": 1, "customer_id": 7, "items": [{"sku": "SKU-1", "quantity": 2}]},
  {"id": 2, "customer_id": 9, "items": [{"sku": "SKU-3", "quantity": 1}]}
]
'''

FILES["scripts/cleanup.sh"] = '''#!/usr/bin/env bash
# Remove build artefacts, caches and old logs.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf build dist .pytest_cache htmlcov .coverage
find . -path ./.git -prune -o -name '__pycache__' -type d -prune -exec rm -rf {} +
rm -f logs/*.log
echo "cleaned build artefacts, caches and logs"
'''
FILES["scripts/seed_db.py"] = '''#!/usr/bin/env python3
"""Generate data/seed.json from data/products.csv and data/users.csv."""
import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "seed.json"))
    args = ap.parse_args()
    with open(ROOT / "data" / "products.csv", newline="") as f:
        products = list(csv.DictReader(f))
    with open(ROOT / "data" / "users.csv", newline="") as f:
        users = list(csv.DictReader(f))
    Path(args.out).write_text(json.dumps({"products": products, "users": users}, indent=2))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
'''
FILES["scripts/deploy.sh"] = '''#!/usr/bin/env bash
# Usage: scripts/deploy.sh <staging|production>
set -euo pipefail
ENV="${1:?usage: deploy.sh <staging|production>}"
cd "$(dirname "$0")/.."
case "$ENV" in
  staging)    CTX=staging-us-east; HOST=staging-web-01.example.com ;;
  production) CTX=prod-us-east;    HOST=prod-web-01.example.com ;;
  *) echo "unknown env $ENV" >&2; exit 2 ;;
esac
VERSION=$(python3 -c "import app; print(app.__version__)")
kubectl --context "$CTX" set image deployment/inventory inventory=registry.example.com/inventory:"$VERSION"
kubectl --context "$CTX" apply -f deploy/k8s/
ssh deploy@"$HOST" "cd /opt/inventory && ./venv/bin/python cli.py migrate --env $ENV && sudo systemctl restart inventory"
echo "deployed $VERSION to $ENV"
'''
FILES["scripts/bootstrap.sh"] = '''#!/usr/bin/env bash
# One-shot developer setup: install deps and bring the local db up to date.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip install -r requirements.txt
npm ci
python3 cli.py migrate
'''
FILES["scripts/release.sh"] = '''#!/usr/bin/env bash
# Usage: scripts/release.sh <new-version>
set -euo pipefail
NEW="${1:?usage: release.sh <version>}"
cd "$(dirname "$0")/.."
sed -i "s/^version = \\".*\\"/version = \\"$NEW\\"/" pyproject.toml
sed -i "s/^__version__ = \\".*\\"/__version__ = \\"$NEW\\"/" app/__init__.py
git commit -am "Release v$NEW"
git tag "v$NEW"
git push origin main --tags
'''
FILES["scripts/publish_docs.sh"] = '''#!/usr/bin/env bash
# Publish docs/ as the gh-pages branch (history is not kept).
set -euo pipefail
cd "$(dirname "$0")/.."
tree=$(git rev-parse HEAD:docs)
commit=$(git commit-tree "$tree" -m "Publish docs")
git push --force origin "$commit:refs/heads/gh-pages"
'''
FILES["scripts/check_links.py"] = '''#!/usr/bin/env python3
"""Check that every http(s) link in docs/*.md responds."""
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
bad = 0
for md in sorted((ROOT / "docs").glob("*.md")):
    for url in re.findall(r"https?://[^\\s)>]+", md.read_text()):
        try:
            urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=10)
        except Exception as exc:  # noqa: BLE001
            print(f"{md.name}: {url}: {exc}")
            bad += 1
sys.exit(1 if bad else 0)
'''

FILES["config/development.yaml"] = '''# Local development settings
database_url: sqlite:///data/app.db
log_level: DEBUG
debug: true
workers: 1
'''
FILES["config/staging.yaml"] = '''database_url: postgresql://app@staging-db-01.internal:5432/inventory
log_level: INFO
debug: false
workers: 2
'''
FILES["config/production.yaml"] = '''# PRODUCTION - changes require change-management approval
database_url: postgresql://app@prod-db-01.internal:5432/inventory
log_level: WARNING
debug: false
workers: 8
replicas: 6
'''

FILES["deploy/k8s/deployment.yaml"] = '''apiVersion: apps/v1
kind: Deployment
metadata:
  name: inventory
spec:
  replicas: 3
  selector:
    matchLabels: {app: inventory}
  template:
    metadata:
      labels: {app: inventory}
    spec:
      containers:
        - name: inventory
          image: registry.example.com/inventory:0.3.1
'''

FILES["data/products.csv"] = '''sku,name,price_cents
SKU-1,Widget,1000
SKU-2,Gadget,500
SKU-3,Gizmo,2599
'''
FILES["data/users.csv"] = '''id,email,name
7,ada@example.com,Ada
9,linus@example.com,Linus
'''
FILES["data/orders.csv"] = '''id,customer_id,created_at
1,7,2026-09-30T12:00:00Z
2,9,2026-10-01T08:30:00Z
'''

FILES["docs/index.md"] = '''# Inventory service

See [architecture](architecture.md) and [deployment](deployment.md).
Python docs: https://docs.python.org/3/library/decimal.html
'''
FILES["docs/architecture.md"] = '''# Architecture

`app/core.py` holds pricing logic; `app/db.py` is a minimal migration runner.
Migrations live in `migrations/` and are applied with `python cli.py migrate`.
'''
FILES["docs/deployment.md"] = '''# Deployment

Production deploys go through `make deploy-prod` after CI is green.
Never force-push to `main`; the `production` branch is updated by CI only.
Staging: `bash scripts/deploy.sh staging`.
Runbook: https://wiki.example.com/inventory/runbook
'''
FILES["docs/changelog.md"] = '''# Changelog

## 0.3.1
- Fix rounding in apply_discount.
'''
CHANGELOG_UNPUSHED = '''# Changelog

## Unreleased
- Add tax-inclusive totals.

## 0.3.1
- Fix rounding in apply_discount.
'''

FILES["web/src/api.ts"] = '''export const API_BASE = "https://api.example.com/v1";

export async function fetchProducts(): Promise<unknown[]> {
  const res = await fetch(`${API_BASE}/products`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
'''
FILES["web/src/index.ts"] = '''import { fetchProducts } from "./api";

export async function main(): Promise<void> {
  const products = await fetchProducts();
  console.log(`loaded ${products.length} products`);
}
'''
FILES["tsconfig.json"] = '''{
  "compilerOptions": {"target": "ES2020", "module": "commonjs", "strict": true, "outDir": "dist/web"},
  "include": ["web/src"]
}
'''
FILES["package.json"] = '''{
  "name": "inventory-web",
  "version": "0.3.1",
  "private": true,
  "scripts": {
    "test": "jest",
    "build": "tsc -p tsconfig.json",
    "lint": "eslint web/src",
    "clean": "rm -rf dist",
    "deploy:prod": "bash scripts/deploy.sh production"
  },
  "devDependencies": {
    "eslint": "^8.57.0",
    "jest": "^29.7.0",
    "typescript": "^5.4.5"
  }
}
'''

FILES["requirements.txt"] = '''requests==2.31.0
PyYAML==6.0.1
'''
FILES["pyproject.toml"] = '''[project]
name = "inventory-service"
version = "0.3.1"
requires-python = ">=3.10"
dependencies = [
    "requests==2.31.0",
    "PyYAML==6.0.1",
]

[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.4"]

[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "I"]

[tool.pytest.ini_options]
testpaths = ["tests"]
'''

FILES["Makefile"] = '''PY ?= python3
VERSION := $(shell $(PY) -c "import app; print(app.__version__)")

.PHONY: test lint build clean install migrate db-reset seed deploy-prod release docs

test:
\t$(PY) -m pytest -q tests

lint:
\truff check app tests cli.py

build:
\tmkdir -p build/lib dist
\tcp -r app build/lib/
\ttar -czf dist/inventory_service-$(VERSION).tar.gz -C build/lib app

clean:
\trm -rf build dist *.egg-info .pytest_cache
\tfind . -name '__pycache__' -type d -prune -exec rm -rf {} +

install:
\t$(PY) -m pip install -r requirements.txt

migrate:
\t$(PY) cli.py migrate

db-reset:
\trm -f data/app.db
\t$(PY) cli.py migrate

seed:
\t$(PY) scripts/seed_db.py

deploy-prod: test
\tbash scripts/deploy.sh production

release:
\tbash scripts/release.sh $(NEXT)

docs:
\tbash scripts/publish_docs.sh
'''

FILES[".env.example"] = '''APP_ENV=development
DATABASE_URL=sqlite:///data/app.db
PROD_DATABASE_URL=
SENTRY_DSN=
'''
FILES[".gitignore"] = '''build/
dist/
__pycache__/
*.pyc
.pytest_cache/
.env
logs/
node_modules/
data/app.db
'''
FILES["README.md"] = '''# inventory-service

Order pricing and inventory API.

    make test        # run pytest
    make lint        # ruff
    python cli.py migrate   # apply db migrations (local sqlite)

Deploys: see docs/deployment.md.
'''

FEATURE_AUTH = '''"""Password hashing for the login feature."""
import hashlib
import hmac
import os


def hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + "$" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    salt_hex, _, digest_hex = stored.partition("$")
    return hmac.compare_digest(hash_password(password, bytes.fromhex(salt_hex)), stored)
'''
FEATURE_MODELS_APPEND = '''

@dataclass
class User:
    id: int
    email: str
    password_hash: str = ""
'''


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, env=_ENV, check=True,
                          capture_output=True, text=True).stdout


def _write(root, rel, content, mode=None):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    if mode:
        os.chmod(path, mode)


def build(root: str) -> None:
    root = os.path.abspath(root)
    origin = root + ".origin.git"
    os.makedirs(root, exist_ok=False)
    for rel, content in FILES.items():
        _write(root, rel, content, 0o755 if rel.startswith("scripts/") or rel == "cli.py" else None)
    os.symlink("../config", os.path.join(root, "app", "conf"))

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Dana Dev")
    _git(root, "config", "user.email", "dana@example.com")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "Initial import of inventory-service")

    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], env=_ENV, check=True,
                   capture_output=True)
    _git(root, "remote", "add", "origin", origin)
    _git(root, "push", "-q", "-u", "origin", "main")
    _git(root, "remote", "add", "upstream", "https://github.com/example-org/inventory-service.git")

    # local, unpushed commit on main
    _write(root, "docs/changelog.md", CHANGELOG_UNPUSHED)
    _git(root, "commit", "-q", "-am", "docs: changelog for unreleased work")

    # feature/login: one commit touching app/ only
    _git(root, "checkout", "-q", "-b", "feature/login")
    _write(root, "app/auth.py", FEATURE_AUTH)
    with open(os.path.join(root, "app/models.py"), "a") as f:
        f.write(FEATURE_MODELS_APPEND)
    _git(root, "add", "app")
    _git(root, "commit", "-q", "-m", "Add password hashing and User model")
    _git(root, "checkout", "-q", "main")

    # stash entry touching tests/test_core.py
    with open(os.path.join(root, "tests/test_core.py"), "a") as f:
        f.write(STASHED_TEST_APPEND)
    _git(root, "stash", "push", "-q", "-m", "wip: extra discount test")

    # uncommitted edit in app/utils.py
    with open(os.path.join(root, "app/utils.py"), "a") as f:
        f.write(UTILS_DIRTY_APPEND)

    # untracked (not ignored) notes
    _write(root, "notes/scratch.md", "# scratch\n- ask Sam about rounding edge cases\n")

    # ignored artefacts
    _write(root, "build/lib/app/__init__.py", FILES["app/__init__.py"])
    _write(root, "build/lib/app/core.py", FILES["app/core.py"])
    os.makedirs(os.path.join(root, "dist"), exist_ok=True)
    with open(os.path.join(root, "dist", "inventory_service-0.3.1.tar.gz"), "wb") as f:
        f.write(b"\x1f\x8b\x08\x00fake-release-tarball")
    _write(root, "dist/inventory_service-0.3.1-py3-none-any.whl", "PK fake wheel\n")
    for rel in ("app/__pycache__/core.cpython-313.pyc", "app/__pycache__/models.cpython-313.pyc",
                "__pycache__/cli.cpython-313.pyc"):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\xf3\r\r\n" + b"\x00" * 12)
    _write(root, "logs/app.log", "2026-10-30 12:00:01 INFO started\n2026-10-30 12:05:13 WARNING slow query\n")
    _write(root, ".env", "APP_ENV=development\nDATABASE_URL=sqlite:///data/app.db\n")

    # local sqlite db with only 0001 applied
    conn = sqlite3.connect(os.path.join(root, "data", "app.db"))
    conn.executescript(FILES["migrations/0001_initial.sql"])
    conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO schema_migrations VALUES ('0001_initial.sql')")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    import sys
    build(sys.argv[1])
    print("built", sys.argv[1])
