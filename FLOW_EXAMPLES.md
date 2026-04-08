# Bilt Transactions End-to-End Example Flow

This guide shows a full example workflow for:

1. Retrieving transactions from Bilt
2. Reviewing the output CSV format
3. Uploading that CSV to Empower
4. Recording `JSESSIONID` and `csrf` values
5. Viewing and deleting manual transactions from Empower

Run all commands from the repository root.

## 1) Retrieve Transactions from Bilt

Command:

```bash
python -m bilt.retrieve_transactions
```

Optional flags:

```bash
python -m bilt.retrieve_transactions \
  --output bilt/transactions_2026-03-01_2026-03-31.csv \
  --cache-file bilt/.bilt_token_cache.json \
  --page-size 100
```

What happens:

1. The script authenticates with Bilt (cached JWT, refresh token, then OTP if needed).
2. You choose a card from a numbered list.
3. You enter a start and end date.
4. The script writes a CSV file.

>*The script will remember your tokens in a cache file so after authenticating once, it will not ask you again for your phone number and OTP.*

Expected output filename pattern:

```text
transactions_<start-date>_<end-date>.csv
```

Real example in this repo:

```text
bilt/transactions_2026-02-01_2026-03-31.csv
```

## 2) Example Output CSV

Header and sample rows:

```csv
status,type,amount,currency,createdAt,merchant,category
SETTLED,PURCHASE,30.88,USD,2026-03-30T00:27:34Z,Trader Joe's,GROCERIES
SETTLED,PURCHASE,3.0,USD,2026-03-30T00:30:54Z,PATH,TRANSIT
SETTLED,PAYMENT,-500.0,USD,2026-03-28T01:30:00Z,Payment - Bilt Housing,RENT
SETTLED,REFUND,-14.08,USD,2026-03-19T21:50:55Z,The Home Depot,HARDWARE
```

Column meanings:

- `status`: transaction state from Bilt (for example `SETTLED`)
- `type`: Bilt transaction type (`PURCHASE`, `PAYMENT`, `REFUND`, etc.)
- `amount`: decimal amount (negative for refunds/payments)
- `currency`: usually `USD`
- `createdAt`: timestamp in UTC (`YYYY-MM-DDTHH:MM:SSZ`)
- `merchant`: merchant/description text
- `category`: Bilt category label

## 3) Empower Session Values (JSESSIONID + csrf)
Retrieve the **JSESSIONID** cookie value and **csrf** from your browser session.

See empower/README.md for more info

## 4) Upload CSV to Empower

Command (prompts for tokens if omitted):

```bash
python -m empower.upload_transactions bilt/transactions_2026-02-01_2026-03-31.csv
```

Command with explicit session values:

```bash
python -m empower.upload_transactions bilt/transactions_2026-02-01_2026-03-31.csv \
  --jsessionid '<your-jsessionid>' \
  --csrf '<your-csrf>'
```

Optional flags:

```bash
python -m empower.upload_transactions bilt/transactions_2026-02-01_2026-03-31.csv \
  --mapping-file empower/category_mappings.json \
  --timeout 30
```

Upload flow summary:

1. Choose the destination Empower account.
2. Resolve each Bilt category to an Empower category.
3. Review the final batch.
4. Confirm upload.
5. Mapping cache is updated for future runs.

### Category Matching Details

The uploader attempts category matching between a Bilt category and existing empower categories in a deterministic order for each CSV row:

1. Saved mapping lookup
2. Exact case-insensitive name match
3. Normalized match (punctuation/spacing-insensitive)
4. Interactive manual resolution

#### 1) Saved Mapping Lookup

The script first checks `empower/category_mappings.json` (or your `--mapping-file`) for the Bilt category name.

- It first tries an exact key match.
- If not found, it tries normalized key matching.
- If a saved `empower_category_id` still exists in current Empower categories, that wins.
- If the ID is stale, it falls back to matching by saved `empower_category_name`.

If any of those succeed, the row is auto-resolved with no prompt.

#### 2) Exact Name Match

If no saved mapping applies, the uploader compares the Bilt category text to Empower category names using case-insensitive equality.

Example:

- `Dining` matches `DINING`

#### 3) Normalized Match

If exact matching fails, the uploader normalizes both names and checks equality on the normalized value.

Normalization is intended to smooth differences like:

- case
- punctuation
- separators/spaces

Example patterns it can help with:

- `Ride Share` vs `Rideshare`
- `Home-Improvement` vs `home improvement`

#### 4) Interactive Manual Resolution

If the row is still unmatched, the script shows options:

- map to an existing Empower category (provide a search query to list possible matches e.g. "Re" -> "Restaurants", "Refunds", etc.)
- create a new Empower category
- skip this transaction

It also provides fuzzy suggestions to speed up selection.

### What Gets Saved to the Mapping File

When you map or create during initial resolution, the mapping file stores:

- `empower_category_id`
- `empower_category_name`
- `empower_category_type`

That mapping is **reused** in future runs so the same Bilt category can be auto-resolved.

> Note: review-time one-off edits affect the current transaction selection for that run; they do not automatically rewrite the shared mapping unless you explicitly map/create during resolution.

## 5) View and Delete Transactions in Empower

Command:

```bash
python -m empower.delete_transactions
```

Command with explicit session values:

```bash
python -m empower.delete_transactions \
  --jsessionid '<your-jsessionid>' \
  --csrf '<your-csrf>'
```

What the script does:

1. Prompts for date range (default is first day of current month through today).
2. Prompts you to choose an Empower account.
3. Shows a numbered table of transactions with a `Manual?` column.
4. Lets you select rows with syntax like `1,3,5-7`.
5. Deletes only manual transactions after confirmation.
6. Prints a summary of deleted/failed/skipped items.

## 6) Quick Command Checklist

```bash
# 1) Export from Bilt
python -m bilt.retrieve_transactions

# 2) Upload exported CSV to Empower
python -m empower.upload_transactions bilt/transactions_2026-02-01_2026-03-31.csv

# 3) Review and delete manual entries in Empower
python -m empower.delete_transactions
```

## 7) Troubleshooting

- If imports fail, make sure you are running from the repository root.
- Use module execution (`python -m ...`) instead of file execution (`python path/to/script.py`).
- If Empower rejects requests, refresh your `JSESSIONID` and `csrf` from a new browser session.
