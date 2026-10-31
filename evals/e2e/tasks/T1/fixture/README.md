# catalog

Tiny product-catalog loader used by the storefront.

* `src/catalog/loader.py` loads every JSON file under `data/` and validates each record.
* `data/` holds the live catalog (schema v2: `id`, `name`, `price`).
* `data/old/` is an **archive** of v1 exports (`sku`, `title`, `price_cents`). We keep
  them around for the finance team; they are not part of the live catalog.

Run the tests with `pytest`.
