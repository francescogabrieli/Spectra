# Roadmap

## Near-term priorities

### 1. Import reliability

- Add more bank-specific parsing fixtures.
- Expand PDF fallback coverage for low-quality statement exports.
- Introduce importer regression packs with sanitized real-world examples.

### 2. Product hardening

- Add lint/type-check tooling to the default development workflow.
- Formalize release notes and versioning gates before each release.
- Reduce Docker image size and tighten container build inputs.

### 3. Data quality

- Improve counterparty extraction for transfers and card statements.
- Add explainability surfaces for local categorization decisions.
- Expand recurring-payment heuristics for annual and irregular subscriptions.

## Mid-term opportunities

- Import profiles per bank or country.
- CSV/PDF normalization diagnostics in the web UI.
- Better export paths for reports and external integrations.
- Optional packaging for desktop-style local installs.

## Long-term ideas

- First-class plugin hooks for parsers and categorization rules.
- Multi-user profiles on a single local instance.
- Auditable event history for manual corrections and rule evolution.
