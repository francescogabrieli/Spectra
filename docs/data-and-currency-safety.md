# Data, currencies, and local operation

Spectra stores imported transactions in SQLite on the machine running the app. The default
categorizer is local. OpenAI and Gemini categorization, Google Sheets synchronization, and
exchange-rate lookup are optional integrations; ordinary parsing, review, local saving, and
exports do not require AI or Sheets credentials.

## Amounts and currency changes

Each transaction stores the denomination of its saved amount, an exact decimal text value, and
currency minor units alongside the legacy `REAL` column retained for compatibility. Conversions
keep the original amount and currency, conversion rate/date/provider, and any failure reason.
Dashboard, budget, trend, and recurring totals filter to the selected base currency and sum the
exact decimal values before converting to JSON numbers. Rows in other or unknown denominations
remain visible in transaction history and exports and are counted separately in aggregate views.

Changing the base-currency preference affects future imports and presentation totals. It does not
rewrite historical amounts or convert older rows. Budgets also store a currency and an exact
decimal limit; when the base currency changes, limits in another or unknown denomination are
excluded and shown as a count rather than being relabeled.

When an import needs a currency conversion, Spectra uses a validated historical rate. Rates are
cached by date, source currency, and target currency in SQLite, with effective date and provider
provenance. If no cached rate is available and the rate service cannot be reached, the transaction
keeps its source amount and denomination and is marked for review. It is never labeled as if the
conversion had succeeded. Local-only operation therefore remains usable offline, but a first-time
cross-currency conversion may wait for connectivity and then require review.

## Historical database migration

Database migrations add columns and tables in place. A pre-currency database cannot prove the
denomination of old amounts, so those rows are preserved with an unknown-currency review state;
the current preference is not used to guess. They stay available in Transactions and exports but
are excluded from currency-specific totals.

Open **Settings → Confirm historical currencies** to review affected rows and explicitly assign
each currency. For currencies whose minor-unit precision would change the historical amount,
provide a corrected exact amount as part of that confirmation. Confirmation is recorded as
user-provided metadata. Do not confirm a currency based only on the current base-currency setting.

Before updating an existing installation, create a SQLite-aware snapshot of the canonical ledger:

```bash
uv run spectra --backup-db /safe/location/spectra-backup.db
```

The command uses SQLite's online backup API, checks database integrity, creates the backup with
owner-only permissions, and refuses to overwrite an existing destination. In Docker, only the
ledger's financial `data/` volume is part of this snapshot. Importer review drafts and the FX
cache have separate stores and are deliberately excluded: a staged but unconfirmed review may
need to be uploaded again after staging is lost, while FX rates can be regenerated. The import
service retains jobs for up to seven days. This is a canonical-ledger backup, not a coordinated
snapshot across processes.

Restore stops all four services first and requires the configured destination database not to
exist. Preserve the existing ledger file and any SQLite `-wal`/`-shm` sidecars before restoring;
the restore command refuses to overwrite an existing destination or run while known local
service ports accept connections:

```bash
mv data/spectra.db data/spectra.db.before-restore
uv run spectra --restore-db /safe/location/spectra-backup.db
```

If sidecars exist, stop services and preserve them alongside the original database before
restoring. The native restore leaves the rest of `data/` (including regenerable caches and other
local files) in place. For Docker, stop the stack and run the ledger's one-shot maintenance
command with a read-only mounted backup directory, after preserving/moving the current DB and
sidecars:

```bash
docker compose stop
docker compose run --rm --no-deps \
  -v /path/to/backups:/restore:ro ledger \
  python -m spectra.services.ledger.restore \
  /restore/spectra-backup.sqlite --destination /app/data/spectra.db
docker compose up -d
```

Never copy a live SQLite database file alone while it may be open; use the command or a
SQLite-aware backup utility.

## Network binding and logs

Native runs bind to `127.0.0.1` unless `--host` is supplied. Docker Compose maps the port to
`127.0.0.1` by default. Set `SPECTRA_HOST` explicitly to a host interface when remote access is
intended. The application has no authentication, so a non-loopback bind exposes financial pages
to clients that can reach that interface.

Routine parser and AI logs omit account/routing details, amounts, prompts, and model responses.
Uploaded statements are staged in temporary storage during processing and removed when the
worker finishes. Upload size is bounded by `MAX_UPLOAD_BYTES` (25 MiB by default; 100 MiB maximum).

## Precision boundary

Parser, conversion, transaction persistence, budget persistence, and the core web aggregates use
currency-aware `Decimal` values. Legacy SQLite `REAL` columns and compatibility JSON numeric
fields remain for existing clients, so consumers that need exact values should use the exported
`amount_decimal`, `amount_minor`, and currency fields. Budget configuration is currently scoped
to one base currency at a time; changing the preference leaves the previous currency's limit
stored and visible as excluded metadata rather than converting it.

The optional Google Sheets dashboard and Trends tables use the transaction `Currency` column to
include only rows matching the selected base currency; rows with a different or blank currency
are omitted instead of being combined or relabeled. Existing budget headings such as
`Monthly Budget (€)` are treated as EUR, and are ignored when another base currency is selected.
New budget tabs include the selected ISO currency code in the heading. Aggregation happens with
decimal arithmetic before values are written, but Google Sheets numeric cells and chart inputs
use the spreadsheet service's floating-point representation; use SQLite's exact decimal exports
when exact values are required.
