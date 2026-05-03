# Changelog

All notable changes to Spectra are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Added
- Launcher auto-update on `start` for clean Git clones with fast-forwardable upstream changes; updated launches rebuild the Docker image automatically
- Upload review now surfaces local classification metadata for uncertain rows, including source badges, confidence, and clickable category suggestions
- Added generic `counterpart` parsing across imports, with support for dedicated payee/beneficiary columns and fallback extraction from detail/reference text
### Changed
- Local ML categorization now trains on weighted hybrid examples (`user overrides` > `merchant memory` > `history` > seed data) and uses richer TF-IDF word + char features
- Local classification decisions now require both confidence and top-1/top-2 margin before auto-assigning a category; uncertain rows stay reviewable instead of being forced aggressively
- Added `numpy` as an explicit runtime dependency because the local classifier now imports it directly
- Improved UX across the web app with a clearer Settings information architecture, guided upload review, and better transaction filter / inbox-zero feedback

---

## [0.4.0] — 2026-03-21

### Highlights
- One-command local startup with Docker
- OFX import support
- Better PDF parsing fallback
- First-run base currency setup in Settings
- Improved Docker launchers with real custom port support

### Added
- Docker support with `Dockerfile` and `docker-compose.yml` for quick local startup
- OFX file import support
- First-run setup flow for choosing the base currency before using the app

### Changed
- Replaced the old `pdfminer.six` dependency with `pypdf`
- PDF parsing now falls back to `pypdf` text extraction while keeping `pdfplumber` for table extraction
- Docker launchers now correctly support custom host ports
- Web assets are now packaged correctly for installable distributions
- Documentation updated and simplified around local-first startup

### Fixed
- Fixed packaging so the web dashboard templates and static assets are included correctly
- Fixed Docker `--port` behavior so launchers no longer open the wrong URL
- Cleaned up release artifacts and removed the old GitHub Actions workflow

### Notes
- If you’re starting fresh, the recommended path is Docker: `./spectra start --build`
- Native mode is still available for local development: `python -m spectra --serve`
- Spectra stays local-first: no bank logins, no mandatory cloud dependency, and full control over your data

---

## [0.3.0] — 2026-03-12

### Added
- **Human-in-the-loop upload review** — upload preview is now an editable table where each row can have its merchant name and category corrected inline before import
- **"Apply to Similar" bulk override** — correct one transaction and propagate the same category to all rows with the same merchant in a single click
- **Per-row "Learn" toggle** — choose per transaction whether a manual correction should train future categorization
- **Learning Center** (Settings page) — displays feedback event stats (total, future-learning, overrides, uncategorized remaining) and a feed of recent manual corrections; includes a "Reapply to history" action to re-categorize past transactions when rules or overrides evolve
- **Custom Rules engine UI** (Settings page) — renamed from "Categorization Rules" to "Your Custom Rules" with clarifying copy; empty state now explains that Spectra categorizes automatically without custom rules
- **Rule management** — toggle active/inactive per rule, reorder priority with ↑/↓ buttons, inline test panel showing historical impact before saving
- **Actionable insights panel** (Dashboard) — surfaces anomaly signals (unusual spend, new recurring, price changes) as linked cards
- **Price-change detection** (Subscriptions page) — badges and inline banner highlight subscriptions whose amount changed vs. prior period
- `httpx>=0.27.0` added as an explicit runtime dependency (used by `fx.py` for ECB rate fetching)
- Python 3.13 added to supported classifiers

### Changed
- Layout centering fixed across all pages — content now respects a `max-width: 1200px` container centered in the main area (`main-inner` wrapper)
- `architecture.md` updated to reflect web upload flow, Custom Rules engine, Human-in-the-loop layer, Learning Center, and expanded dashboard pages
- License corrected in `pyproject.toml` from MIT to AGPL-3.0-or-later (LICENSE file was always AGPL)

### Fixed
- All dashboard sub-pages had content shifted left due to missing `margin: auto` on the max-width container

---

## [0.2.0] — 2026-02-15

### Added
- **Full local mode** — categorization now runs entirely on-device with no cloud calls required when `AI_PROVIDER=local`
- **4-layer cascade** — merchant memory → fuzzy matching (rapidfuzz) → ML classifier → Uncategorized fallback
- **On-device ML classifier** — TF-IDF + Logistic Regression trained on ~300 seed examples, then learns from your own transaction history with 10× weight; confidence threshold ≥ 0.20
- **Local web dashboard** — browse transactions, categories, trends and budgets at `localhost:8080` (FastAPI + Jinja2)
  - Dashboard page with summary cards and charts
  - Transactions table with inline category editing
  - Budget monthly targets vs actuals
  - Trends month-over-month comparison
  - Subscriptions recurring detection
  - Settings (theme, cycle, AI provider, DB stats)
  - Upload drag-and-drop for CSV/PDF
- **Dark / light / auto theme** — follows system preference or manual override, persisted to server
- **Configurable financial cycle** — pay-day aware monthly cycle (1-28th of month)

### Changed
- OpenAI/Gemini still supported as alternative providers via `AI_PROVIDER=openai|gemini`

---

## [0.1.0] — initial release

### Added
- **CLI pipeline** — `python -m spectra` ingests bank exports (CSV/PDF), normalizes messy formats, and stores in SQLite
- **Optional LLM categorization** — OpenAI/Gemini via `AI_PROVIDER=openai|gemini` for cloud-based transaction categorization and merchant name cleaning
- **Idempotent imports** — SQLite + content hashes avoid duplicate transactions across repeated imports
- **Recurring detection** — tags subscriptions and recurring income based on temporal patterns and static criteria
- **Historical FX conversion** — multi-currency support via Frankfurter API (ECB rates, free, no API key required)
- **Google Sheets sync** — updates a multi-tab dashboard (Transactions, Dashboard, Trends); runs locally or via GitHub Actions
- **CSV and PDF support** — parses exports from any bank
- **AGPL-3.0** — fully open source

### Notes
- Core pipeline runs locally; LLM runs in the cloud depending on provider (no local ML yet)
