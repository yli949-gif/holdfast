# app config

Per-environment settings live in `config/<env>.yaml` (a flat `key: value` subset of YAML, parsed by
`src/app/config.py` without third-party packages). `config/prod.yaml` is deployed as-is to production.

Run `pytest`.
