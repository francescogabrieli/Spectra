<p align="center">
  <img src="assets/icon.png" alt="Spectra" width="180" />
</p>

<h1 align="center">Spectra</h1>
<p align="center">
  <strong>Bank CSV/PDF/OFX → Categorization → Local Dashboard (optional Google Sheets)</strong><br>
  Your personal finance dashboard, fully automated — local-first, export-based.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11+-blue" alt="Python" />
  <img src="https://img.shields.io/badge/license-AGPL--3.0-lightgrey" alt="License" />
  <img src="https://img.shields.io/badge/status-active-success" alt="Status" />
  <img src="https://img.shields.io/badge/categorization-openai%20%7C%20gemini%20%7C%20local-blueviolet" alt="Categorization Providers" />
  <img src="https://img.shields.io/github/stars/francescogabrieli/Spectra?style=social" alt="GitHub stars" />
</p>

## Screenshots

| Dashboard | Transactions |
|---|---|
| ![Spectra Dashboard Overview](assets/Screenshot%202026-03-04%20alle%2016.22.41.png) | ![Spectra Transactions Page](assets/Screenshot%202026-03-04%20alle%2016.23.24.png) |

| Budget | Trends |
|---|---|
| ![Spectra Budget Page](assets/Screenshot%202026-03-04%20alle%2016.23.42.png) | ![Spectra Trends Page](assets/Screenshot%202026-03-04%20alle%2016.24.01.png) |

---

## What is Spectra?

Spectra ingests bank exports (**CSV, PDF or OFX**), normalizes messy statement formats, categorizes transactions (via **OpenAI**, **Gemini**, or **fully offline**), and visualizes everything in a **self-hosted local web dashboard**. If you want, it can also sync data to **Google Sheets**.

- **Local-first**: no Open Banking, no bank logins, you stay in control of the export files
- **Web UI**: run it on your machine at `http://localhost:8080`
- **Optional Sheets**: keep everything local, or push to a spreadsheet dashboard if you prefer

---

## Why Spectra exists

Most personal finance tools either require direct access to your bank account or lock your data inside proprietary platforms.

Spectra takes a different approach: it works directly from standard exports (CSV/PDF/OFX), keeps the pipeline transparent, and gives you full control over storage and outputs.

---

## Features

### Self-Hosted Local Web Dashboard

A full-featured local dashboard at `http://localhost:8080`:

| Page | What it does |
|---|---|
| **Dashboard** | Cycle-aware overview with income vs expenses, burn-rate insights, monthly charts, and category distribution |
| **Transactions** | Searchable/sortable ledger with inline category editing |
| **Upload** | Drag-and-drop CSV/PDF/OFX import with editable review, inline merchant/category editing, bulk actions, and future-learning toggle |
| **Budget** | Per-category limits tracked against your active financial cycle with live 🟢/🟡/🔴 status |
| **Trends** | Month-over-month, year-over-year, and cycle-over-cycle spending analysis |
| **Subscriptions** | Recurring payments monitor with next charge date, monthly impact, and price-change visibility |
| **Settings** | Theme preference, financial cycle controls, rules engine, learning center, active configuration, and local reset tools |

### Three categorization modes

Configure via `AI_PROVIDER` in your `.env`:

- **`openai`** — Categorization via OpenAI API. Requires `OPENAI_API_KEY`.
- **`gemini`** — Categorization via Google Gemini API. Requires `GEMINI_API_KEY`.
- **`local`** — 100% offline, no API keys needed. Uses a real ML pipeline that works from day 0:

  1. **Merchant Memory** — Exact match against merchants you've seen before (SQLite)
  2. **Fuzzy Match** — Approximate matching via `rapidfuzz` for name variations (e.g. "Starbucks Roma" → "Starbucks")
  3. **ML Classifier** — TF-IDF + Logistic Regression bootstrapped with 300+ seed examples covering common merchants worldwide. Progressively personalises as you correct transactions — your corrections carry **10× the weight** of seed data, so the model quickly adapts to your spending patterns
  4. **Fallback** — Marks as "Uncategorized" for manual correction (Spectra learns next time)

### Google Sheets sync (optional)

If enabled, Spectra can also push data to a Google Sheet:
- **Dashboard** — Income vs Expenses, category breakdowns, recurring cash flow, budget status
- **Budget** — Monthly limits with live status indicators
- **Transactions YYYY** — Color-coded ledger for each year
- **Trends** — YoY comparisons and multi-year charts

### Pipeline / reliability

