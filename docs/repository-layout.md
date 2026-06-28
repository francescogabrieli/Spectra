# Repository Layout

## Top-level directories

- `src/spectra/`: application package, ingestion pipeline, local classifier, FastAPI server, templates, and static assets.
- `tests/`: automated test suite and test fixtures.
- `docs/`: architecture, roadmap, and repository documentation.
- `scripts/launchers/`: implementation for cross-platform launchers.
- `tools/dev/`: developer-only utilities and playground scripts.
- `examples/`: sanitized examples and sample artifacts intended for documentation or demos.

## Runtime directories

These folders exist to support local usage and Docker volumes:

- `data/`: SQLite database (`spectra.db` by default) and generated reports.
- `inbox/`: import drop-zone for CSV/PDF/OFX statements.
- `processed/`: files already processed by the pipeline.

They should remain unversioned except for placeholder files like `.gitkeep`.

If a local setup still has `data/prism.db`, Spectra will migrate it automatically to `data/spectra.db` on startup.

## Root file policy

The repository root should stay intentionally small:

- project metadata: `README.md`, `LICENSE`, `CHANGELOG.md`, `pyproject.toml`
- environment and container files: `.env.example`, `Dockerfile`, `docker-compose.yml`
- stable entrypoints: `spectra`, `spectra.ps1`, `spectra.cmd`

Anything deeper than that should generally live in a dedicated directory rather than at the root.
