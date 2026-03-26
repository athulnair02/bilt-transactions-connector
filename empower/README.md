# Empower Financial Transaction Connector

Uploads transactions from a Bilt-exported CSV into an Empower account so they do not need to be entered manually in the Empower UI.

## Requirements

- JSESSIONID cookie
- csrf token
- Bilt CSV file path

## Run

From the repository root:

```bash
python empower/upload_transactions.py /path/to/transactions.csv
```

You can also pass credentials directly:

```bash
python empower/upload_transactions.py /path/to/transactions.csv \
  --jsessionid '<cookie-value>' \
  --csrf '<csrf-token>'
```

Optional flags:

- `--mapping-file <path>`: override the local Bilt-to-Empower category mapping JSON file
- `--timeout <seconds>`: change the HTTP timeout for Empower API calls

## Workflow

1. Provide the Empower session values: `JSESSIONID` and `csrf`
2. Fetch available Empower accounts and choose the destination account
3. Fetch all current Empower categories
4. Read the Bilt CSV and try to resolve each Bilt category in this order:
	- saved local mapping
	- exact case-insensitive match
	- normalized match
	- interactive choice with fuzzy suggestions
5. For unmatched categories, choose one of these actions:
	- map to an existing Empower category
	- create a new Empower category
	- skip that transaction
6. After initial resolution, review the full batch before upload
7. In the review loop, either:
	- accept the batch
	- edit a single transaction and again choose one of:
	  - map to an existing Empower category
	  - create a new Empower category
	  - skip the transaction
8. After accepting the batch, upload the transactions to the selected Empower account
9. Save the local category mapping JSON for future runs

## Local Mapping Cache

The uploader stores Bilt-to-Empower category mappings in `empower/category_mappings.json` by default.

- Saved mappings are checked before any category comparison on future runs
- When you explicitly map a Bilt category or create a new Empower category during initial resolution, that mapping is saved for reuse
- Review-time edits only change the selected transaction for the current run; they do not rewrite the shared category mapping automatically

## Notes

- The uploader prints a batch confirmation view with every transaction that will be uploaded, including the original Bilt category and final Empower category
- Transactions skipped during initial resolution or review are excluded from upload
- New Empower categories are used immediately after creation; a separate category refresh is not required
