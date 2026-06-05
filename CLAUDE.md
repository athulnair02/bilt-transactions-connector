# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Install dependencies (Python 3.12, venv already at `.venv`):
```bash
pip install -r requirements.txt
```

Run the three CLI entry points (always from repo root):
```bash
python -m bilt.retrieve_transactions
python -m empower.upload_transactions <path-to-csv>
python -m empower.delete_transactions
```

Each module supports `--help`. There are no automated tests or linting configs in this repo.

## Architecture

Three interactive CLI scripts bridging the Bilt card and Empower personal finance platform.

**Packages:**
- `bilt/` — Bilt transaction exporter. Self-contained in `retrieve_transactions.py`.
- `empower/` — Empower upload and delete CLIs. Uses `EmpowerClient` in `client.py`, domain models in `models.py`.
- `utils/` — Shared `errors.py` (exception types) and `helpers.py` (parsing, formatting, prompts).

**Bilt auth chain** (`bilt/retrieve_transactions.py`):
1. Cached JWT (`access_token`) if still valid (60s skew)
2. Cached refresh token → new JWT
3. SMS OTP flow (interactive)

Token state is persisted in `bilt/.bilt_token_cache.json`. On a 401 from a protected endpoint, `request_with_reauth` re-runs the auth chain once and retries.

**Empower auth:** Fully automated via `empower/auth.py`. On first run, prompts for username, password, and plan/employer code (`accu`); on new devices triggers an SMS/email activation code flow (`rememberDevice: true` registers the device). Session tokens are cached in `empower/.empower_auth_cache.json`. Manual `--jsessionid`/`--csrf` flags bypass auto-login. Use `--force-login` to re-authenticate.

**EmpowerClient** (`empower/client.py`): wraps all Empower API calls. Every response envelope has `spHeader.errors` (checked automatically) and a `spData` payload. Amount sign convention: Bilt amounts are negated on upload (`-transaction.amount`) to match Empower's sign scheme.

**Category mapping** (`empower/category_mappings.json`): persists Bilt-category → Empower-category mappings across runs. Resolution order: saved mapping → exact name match → normalized match → interactive. New mappings are saved after interactive resolution.

**Known Bilt API quirk:** The transactions endpoint returns rows with dates before the requested `startDate`. `fetch_transactions` filters these out client-side.

**Import requirement:** Always run via `python -m <package>.<module>` from the repo root. Direct file execution breaks relative imports.
