#!/usr/bin/env python3
"""Bilt transactions exporter with cached auth fallback chain.

Auth precedence:
1) Cached accessToken if still valid.
2) Cached refreshToken to get a new accessToken.
3) SMS OTP flow if refresh token fails.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import json
import time
from pathlib import Path
from typing import Any, Callable

import requests

from utils.errors import BiltError, UnauthorizedError
from utils.helpers import prompt_date_range

ID_BASE_URL = "https://id.biltrewards.com"
WEB_BASE_URL = "https://www.bilt.com"
API_BASE_URL = "https://api.biltrewards.com"

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_PAGE_SIZE = 100
DEFAULT_CACHE_FILE = "bilt/.bilt_token_cache.json"


def print_info(message: str) -> None:
    print(f"[INFO] {message}")


def print_warn(message: str) -> None:
    print(f"[WARN] {message}")


def print_error(message: str) -> None:
    print(f"[ERROR] {message}")


def load_cache(cache_path: Path) -> dict[str, Any]:
    if not cache_path.exists():
        return {}

    try:
        with cache_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        print_warn(f"Cache file is unreadable ({exc}). Starting with empty cache.")
        return {}


def save_cache(cache_path: Path, cache_data: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(cache_data, f, indent=2, sort_keys=True)


def decode_jwt_exp(token: str) -> int | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        padding = "=" * ((4 - len(payload) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding)
        payload_obj = json.loads(decoded.decode("utf-8"))
        exp = payload_obj.get("exp")
        return int(exp) if isinstance(exp, int) else None
    except (ValueError, json.JSONDecodeError):
        return None


def is_token_valid(token: str | None, now_epoch: int | None = None, skew_seconds: int = 60) -> bool:
    if not token:
        return False
    exp = decode_jwt_exp(token)
    if exp is None:
        return False
    now = now_epoch if now_epoch is not None else int(time.time())
    return exp > now + skew_seconds



def ask_phone(cached_phone: str | None = None) -> str:
    while True:
        default_note = f" [{cached_phone}]" if cached_phone else ""
        raw = input(f"Enter phone number with country code{default_note}: ").strip()
        if not raw and cached_phone:
            raw = cached_phone
        if raw and raw.startswith("+"):
            return raw
        print_warn("Phone number must include country code, for example +13463072170.")


def ask_otp() -> str:
    while True:
        otp = input("Enter OTP code: ").strip()
        if otp:
            return otp
        print_warn("OTP cannot be empty.")


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    expected_status: int | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    **kwargs: Any,
) -> dict[str, Any]:
    response = session.request(method=method, url=url, timeout=timeout, **kwargs)

    if response.status_code == 401:
        raise UnauthorizedError(f"401 from {url}")

    if expected_status is not None and response.status_code != expected_status:
        raise BiltError(
            f"{method} {url} failed with {response.status_code}: {response.text[:500]}"
        )

    if response.status_code >= 400:
        raise BiltError(f"{method} {url} failed with {response.status_code}: {response.text[:500]}")

    if not response.text.strip():
        return {}

    try:
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError as exc:
        raise BiltError(f"{method} {url} did not return JSON.") from exc


def trigger_sms(session: requests.Session, phone: str) -> str:
    url = f"{ID_BASE_URL}/public/auth/sms"
    headers = {
        "accept": "application/json, text/plain, */*",
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": "https://id.biltrewards.com",
        "referer": "https://id.biltrewards.com/",
    }
    payload = {"channel": "SMS", "loginId": phone, "iframe": False}

    response = session.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT_SECONDS)
    if response.status_code >= 400:
        raise BiltError(f"Failed to trigger SMS ({response.status_code}): {response.text[:500]}")

    try:
        body = response.json()
    except ValueError as exc:
        raise BiltError("Trigger SMS response was not valid JSON.") from exc

    verification_id = body.get("verificationId") if isinstance(body, dict) else None
    if not verification_id or not isinstance(verification_id, str):
        raise BiltError("verificationId missing in Trigger SMS response.")

    return verification_id


def verify_otp_and_get_tokens(
    session: requests.Session, phone: str, verification_id: str, otp_code: str
) -> tuple[str, str]:
    """Verify OTP and extract accessToken and refreshToken from response.
    
    Returns:
        tuple[str, str]: (accessToken, refreshToken)
    """
    url = f"{ID_BASE_URL}/public/auth/sms"
    payload = {
        "channel": "SMS",
        "iframe": False,
        "loginId": phone,
        "otpCode": otp_code,
        "rememberMe": True,
        "verificationId": verification_id
    }

    response = session.put(url, json=payload, timeout=DEFAULT_TIMEOUT_SECONDS)
    if response.status_code >= 400:
        raise BiltError(f"OTP verification failed ({response.status_code}): {response.text[:500]}")

    try:
        body = response.json()
    except ValueError as exc:
        raise BiltError("OTP verification response was not valid JSON.") from exc

    if not isinstance(body, dict):
        raise BiltError("OTP verification response was not a JSON object.")

    access_token = body.get("accessToken")
    refresh_token = body.get("refreshToken")

    if not isinstance(access_token, str) or not access_token.strip():
        raise BiltError("accessToken missing or empty in OTP verification response.")
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise BiltError("refreshToken missing or empty in OTP verification response.")

    return access_token, refresh_token


def refresh_access_token(session: requests.Session, refresh_token: str) -> tuple[str, str]:
    """Get new accessToken using refreshToken.
    
    Args:
        session: requests session
        refresh_token: refresh token to use
    
    Returns:
        tuple[str, str]: (accessToken, refreshToken)
            If refreshToken in response is non-empty, it replaces the input token.
            Otherwise, the input refreshToken is returned unchanged.
    """
    url = f"{WEB_BASE_URL}/public/user/authentication/token"
    headers = {"Cookie": f"__Host-bilt-rt={refresh_token}"}

    payload = request_json(session, "GET", url, headers=headers, expected_status=200)
    
    access_token = payload.get("accessToken")
    if not isinstance(access_token, str) or not access_token.strip():
        raise BiltError("accessToken missing or empty in auth token response.")
    
    # Check if response contains a refreshToken to update; only use if non-empty
    response_refresh_token = payload.get("refreshToken")
    if isinstance(response_refresh_token, str) and len(response_refresh_token) > 0:
        return access_token, response_refresh_token
    
    return access_token, refresh_token


def ensure_auth(
    session: requests.Session,
    cache: dict[str, Any],
    cache_path: Path,
    *,
    force_otp: bool = False,
) -> str:
    if not force_otp:
        cached_access_token = cache.get("access_token") if isinstance(cache.get("access_token"), str) else None
        if is_token_valid(cached_access_token):
            print_info("Using cached accessToken.")
            return cached_access_token

        refresh_token = cache.get("refresh_token") if isinstance(cache.get("refresh_token"), str) else None
        if refresh_token:
            print_info("Cached accessToken missing/expired. Requesting new accessToken using cached refresh token.")
            try:
                access_token, updated_refresh_token = refresh_access_token(session, refresh_token)
                cache["access_token"] = access_token
                cache["access_token_exp"] = decode_jwt_exp(access_token)
                cache["refresh_token"] = updated_refresh_token
                cache["updated_at"] = int(time.time())
                save_cache(cache_path, cache)
                return access_token
            except BiltError as exc:
                print_warn(f"Refresh-token auth failed: {exc}")

    print_info("Starting SMS OTP authentication flow.")
    phone = ask_phone(cache.get("phone") if isinstance(cache.get("phone"), str) else None)
    verification_id = trigger_sms(session, phone)
    print_info(f"OTP requested. verificationId captured: {verification_id}")
    otp_code = ask_otp()
    access_token, refresh_token = verify_otp_and_get_tokens(session, phone, verification_id, otp_code)

    cache["phone"] = phone
    cache["refresh_token"] = refresh_token
    cache["access_token"] = access_token
    cache["access_token_exp"] = decode_jwt_exp(access_token)
    cache["updated_at"] = int(time.time())
    save_cache(cache_path, cache)
    print_info("Authentication cache updated with new refresh token and accessToken.")

    return access_token


def request_with_reauth(
    request_fn: Callable[[str], requests.Response],
    session: requests.Session,
    cache: dict[str, Any],
    cache_path: Path,
    bearer_token: str,
) -> tuple[dict[str, Any], str]:
    response = request_fn(bearer_token)
    if response.status_code != 401:
        if response.status_code >= 400:
            raise BiltError(f"Request failed ({response.status_code}): {response.text[:500]}")
        try:
            payload = response.json()
            if isinstance(payload, dict):
                # Check for refreshToken update in response
                response_refresh_token = payload.get("refreshToken")
                if isinstance(response_refresh_token, str) and len(response_refresh_token) > 0:
                    cache["refresh_token"] = response_refresh_token
                    cache["updated_at"] = int(time.time())
                    save_cache(cache_path, cache)
                return payload, bearer_token
            return {"data": payload}, bearer_token
        except ValueError as exc:
            raise BiltError("Protected endpoint did not return JSON.") from exc

    print_warn("Received 401. Attempting auth recovery chain (accessToken->refresh->OTP) and one retry.")
    new_token = ensure_auth(session, cache, cache_path, force_otp=False)
    retry_response = request_fn(new_token)
    if retry_response.status_code >= 400:
        raise BiltError(f"Retry after re-auth failed ({retry_response.status_code}): {retry_response.text[:500]}")

    try:
        payload = retry_response.json()
        if isinstance(payload, dict):
            # Check for refreshToken update in response
            response_refresh_token = payload.get("refreshToken")
            if isinstance(response_refresh_token, str) and len(response_refresh_token) > 0:
                cache["refresh_token"] = response_refresh_token
                cache["updated_at"] = int(time.time())
                save_cache(cache_path, cache)
            return payload, new_token
        return {"data": payload}, new_token
    except ValueError as exc:
        raise BiltError("Retry endpoint did not return JSON.") from exc


def walk_json(value: Any) -> list[Any]:
    items: list[Any] = []
    if isinstance(value, dict):
        items.append(value)
        for child in value.values():
            items.extend(walk_json(child))
    elif isinstance(value, list):
        for child in value:
            items.extend(walk_json(child))
    return items


def extract_cards(payload: dict[str, Any]) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    credit_cards = payload.get("creditCards")
    if not isinstance(credit_cards, list):
        return cards

    for item in credit_cards:
        if not isinstance(item, dict):
            continue

        if item.get("isBiltCard") is not True:
            continue

        card_id = item.get("uuid")
        if not isinstance(card_id, str) or not card_id.strip():
            continue

        alias = item.get("alias") if isinstance(item.get("alias"), str) else "Bilt Card"
        last4_value = item.get("cardNumberLastFour")
        last4 = last4_value.strip() if isinstance(last4_value, str) and last4_value.strip() else ""
        label = f"{alias} ({last4})" if last4 else alias
        cards.append({"card_id": card_id, "label": label})

    return cards


def choose_card(cards: list[dict[str, str]]) -> str:
    if not cards:
        raise BiltError("No cards found in wallet response.")

    print("\nAvailable cards:")
    for idx, card in enumerate(cards, start=1):
        print(f"  {idx}. {card['label']} ({card['card_id']})")

    while True:
        raw = input("Choose a card number: ").strip()
        if not raw.isdigit():
            print_warn("Please enter a valid numeric choice.")
            continue

        index = int(raw) - 1
        if 0 <= index < len(cards):
            return cards[index]["card_id"]

        print_warn("Choice out of range.")


def extract_cardholders(payload: dict[str, Any]) -> list[dict[str, str]]:
    cardholders: list[dict[str, str]] = []
    filters = payload.get("filters")
    raw_cardholders = filters.get("cardholders") if isinstance(filters, dict) else None
    if not isinstance(raw_cardholders, list):
        return cardholders

    for item in raw_cardholders:
        if not isinstance(item, dict):
            continue

        cardholder_id = item.get("id")
        name = item.get("displayName")
        cardholders.append({"cardholder_id": cardholder_id, "label": name})

    return cardholders


def choose_cardholder(cardholders: list[dict[str, str]]) -> str | None:
    """Return the selected cardholder id, or None for all cardholders."""
    print("\nCardholders:")
    print("  0. All cardholders")
    for idx, cardholder in enumerate(cardholders, start=1):
        print(f"  {idx}. {cardholder['label']} ({cardholder['cardholder_id']})")

    while True:
        raw = input("Choose a cardholder number [0]: ").strip() or "0"
        if not raw.isdigit():
            print_warn("Please enter a valid numeric choice.")
            continue

        index = int(raw)
        if index == 0:
            return None
        if 1 <= index <= len(cardholders):
            return cardholders[index - 1]["cardholder_id"]

        print_warn("Choice out of range.")


def flatten_transaction(tx: dict[str, Any]) -> dict[str, str]:
    category = tx.get("displayCategory")
    if category != "RENT":
        category = tx.get("merchant").get("category") if tx.get("merchant") and isinstance(tx.get("merchant").get("category"), str) else None

    row = {
        "status": tx.get("status"),
        "type": tx.get("type"),
        "amount": tx.get("amount").get("amount"),
        "currency": tx.get("amount").get("currencyCode"),
        "createdAt": tx.get("createdAt"),
        "merchant": tx.get("description"),
        "category": category,
    }

    return row


def fetch_wallet(session: requests.Session, token: str, cache: dict[str, Any], cache_path: Path) -> tuple[dict[str, Any], str]:
    def _wallet_request(access_token: str) -> requests.Response:
        return session.get(
            f"{API_BASE_URL}/wallet",
            params={"showBiltCards": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )

    return request_with_reauth(_wallet_request, session, cache, cache_path, token)


def fetch_transactions(
    session: requests.Session,
    token: str,
    cache: dict[str, Any],
    cache_path: Path,
    card_id: str,
    start_date: dt.date,
    end_date: dt.date,
    page_size: int,
    cardholder_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    all_transactions: list[dict[str, Any]] = []
    cardholders: list[dict[str, str]] = []
    page_index = 0
    current_token = token

    start_iso = f"{start_date.isoformat()}T00:00:00Z"
    end_iso = f"{end_date.isoformat()}T23:59:59Z"

    while True:
        params: dict[str, Any] = {
            "startDate": start_iso,
            "endDate": end_iso,
            "pageIndex": page_index,
            "pageSize": page_size,
        }
        if cardholder_id:
            params["cardholderIds"] = cardholder_id

        def _tx_request(access_token: str) -> requests.Response:
            return session.get(
                f"{API_BASE_URL}/bilt-card/cards/{card_id}/transactions",
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )

        payload, current_token = request_with_reauth(
            _tx_request,
            session,
            cache,
            cache_path,
            current_token,
        )
        if not cardholders:
            cardholders = extract_cardholders(payload)

        rows = payload.get("transactions")

        if not rows:
            break

        # Filter out rows have a createdAt before start_date since their API returns values before startDate (idk why)
        filtered_rows = []
        for row in rows:
            created_at_str = row.get("createdAt")
            created_at = dt.datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            
            if created_at.date() < start_date:
                continue
            filtered_rows.append(row)

        all_transactions.extend(filtered_rows)
        print_info(f"Fetched page {page_index} with {len(filtered_rows)} transactions.")

        has_more_pages = payload.get("hasMorePages")
        if has_more_pages is False:
            break

        if has_more_pages is not True and len(filtered_rows) < page_size:
            break

        page_index += 1
        if page_index > 10:
            raise BiltError("Aborting pagination after 10 pages for safety.")

    return all_transactions, cardholders, current_token


def write_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    fields = [
        "status",
        "type",
        "amount",
        "currency",
        "createdAt",
        "merchant",
        "category",
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Bilt transactions to CSV.")
    parser.add_argument("--output", help="Optional output CSV path.")
    parser.add_argument(
        "--cache-file",
        default=DEFAULT_CACHE_FILE,
        help=f"Token cache file path (default: {DEFAULT_CACHE_FILE}).",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"Transactions page size (default: {DEFAULT_PAGE_SIZE}).",
    )
    parser.add_argument(
        "--force-otp",
        action="store_true",
        help="Ignore cache and force fresh OTP authentication.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cache_path = Path(args.cache_file)
    cache = load_cache(cache_path)

    if args.page_size <= 0:
        raise BiltError("--page-size must be greater than 0.")

    session = requests.Session()

    token = ensure_auth(session, cache, cache_path, force_otp=args.force_otp)

    wallet_payload, token = fetch_wallet(session, token, cache, cache_path)
    cards = extract_cards(wallet_payload)
    card_id = choose_card(cards)

    today = dt.date.today()
    start_date, end_date = prompt_date_range(
        default_start=today.replace(day=1),
        default_end=today,
    )

    print_info(f"Fetching transactions for card {card_id} from {start_date} to {end_date}.")
    transactions, cardholders, token = fetch_transactions(
        session,
        token,
        cache,
        cache_path,
        card_id,
        start_date,
        end_date,
        args.page_size,
    )

    if cardholders:
        cardholder_id = choose_cardholder(cardholders)
        if cardholder_id:
            print_info(f"Re-fetching transactions for cardholder {cardholder_id}.")
            transactions, _, token = fetch_transactions(
                session,
                token,
                cache,
                cache_path,
                card_id,
                start_date,
                end_date,
                args.page_size,
                cardholder_id=cardholder_id,
            )
    else:
        print_warn("No cardholders found in transactions response; exporting all transactions.")

    cache["access_token"] = token
    cache["access_token_exp"] = decode_jwt_exp(token)
    cache["updated_at"] = int(time.time())
    save_cache(cache_path, cache)

    rows = [flatten_transaction(tx) for tx in transactions]

    output_path = (
        Path(args.output)
        if args.output
        else Path(f"transactions_{start_date.isoformat()}_{end_date.isoformat()}.csv")
    )

    # Make sure the output path starts with "bilt/"
    if output_path.parts[0] != "bilt":
        output_path = Path("bilt") / output_path
    
    write_csv(output_path, rows)

    print_info(f"Export complete. Wrote {len(rows)} rows to {output_path}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print_error("Interrupted by user.")
        raise SystemExit(130)
    except BiltError as exc:
        print_error(str(exc))
        raise SystemExit(1)
