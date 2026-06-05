# Empower Scripts

The following scripts are to help manage your transactions with Empower (formerly Personal Capital) so that you can better understand your cash flow and budgeting since there is no option to connect with biltrewards or to properly upload a CSV of transactions. 

These scripts therefore allow you to continue using the Bilt 2.0 card and have a connection to Empower's personal finance tools so you can be a responsible spender.

The `empower` package contains two interactive CLIs:

- `python -m empower.upload_transactions`: upload a Bilt-exported CSV into an Empower account
- `python -m empower.delete_transactions`: list Empower transactions in a date range and delete selected manual entries

Run both commands from the repository root.

## Authentication

Both scripts log in to Empower automatically.

**First run:** the script prompts for your Empower username, password, and plan/employer code (e.g. `TriNet`). On a new device it also sends an SMS/email activation code — enter it when prompted. Credentials are never stored; only the resulting session tokens are cached.

**Subsequent runs:** the cached session is reused. Use `--force-login` to re-authenticate if the session has expired.

**Cache file:** `empower/.empower_auth_cache.json` (gitignored)

### Manual override

If you prefer to supply session values directly (e.g. from browser DevTools), pass them as flags and auto-login is skipped:

```bash
python -m empower.upload_transactions transactions.csv \
  --jsessionid '<cookie-value>' \
  --csrf '<csrf-token>'
```

## Upload Transactions

### Command

```bash
python -m empower.upload_transactions /path/to/transactions.csv
```

Help:

```bash
python -m empower.upload_transactions --help
```

Options:

- `--jsessionid <value>`: override auto-login with a manual JSESSIONID cookie
- `--csrf <value>`: override auto-login with a manual csrf token
- `--force-login`: ignore the cached session and re-authenticate
- `--mapping-file <path>`: override the local Bilt-to-Empower mapping file
- `--timeout <seconds>`: override the HTTP timeout

Workflow:

1. Load the CSV exported by `python -m bilt.retrieve_transactions`
2. Fetch available Empower accounts and choose the destination account
3. Fetch current Empower categories
4. Resolve each Bilt category using this order:
   - saved local mapping
   - exact case-insensitive match
   - normalized match
   - interactive selection with fuzzy suggestions
5. For unmatched categories, choose to map, create a category, or skip the transaction
6. Review the full upload batch before submission
7. Upload the accepted transactions to Empower
8. Save reusable category mappings for future runs

Mapping file behavior:

- Default mapping file: `empower/category_mappings.json`
- New saved mappings are reused on later runs
- Review-time one-off edits affect only the current run unless a new mapping is explicitly saved

Notes:

- The uploader prints a review table before creating any Empower transactions
- Skipped rows are excluded from upload
- Newly created Empower categories are usable immediately in the same run

## Delete Manual Transactions

### Command

```bash
python -m empower.delete_transactions
```

Help:

```bash
python -m empower.delete_transactions --help
```

Options:

- `--jsessionid <value>`: override auto-login with a manual JSESSIONID cookie
- `--csrf <value>`: override auto-login with a manual csrf token
- `--force-login`: ignore the cached session and re-authenticate
- `--timeout <seconds>`: override the HTTP timeout

Workflow:

1. Authenticate (auto-login or manual session values)
2. Prompt for a date range, defaulting to the first day of the current month through today
3. Fetch available Empower accounts and choose one
4. Fetch categories and transactions for the chosen account and date range
5. Print a numbered table including date, amount, type, category, and whether the transaction is manual
6. Enter row numbers to delete using values like `1,3,5-7`
7. Review the selected manual transactions and confirm deletion
8. Delete only transactions marked as manual
9. Print a summary of deleted, failed, and skipped non-manual rows

Notes:

- Non-manual transactions cannot be deleted by this script and are skipped automatically
- If you select only non-manual rows, the script asks you to choose again
- Enter `q` at the selection prompt to exit without deleting anything
