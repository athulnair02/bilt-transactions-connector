# Bilt Transaction Manager

The Bilt platform has undergone plenty of changes after their shift to Bilt 2.0 which has brought on some pain points for their customers. One of which is the ability to connect with certain financial aggregators (e.g. Empower) to have a better picture of financial health. These scripts allow a user to download all their transactions (including spending category, which is still a missing feature) and upload them to financial aggregators missing connections with Plaid/biltrewards.

This repository contains three CLI entry points:

- `python -m bilt.retrieve_transactions`: export Bilt card transactions to CSV
- `python -m empower.upload_transactions`: upload a Bilt CSV into Empower
- `python -m empower.delete_transactions`: review and delete manual Empower transactions

Use module execution (`python -m ...`) from the repository root. That keeps imports consistent across the `bilt`, `empower`, and `utils` packages.

## Setup

1. Create and activate a Python virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run commands from the repository root:

```bash
cd /path/to/bilt-transactions
```

## Scripts

### Bilt Export

Run:

```bash
python -m bilt.retrieve_transactions
```

Help:

```bash
python -m bilt.retrieve_transactions --help
```

What it does:

1. Authenticates with Bilt using the cached JWT, then the cached refresh token, then SMS OTP if needed.
2. Fetches your Bilt wallet and shows a numbered card-selection menu.
3. Prompts for a start and end date.
4. Retrieves paginated transactions for the selected card.
5. Writes the result to CSV.

Common flags:

- `--output <path>`: write the CSV to a custom location
- `--cache-file <path>`: override the token cache file path
- `--page-size <n>`: override the Bilt transactions page size
- `--force-otp`: ignore the cache and force a new OTP login

Default output filename:

```text
transactions_<start-date>_<end-date>.csv
```

CSV columns:

- `status`
- `type`
- `amount`
- `currency`
- `createdAt`
- `merchant`
- `category`

Cache behavior:

- Default cache file: `bilt/.bilt_token_cache.json`
- Stored values: phone number, refresh token, JWT, JWT expiry, updated timestamp
- If a protected Bilt endpoint returns `401`, the exporter re-runs the auth chain and retries once

### Empower Upload

See [empower/README.md](./empower/README.md) for the full upload workflow.

Quick start:

```bash
python -m empower.upload_transactions transactions_2026-03-01_2026-03-31.csv
```

### Empower Delete

See [empower/README.md](./empower/README.md) for the full delete workflow.

Quick start:

```bash
python -m empower.delete_transactions
```

## Notes

- Phone numbers for Bilt OTP must include country code, for example `+11234567890`
- The Bilt OTP step is manual: the script prompts you to enter the SMS code
- Empower scripts require a valid `JSESSIONID` cookie and `csrf` token from an active Empower session
