# Bank statement import into Sure (self-hosted)

Imports ACLEDA-style `.xlsx` bank statements into a self-hosted Sure
(we-promise/sure) instance via its REST API, triggered from n8n by a
Telegram file upload.

## How it works

1. **Parsing** (`parse_raw_name` / `parse_statement` in
   `bank_statement_import.py`) reads the statement's `Date`, `Transaction
   Details`, `Money In`, `Money Out`, and `Ccy` columns with regex — no
   LLM, no network call. It extracts the merchant/payee name and a REF#
   (or a hash fallback if no REF# is present) from the free-text
   "Transaction Details" column.
2. **Import** posts each row straight to Sure's `POST
   /api/v1/transactions` API — no intermediate database.
3. **Dedup** is handled by Sure itself: every transaction is created with
   `external_id` = the statement's REF# and a fixed `source` namespace.
   Re-running the script on an overlapping date range re-sends the same
   `external_id` for those rows; Sure returns the existing transaction
   (HTTP 200) instead of creating a duplicate (HTTP 201). No local dedup
   table needed.
4. **Merchant name cleanup + category assignment** is left to Sure. As of
   v0.6.6, Sure's own background enrichment reads transaction notes to
   determine merchant/category (when AI is enabled for the family in
   Sure's own settings) — so the raw statement text is passed through in
   the `notes` field and this script does not call any LLM itself. If you
   want AI categorization, turn it on in Sure's own family settings; this
   script doesn't need to know about it either way.
5. **n8n workflow** (`n8n_statement_import_workflow.json`): Telegram file
   upload -> download to disk -> run this script -> reply with an import
   summary.

## Setup

### 1. Get a Sure API key

In Sure, go to your account settings and generate an API key with
`read_write` scope. Sure auth uses the `X-Api-Key` header.

### 2. Find your account

Either note the account's UUID from Sure's UI/API, or just use its exact
display name — the script will look it up via `GET /api/v1/accounts`.

### 3. Set environment variables

Required:
- `SURE_API_URL` — base URL of your Sure instance, e.g.
  `https://sure.example.com` (no trailing slash)
- `SURE_API_KEY` — the API key from step 1
- `SURE_ACCOUNT_ID` — UUID of the account to import into
  — **or** —
- `SURE_ACCOUNT_NAME` — exact account name (used only if
  `SURE_ACCOUNT_ID` is not set)

Optional:
- `SURE_IMPORT_SOURCE` — `external_id` namespace used for dedup, default
  `acleda_statement_import`. Only change this if you need statements from
  different sources to be deduped independently.

### 4. Run it

```bash
pip install openpyxl
SURE_API_URL=https://sure.example.com \
SURE_API_KEY=sk_... \
SURE_ACCOUNT_NAME="ACLEDA Main" \
python3 bank_statement_import.py /path/to/statement.xlsx
```

Prints a JSON summary to stdout:

```json
{"rows_imported": 12, "rows_skipped_dupe": 3, "rows_failed": 0}
```

If any rows fail (e.g. a validation error from Sure), `rows_failed` is
non-zero and an `errors` array (capped at 10) is included with each
failing row's REF# and HTTP status.

### 5. Wire into n8n

Import `n8n_statement_import_workflow.json` into n8n. Set
`SURE_API_URL`, `SURE_API_KEY`, and `SURE_ACCOUNT_ID` (or
`SURE_ACCOUNT_NAME`) either in the n8n process environment or inline in
the "Run Import Script" node's Environment Variables option. Point the
`command` at wherever you deploy `bank_statement_import.py` on the VPS
(e.g. `/opt/scripts/bank_statement_import.py`), and set up the Telegram
Bot credential for the trigger/reply nodes.

## Files

- `bank_statement_import.py` — parser + Sure API importer (this is the
  whole implementation; no external services besides Sure itself)
- `n8n_statement_import_workflow.json` — n8n workflow: Telegram upload ->
  parse -> call Sure API -> Telegram reply with summary
- `schema.sql` — **unused**, left over from an earlier draft that wrote
  to a parallel Postgres database instead of going through Sure's API.
  Not referenced by anything; safe to delete.

## Sample statement format

The source `.xlsx` has columns: Date, Transaction Details, Money In, Ccy,
Money Out, Ccy, Balance, Ccy, with the merchant/payee and a REF# embedded
inside "Transaction Details" as free text (patterns like "PURCHASE AT X
ON ...", "FUNDS TRANSFERRED TO X ...", "FUNDS RECEIVED FROM X (...)").
Data rows start at row 4 (rows 1-3 are header/title rows in the ACLEDA
export).

## Reference: relevant Sure API endpoints

- `GET /api/v1/accounts` — list accounts, used to resolve
  `SURE_ACCOUNT_NAME` to an ID
- `POST /api/v1/transactions` — create a transaction; `nature` (`income`
  / `expense`) determines sign, `external_id` + `source` give built-in
  idempotency
- Full contract: `docs/api/transactions.md`, `docs/api/openapi.yaml` in
  the we-promise/sure repo
