# Contributing

## Development setup

```bash
git clone https://github.com/francescogabrieli/Spectra.git
cd Spectra
uv sync --locked
```

Run the app locally:

```bash
uv run python -m spectra --serve
```

Run the test suite:

```bash
uv run pytest
```

## Repository conventions

- Keep application code in `src/spectra/`.
- Keep automated tests in `tests/`.
- Put architectural and project documentation in `docs/`.
- Keep launcher implementation in `scripts/launchers/`; root launchers should stay as stable wrappers.
- Use `tools/dev/` for ad hoc developer utilities, prototypes, and playground scripts that are useful to keep in the repo.
- Treat `data/`, `inbox/`, and `processed/` as local runtime state, not as a place for versioned fixtures.

## Pull request checklist

- Update docs when behavior or structure changes.
- Prefer adding or updating tests alongside behavior changes.
- Do not commit `.env`, `credentials.json`, local databases, or real bank exports.
- Keep the root directory reserved for user-facing entrypoints and top-level project metadata.