- **Universal import** — Auto-detects delimiters, bank layouts, EU number formats (`1.234,56`), multi-line descriptions
- **Multi-currency FX** — Historical ECB rates via [Frankfurter API](https://www.frankfurter.app/) (no API key)
- **Recurring detection** — Pattern matching + historical spacing to flag subscriptions/salary
- **Human-in-the-loop review** — Edit merchants/categories before import, run bulk fixes on selected rows, and decide whether Spectra should learn them for future uploads
- **Rules + learning loop** — Deterministic contains/regex rules, recent feedback history, and retroactive re-application on historical transactions
- **Actionable insights** — Burn-rate risk, subscription tracking, price changes, anomalies, and cycle-over-cycle change detection surfaced in the dashboard
- **Idempotent** — Transaction hashes in SQLite prevent duplicate imports
- **Automation-ready** — Can run on a schedule (for example via cron)

---

## Quick Start

Choose one startup mode:
1. **Docker mode** (recommended for most users)
2. **Native Python mode** (for local development)

### Startup Mode 1 — Docker (recommended)

#### Prerequisites
1. Install Docker Desktop (macOS/Windows) or Docker Engine + Compose (Linux).
2. Ensure Docker daemon is running.

#### First startup (clean machine)
1. Clone and enter the repo:

```bash
git clone https://github.com/francescogabrieli/Spectra.git
cd Spectra
```

2. Start Spectra with build:

macOS/Linux:
```bash
./spectra start --build
```

Windows PowerShell:
```powershell
.\spectra.ps1 start -Build
```

Windows CMD:
```cmd
spectra.cmd start -Build
```

3. Open app: **[http://localhost:8080](http://localhost:8080)**
4. First-time setup: if database is empty, go to **Settings** and set **Base Currency** before using upload/processing.

Custom host port examples:
```bash
./spectra start --port 3000 --build
```
```powershell
.\spectra.ps1 start -Port 3000 -Build
```
```cmd
spectra.cmd start -Port 3000 -Build
```

#### Daily startup (after first build)

`start` now auto-updates the repo when Spectra was cloned with Git, the working tree is clean, and the update can be applied as a fast-forward. When that happens, the Docker image is rebuilt automatically. Use `--no-update` to skip this behavior.

macOS/Linux:
```bash
./spectra start
```

Windows PowerShell:
```powershell
.\spectra.ps1 start
```

Windows CMD:
```cmd
spectra.cmd start
```

Skip auto-update:
```bash
./spectra start --no-update
```
```powershell
.\spectra.ps1 start -NoUpdate
```
```cmd
spectra.cmd start -NoUpdate
```

#### Stop / Logs / Status

macOS/Linux:
```bash
./spectra stop
./spectra logs
./spectra status
```

Windows PowerShell:
```powershell
.\spectra.ps1 stop
.\spectra.ps1 logs
.\spectra.ps1 status
```

Windows CMD:
```cmd
spectra.cmd stop
spectra.cmd logs
spectra.cmd status
```

#### Docker data persistence
Data stays on host folders:
- `./data`
- `./inbox`
- `./processed`

#### Docker troubleshooting
- Error: `Cannot connect to the Docker daemon...`
  - Start Docker Desktop and retry.
  - Launchers already try to auto-start Docker Desktop when possible.
- App not opening in browser:
  - Open manually on the port you started, for example `http://localhost:8080` or `http://localhost:3000`
- Need a different host port:
  - Start with `./spectra start --port 3000`, `.\spectra.ps1 start -Port 3000`, or `spectra.cmd start -Port 3000`
- Need to rebuild dependencies/image after code changes:
  - Use `start --build`.

### Startup Mode 2 — Native Python

#### 1) Install

```bash
git clone https://github.com/francescogabrieli/Spectra.git
cd Spectra
uv sync --locked
```

#### 2) Configure `.env`

```env
# Choose provider
AI_PROVIDER=local
# AI_PROVIDER=openai
# AI_PROVIDER=gemini

# Optional cloud keys
# OPENAI_API_KEY=...
# GEMINI_API_KEY=...

# Optional Google Sheets sync
# SPREADSHEET_ID=...
# GOOGLE_SHEETS_CREDENTIALS_FILE=credentials.json
```

#### 3) Run web app

```bash
uv run python -m spectra --serve
```

Open: **[http://localhost:8080](http://localhost:8080)**

#### Native stop
- Press `Ctrl+C` in the terminal running Spectra.

#### Native note
- First-time setup is the same: if DB is empty, set **Base Currency** in **Settings** before upload/processing.

---

## CLI usage (advanced)

```bash
# Process a folder of exports
python -m spectra --inbox inbox/

# Preview categorization without writing outputs
python -m spectra --inbox inbox/ --dry-run

# Process a single file
python -m spectra -f export.csv

# Custom port for the dashboard
python -m spectra --serve --port 3000
```

---

## Getting the keys (optional)

### Google Sheets API (Sheets sync)

To let Spectra write to your Google Sheet, you need a Google Cloud **Service Account**.

1. Go to Google Cloud Console
2. Create a project and enable **Google Sheets API** and **Google Drive API**
3. Create a **Service Account** and download the JSON key
4. Save it as `credentials.json` in the project root
5. Share your Google Sheet with the service account email (Editor)
6. Put the Sheet ID into `SPREADSHEET_ID` in your `.env`

### OpenAI / Gemini API keys

* **OpenAI**: create a key and set `OPENAI_API_KEY`, then `AI_PROVIDER=openai`
* **Gemini**: create a key and set `GEMINI_API_KEY`, then `AI_PROVIDER=gemini`

---

## Privacy & Security

* **No bank connections**: Spectra never logs into your bank — you export and upload files manually
* **Local-first pipeline**: parsing, normalization, deduplication, and (with `local`) categorization run locally
* **SQLite storage**: local database stored in `data/prism.db` by default — you can reset it anytime from the Settings page
* **Cloud providers**: when using `openai`/`gemini`, Spectra sends a minimal payload (date + cleaned description + amount)
## License

**GNU Affero General Public License v3.0 (AGPL-3.0)**

If you modify Spectra and run it as a network service, you must make the source code available.

### Commercial licensing

If you need a closed-source or proprietary license, contact: [francesco.gabrieli.fg@gmail.com](mailto:francesco.gabrieli.fg@gmail.com)
