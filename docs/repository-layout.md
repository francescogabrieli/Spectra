# Repository layout

The project is a Python monorepo with four independently runnable local HTTP services. Compose
currently builds one image from this repository and runs a separate container per service; the
process and storage boundaries are enforced by runtime APIs, roles, and volume ownership.

```text
src/spectra/
  services/
    contracts.py             Versioned request and response models
    clients.py               Typed HTTP clients; no cross-service DB access
    ledger/                   Canonical financial store owner and restore utility
    importer/                 Parse/review/commit orchestration and staging store
    enrichment/               Categorization, FX, and regenerable cache
  web/server.py               Gateway/UI routes and templates
  launcher.py                 Native four-process launcher
  pipeline.py                 CLI adapter using the service contracts
  db.py                       Ledger-owned SQLite schema and domain operations
  import_service.py           Reusable, side-effect-light import preparation
tests/
  test_services_integration.py  Synthetic HTTP tests against separate processes
docs/                            Architecture, currency safety, roadmap
docker-compose.yml               Four-process local topology
Dockerfile                       Shared install image for the four process commands
```

## Runtime data ownership

- `DB_PATH` defaults to `data/spectra.db`; only the ledger opens it, runs additive migrations,
  performs the legacy `prism.db` migration, and commits financial changes.
- `IMPORT_DB_PATH` defaults to `data/imports.sqlite`; only importer stores expiring review jobs.
- `FX_DB_PATH` defaults to `data/fx-rates.sqlite`; only enrichment stores reusable exchange rates.
- Backups contain the canonical ledger only. Import staging expires after seven days and FX cache
  can be regenerated, so Spectra does not claim a transactionally consistent snapshot of all
  process state.

The browser gateway and CLI adapters use named, versioned ledger APIs/read models; they do not
open SQLite and cannot send arbitrary SQL. The internal ledger-operation allowlist remains a
compatibility seam for the existing UI domain commands while read models and atomic import commit
remain ledger-owned.

## Runtime directories

`data/`, `inbox/`, and `processed/` are local runtime folders. Docker Compose mounts the financial
`data/` folder only into the ledger; it gives importer and enrichment separate staging/cache
volumes. `.dockerignore` excludes environment files, the local data trees, and credential files
from the image build context.

See [Local service architecture](architecture.md) for process startup, API flows, and backup/restore
commands.
