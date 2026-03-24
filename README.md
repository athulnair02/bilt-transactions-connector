# Bilt Transaction Retriever

CLI script to export Bilt transactions to CSV with resilient authentication.

Auth fallback chain:
1. Use cached JWT if still valid.
2. If JWT is expired/missing, use cached refresh token cookie (`app.rt`) to mint a new JWT.
3. If refresh token fails, run SMS OTP flow.

If any protected endpoint returns `401`, the script automatically runs the same chain and retries once.

## Setup

1. Create and activate a Python environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

The script will:
1. Authenticate (cache/JWT/refresh/OTP fallback).
2. Fetch wallet cards and show a numbered selection menu.
3. Ask for date range (`YYYY-MM-DD` start and end).
4. Fetch paginated transactions.
5. Export CSV (one transaction per row).

## Command options

```bash
python main.py --help
```

Common flags:
- `--output <path>`: custom CSV output path.
- `--cache-file <path>`: custom cache file path (default `.bilt_token_cache.json`).
- `--page-size <n>`: API page size for transaction retrieval (default `100`).
- `--force-otp`: bypass cache and force OTP login.

## Output

Default output filename when `--output` is omitted:

`transactions_<start-date>_<end-date>.csv`

CSV columns:
- `status`
- `type`
- `amount`
- `currency`
- `createdAt`
- `merchant`
- `category`

## Cache behavior

- Cache file: `.bilt_token_cache.json`
- Stored values: phone, JWT, JWT expiry, refresh token cookie, update timestamp.
- Cache file is ignored by git via `.gitignore`.

## Notes

- Phone number must include country code, for example `+11234567890`.
- OTP retrieval remains manual (you enter the code received by SMS).
- If token schema changes upstream, you may need to update response parsing logic.
