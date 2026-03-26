#!/usr/bin/env python3
"""Upload exported Bilt transactions into Empower."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from difflib import get_close_matches
from pathlib import Path
from typing import Any

import requests

EMPOWER_API_BASE_URL = "https://pc-api.empower-retirement.com/api"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAPPING_FILE = Path(__file__).with_name("category_mappings.json")
DEFAULT_CATEGORY_TYPE = "EXPENSE"
API_CLIENT = "WEB"


class EmpowerError(Exception):
    """Base application error."""


@dataclass(frozen=True)
class EmpowerAccount:
    account_id: str
    name: str
    firm_name: str
    product_type: str
    current_balance: Decimal


@dataclass(frozen=True)
class EmpowerCategory:
    category_id: int
    name: str
    category_type: str
    is_editable: bool


@dataclass
class CsvTransaction:
    row_number: int
    transaction_date: dt.date
    merchant: str
    bilt_category: str
    amount: Decimal
    currency: str
    status: str
    transaction_type: str
    subtype: str
    empower_category: EmpowerCategory | None = None
    skipped: bool = False


def print_info(message: str) -> None:
    print(f"[INFO] {message}")


def print_warn(message: str) -> None:
    print(f"[WARN] {message}")


def print_error(message: str) -> None:
    print(f"[ERROR] {message}")


def normalize_text(value: str) -> str:
    normalized = "".join(char.lower() if char.isalnum() else " " for char in value)
    return " ".join(normalized.split())


def sanitize_jsessionid(value: str) -> str:
    cookie_value = value.strip()
    if cookie_value.startswith("JSESSIONID="):
        cookie_value = cookie_value.split("=", 1)[1]
    if ";" in cookie_value:
        cookie_value = cookie_value.split(";", 1)[0]
    if not cookie_value:
        raise EmpowerError("JSESSIONID cannot be empty.")
    return cookie_value


def parse_decimal(value: str, *, field_name: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise EmpowerError(f"Invalid decimal value for {field_name}: {value!r}") from exc


def format_decimal(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"))
    return format(quantized, "f")


def parse_created_at(value: str) -> dt.date:
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError as exc:
        raise EmpowerError(f"Invalid createdAt value: {value!r}") from exc


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    try:
        with path.open("r", encoding="utf-8") as file_handle:
            payload = json.load(file_handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise EmpowerError(f"Could not read JSON file {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise EmpowerError(f"JSON file {path} must contain an object at the top level.")
    return payload


def save_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_handle:
        json.dump(payload, file_handle, indent=2, sort_keys=True)


def load_mapping_file(path: Path) -> dict[str, dict[str, Any]]:
    raw = load_json_file(path)
    mappings: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, dict):
            mappings[key] = value
    return mappings


class EmpowerClient:
    def __init__(self, jsessionid: str, csrf: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.session = requests.Session()
        self.jsessionid = sanitize_jsessionid(jsessionid)
        self.csrf = csrf.strip()
        self.timeout = timeout

        if not self.csrf:
            raise EmpowerError("csrf token cannot be empty.")

    def _headers(self) -> dict[str, str]:
        return {"Cookie": f"JSESSIONID={self.jsessionid}"}

    def _request_json(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.request(
            method=method,
            url=f"{EMPOWER_API_BASE_URL}{endpoint}",
            headers=self._headers(),
            timeout=self.timeout,
            **kwargs,
        )

        if response.status_code >= 400:
            raise EmpowerError(
                f"{method} {endpoint} failed with {response.status_code}: {response.text[:500]}"
            )
        print_info(f"{method} {endpoint} returned {response.status_code}.")

        # Response status code may show success, but an error message can still exist
        err_msg = response.json().get('spHeader').get('errors', [{}])[0].get('message', None)
        if err_msg:
            raise EmpowerError(
                f"{method} {endpoint} returned an error. Response: {err_msg}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise EmpowerError(f"{method} {endpoint} did not return JSON.") from exc

        if not isinstance(payload, dict):
            raise EmpowerError(f"{method} {endpoint} returned a non-object JSON response.")
        return payload

    def get_accounts(self) -> list[EmpowerAccount]:
        payload = self._request_json(
            "POST",
            "/newaccount/getAccountsLite",
            files={
                "csrf": (None, self.csrf),
                "apiClient": (None, API_CLIENT),
            },
        )

        accounts = payload.get("spData")
        if not isinstance(accounts, list):
            raise EmpowerError("Account response does not contain a spData list.")

        extracted: list[EmpowerAccount] = []
        for item in accounts:
            if not isinstance(item, dict):
                continue
            account_id = item.get("pcapAccountId")
            name = item.get("name")
            if not isinstance(account_id, str) or not isinstance(name, str):
                continue
            current_balance_value = item.get("currentBalance", 0)
            extracted.append(
                EmpowerAccount(
                    account_id=account_id,
                    name=name,
                    firm_name=item.get("firmName") if isinstance(item.get("firmName"), str) else "",
                    product_type=item.get("productType")
                    if isinstance(item.get("productType"), str)
                    else "UNKNOWN",
                    current_balance=parse_decimal(str(current_balance_value), field_name="currentBalance"),
                )
            )

        if not extracted:
            raise EmpowerError("No uploadable accounts were found in the account response.")
        return extracted

    def get_categories(self) -> list[EmpowerCategory]:
        payload = self._request_json(
            "POST",
            "/transactioncategory/getCategories",
            data={
                "csrf": self.csrf,
                "apiClient": API_CLIENT,
            },
        )
        categories = payload.get("spData")
        if not isinstance(categories, list):
            raise EmpowerError("Category response does not contain a spData list.")

        extracted: list[EmpowerCategory] = []
        for item in categories:
            if not isinstance(item, dict):
                continue
            category_id = item.get("transactionCategoryId")
            name = item.get("name")
            category_type = item.get("type")
            if not isinstance(category_id, int) or not isinstance(name, str) or not isinstance(category_type, str):
                continue
            extracted.append(
                EmpowerCategory(
                    category_id=category_id,
                    name=name,
                    category_type=category_type,
                    is_editable=bool(item.get("isEditable", False)),
                )
            )

        if not extracted:
            raise EmpowerError("No categories were returned by Empower.")
        return extracted

    def add_category(self, name: str, category_type: str = DEFAULT_CATEGORY_TYPE) -> EmpowerCategory:
        payload = self._request_json(
            "POST",
            "/transactioncategory/addCategory",
            data={
                "name": name,
                "type": category_type,
                "csrf": self.csrf,
                "apiClient": API_CLIENT,
            },
        )
        item = payload.get("spData")
        if not isinstance(item, dict):
            raise EmpowerError("Create-category response does not contain a spData object.")

        category_id = item.get("transactionCategoryId")
        category_name = item.get("name")
        category_type_value = item.get("type")
        if (
            not isinstance(category_id, int)
            or not isinstance(category_name, str)
            or not isinstance(category_type_value, str)
        ):
            raise EmpowerError("Create-category response is missing category fields.")

        return EmpowerCategory(
            category_id=category_id,
            name=category_name,
            category_type=category_type_value,
            is_editable=bool(item.get("isEditable", False)),
        )

    def create_transaction(self, account_id: str, transaction: CsvTransaction) -> dict[str, Any]:
        if transaction.empower_category is None:
            raise EmpowerError("Cannot upload a transaction without an Empower category.")

        payload = self._request_json(
            "POST",
            "/transaction/createUserTransaction",
            data={
                "transactionDate": transaction.transaction_date.isoformat(),
                "userAccountId": account_id,
                "description": transaction.merchant,
                "transactionCategoryId": str(transaction.empower_category.category_id),
                "amount": format_decimal(-transaction.amount),
                "customTags": "[]",
                "csrf": self.csrf,
            },
        )
        sp_data = payload.get("spData")
        if not isinstance(sp_data, dict):
            raise EmpowerError("Create-transaction response does not contain a spData object.")
        return sp_data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload Bilt transactions CSV rows into Empower.")
    parser.add_argument("csv_path", help="Path to a Bilt-exported CSV file.")
    parser.add_argument("--jsessionid", help="Empower JSESSIONID cookie value.")
    parser.add_argument("--csrf", help="Empower csrf token value.")
    parser.add_argument(
        "--mapping-file",
        default=str(DEFAULT_MAPPING_FILE),
        help=f"Path to the local Bilt-to-Empower category mapping file (default: {DEFAULT_MAPPING_FILE}).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    return parser.parse_args()


def ask_non_empty(prompt: str, default: str | None = None) -> str:
    while True:
        default_note = f" [{default}]" if default else ""
        value = input(f"{prompt}{default_note}: ").strip()
        if not value and default is not None:
            return default
        if value:
            return value
        print_warn("This value cannot be empty.")


def read_transactions(csv_path: Path) -> list[CsvTransaction]:
    required_columns = {"amount", "createdAt", "merchant", "category", "currency", "status", "type"}
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as file_handle:
            reader = csv.DictReader(file_handle)
            fieldnames = set(reader.fieldnames or [])
            missing = sorted(required_columns - fieldnames)
            if missing:
                raise EmpowerError(
                    f"CSV is missing required columns: {', '.join(missing)}"
                )

            rows: list[CsvTransaction] = []
            for index, row in enumerate(reader, start=2):
                if row is None:
                    continue
                rows.append(
                    CsvTransaction(
                        row_number=index,
                        transaction_date=parse_created_at(row.get("createdAt", "")),
                        merchant=(row.get("merchant") or "").strip(),
                        bilt_category=(row.get("category") or "UNCATEGORIZED").strip() or "UNCATEGORIZED",
                        amount=parse_decimal(row.get("amount", ""), field_name="amount"),
                        currency=(row.get("currency") or "").strip(),
                        status=(row.get("status") or "").strip(),
                        transaction_type=(row.get("type") or "").strip(),
                        subtype=(row.get("subtype") or "").strip(),
                    )
                )
    except OSError as exc:
        raise EmpowerError(f"Could not read CSV file {csv_path}: {exc}") from exc

    if not rows:
        raise EmpowerError("The CSV file does not contain any transaction rows.")
    return rows


def choose_account(accounts: list[EmpowerAccount]) -> EmpowerAccount:
    print("\nAvailable Empower accounts:")
    for index, account in enumerate(accounts, start=1):
        balance = format_decimal(account.current_balance)
        label = f"{account.firm_name} | {account.name} | {account.product_type} | balance {balance}"
        print(f"  {index}. {label}")

    while True:
        raw = input("Choose an account number: ").strip()
        if not raw.isdigit():
            print_warn("Please enter a numeric account choice.")
            continue

        selection = int(raw) - 1
        if 0 <= selection < len(accounts):
            return accounts[selection]
        print_warn("Choice out of range.")


def build_category_indexes(categories: list[EmpowerCategory]) -> tuple[dict[int, EmpowerCategory], dict[str, list[EmpowerCategory]]]:
    by_id = {category.category_id: category for category in categories}
    by_normalized_name: dict[str, list[EmpowerCategory]] = {}
    for category in categories:
        by_normalized_name.setdefault(normalize_text(category.name), []).append(category)
    return by_id, by_normalized_name


def match_exact_category(bilt_category: str, categories: list[EmpowerCategory]) -> EmpowerCategory | None:
    category_lower = bilt_category.casefold()
    for category in categories:
        if category.name.casefold() == category_lower:
            return category
    return None


def match_normalized_category(
    bilt_category: str,
    by_normalized_name: dict[str, list[EmpowerCategory]],
) -> EmpowerCategory | None:
    matches = by_normalized_name.get(normalize_text(bilt_category), [])
    if len(matches) == 1:
        return matches[0]
    return None


def find_mapping_category(
    bilt_category: str,
    mapping_store: dict[str, dict[str, Any]],
    categories: list[EmpowerCategory],
    category_by_id: dict[int, EmpowerCategory],
) -> EmpowerCategory | None:
    entry = mapping_store.get(bilt_category)
    if entry is None:
        normalized = normalize_text(bilt_category)
        for key, value in mapping_store.items():
            if normalize_text(key) == normalized:
                entry = value
                break

    if not isinstance(entry, dict):
        return None

    category_id = entry.get("empower_category_id")
    if isinstance(category_id, int) and category_id in category_by_id:
        return category_by_id[category_id]

    category_name = entry.get("empower_category_name")
    if isinstance(category_name, str):
        category_lower = category_name.casefold()
        for category in categories:
            if category.name.casefold() == category_lower:
                return category

    return None


def collect_fuzzy_suggestions(bilt_category: str, categories: list[EmpowerCategory], limit: int = 5) -> list[EmpowerCategory]:
    names = [category.name for category in categories]
    suggestion_names = get_close_matches(bilt_category, names, n=limit, cutoff=0.35)
    suggestions: list[EmpowerCategory] = []
    for name in suggestion_names:
        for category in categories:
            if category.name == name:
                suggestions.append(category)
                break
    return suggestions


def preview_categories(categories: list[EmpowerCategory], query: str | None = None, limit: int = 10) -> list[EmpowerCategory]:
    ordered = sorted(categories, key=lambda item: item.name.casefold())
    if query:
        normalized_query = normalize_text(query)
        filtered = [
            category
            for category in ordered
            if normalized_query in normalize_text(category.name)
        ]
        return filtered[:limit]
    return ordered[:limit]


def choose_existing_category(categories: list[EmpowerCategory], initial_query: str) -> EmpowerCategory:
    query = initial_query
    while True:
        matches = preview_categories(categories, query=query)
        if not matches:
            print_warn("No categories matched that search. Try another term.")
        else:
            print("\nMatching Empower categories:")
            for index, category in enumerate(matches, start=1):
                print(f"  {index}. {category.name} [{category.category_type}] (id={category.category_id})")

        raw = input(
            "Choose a category number, type a new search term, 'list' for the first categories, or 'cancel': "
        ).strip()
        if raw.lower() == "cancel":
            raise EmpowerError("Category selection cancelled.")
        if raw.lower() == "list":
            query = ""
            continue
        if raw.isdigit() and matches:
            selection = int(raw) - 1
            if 0 <= selection < len(matches):
                return matches[selection]
            print_warn("Choice out of range.")
            continue
        if raw:
            query = raw
            continue
        print_warn("Please choose a category number or enter a search term.")


def prompt_resolution_action(transaction: CsvTransaction, suggestions: list[EmpowerCategory]) -> str:
    print(
        "\nUnmatched category for row "
        f"{transaction.row_number}: {transaction.merchant} | amount {format_decimal(transaction.amount)} "
        f"| Bilt category '{transaction.bilt_category}'"
    )
    if suggestions:
        print("Suggested Empower categories:")
        for index, category in enumerate(suggestions, start=1):
            print(f"  {index}. {category.name} [{category.category_type}] (id={category.category_id})")
    print("Options: [m]ap existing category, [c]reate new category, [s]kip transaction")

    while True:
        choice = input("Choose an option (m/c/s): ").strip().lower()
        if choice in {"m", "c", "s"}:
            return choice
        print_warn("Please enter 'm', 'c', or 's'.")


def create_new_category_flow(client: EmpowerClient, categories: list[EmpowerCategory]) -> EmpowerCategory:
    while True:
        name = ask_non_empty("New Empower category name")
        try:
            category = client.add_category(name)
        except EmpowerError as exc:
            print_warn(str(exc))
            retry = input("Try a different category name? [y/N]: ").strip().lower()
            if retry != "y":
                raise
            continue
        categories.append(category)
        print_info(
            f"Created Empower category '{category.name}' with id {category.category_id}."
        )
        return category


def assign_transaction_category(transaction: CsvTransaction, category: EmpowerCategory) -> None:
    transaction.empower_category = category
    transaction.skipped = False


def skip_transaction(transaction: CsvTransaction) -> None:
    transaction.empower_category = None
    transaction.skipped = True


def remember_mapping(
    mapping_store: dict[str, dict[str, Any]],
    bilt_category: str,
    category: EmpowerCategory,
) -> None:
    mapping_store[bilt_category] = {
        "empower_category_id": category.category_id,
        "empower_category_name": category.name,
        "empower_category_type": category.category_type,
    }


def apply_resolution_action(
    transaction: CsvTransaction,
    client: EmpowerClient,
    categories: list[EmpowerCategory],
    suggestions: list[EmpowerCategory],
    *,
    save_mapping: bool,
    mapping_store: dict[str, dict[str, Any]] | None,
) -> bool:
    while True:
        action = prompt_resolution_action(transaction, suggestions)
        if action == "m":
            try:
                chosen_category = choose_existing_category(categories, transaction.bilt_category)
            except EmpowerError as exc:
                print_warn(str(exc))
                continue
            assign_transaction_category(transaction, chosen_category)
            if save_mapping and mapping_store is not None:
                remember_mapping(mapping_store, transaction.bilt_category, chosen_category)
                print_info(
                    f"Saved mapping for Bilt category '{transaction.bilt_category}' -> '{chosen_category.name}'."
                )
            else:
                print_info(
                    f"Updated row {transaction.row_number} to Empower category '{chosen_category.name}'."
                )
            return False

        if action == "c":
            try:
                chosen_category = create_new_category_flow(client, categories)
            except EmpowerError as exc:
                print_warn(str(exc))
                continue
            assign_transaction_category(transaction, chosen_category)
            if save_mapping and mapping_store is not None:
                remember_mapping(mapping_store, transaction.bilt_category, chosen_category)
                print_info(
                    f"Saved mapping for Bilt category '{transaction.bilt_category}' -> '{chosen_category.name}'."
                )
            else:
                print_info(
                    f"Updated row {transaction.row_number} to new Empower category '{chosen_category.name}'."
                )
            return True

        skip_transaction(transaction)
        if save_mapping:
            print_info(f"Skipping row {transaction.row_number}.")
        else:
            print_info(f"Row {transaction.row_number} will be skipped from upload.")
        return False


def resolve_categories(
    transactions: list[CsvTransaction],
    client: EmpowerClient,
    categories: list[EmpowerCategory],
    mapping_store: dict[str, dict[str, Any]],
) -> None:
    category_by_id, by_normalized_name = build_category_indexes(categories)

    for transaction in transactions:
        mapped_category = find_mapping_category(
            transaction.bilt_category,
            mapping_store,
            categories,
            category_by_id,
        )
        if mapped_category is not None:
            assign_transaction_category(transaction, mapped_category)
            print_info(
                f"Used saved mapping for Bilt category '{transaction.bilt_category}' -> '{mapped_category.name}'."
            )
            continue

        exact_match = match_exact_category(transaction.bilt_category, categories)
        if exact_match is not None:
            assign_transaction_category(transaction, exact_match)
            continue

        normalized_match = match_normalized_category(transaction.bilt_category, by_normalized_name)
        if normalized_match is not None:
            assign_transaction_category(transaction, normalized_match)
            continue

        suggestions = collect_fuzzy_suggestions(transaction.bilt_category, categories)
        created_category = apply_resolution_action(
            transaction,
            client,
            categories,
            suggestions,
            save_mapping=True,
            mapping_store=mapping_store,
        )
        if created_category:
            category_by_id, by_normalized_name = build_category_indexes(categories)


def print_review(transactions: list[CsvTransaction]) -> list[CsvTransaction]:
    included = [transaction for transaction in transactions if not transaction.skipped]
    skipped = [transaction for transaction in transactions if transaction.skipped]

    if not included:
        raise EmpowerError("All transactions are currently skipped. Nothing is available to upload.")

    print("\nBatch review:")
    header = (
        f"{'No.':<4} {'Row':<4} {'Date':<10} {'Amount':>10} {'Bilt Category':<20} "
        f"{'Empower Category':<24} Merchant"
    )
    print(header)
    print("-" * len(header))
    for index, transaction in enumerate(included, start=1):
        merchant = transaction.merchant
        if len(merchant) > 40:
            merchant = f"{merchant[:37]}..."
        bilt_category = transaction.bilt_category[:20]
        empower_category = transaction.empower_category.name[:24] if transaction.empower_category else ""
        print(
            f"{index:<4} {transaction.row_number:<4} {transaction.transaction_date.isoformat():<10} "
            f"{format_decimal(transaction.amount):>10} {bilt_category:<20} {empower_category:<24} {merchant}"
        )

    print_info(
        f"Review contains {len(included)} upload candidates and {len(skipped)} skipped transactions."
    )
    return included


def review_and_confirm(
    transactions: list[CsvTransaction],
    client: EmpowerClient,
    categories: list[EmpowerCategory],
) -> list[CsvTransaction]:
    while True:
        included = print_review(transactions)
        print("Options: [a]ccept batch, [e]dit one transaction, [q]uit without uploading")
        choice = input("Choose an option (a/e/q): ").strip().lower()
        if choice == "a":
            return included
        if choice == "q":
            raise EmpowerError("Upload cancelled before confirmation.")
        if choice != "e":
            print_warn("Please enter 'a', 'e', or 'q'.")
            continue

        raw = input("Enter the review number of the transaction to edit: ").strip()
        if not raw.isdigit():
            print_warn("Please enter a numeric review number.")
            continue
        selection = int(raw) - 1
        if not (0 <= selection < len(included)):
            print_warn("Review number out of range.")
            continue

        transaction = included[selection]
        suggestions = collect_fuzzy_suggestions(transaction.bilt_category, categories)
        apply_resolution_action(
            transaction,
            client,
            categories,
            suggestions,
            save_mapping=False,
            mapping_store=None,
        )


def upload_transactions(
    client: EmpowerClient,
    account: EmpowerAccount,
    transactions: list[CsvTransaction],
) -> tuple[list[dict[str, Any]], list[tuple[CsvTransaction, str]]]:
    successes: list[dict[str, Any]] = []
    failures: list[tuple[CsvTransaction, str]] = []
    for transaction in transactions:
        try:
            response = client.create_transaction(account.account_id, transaction)
        except EmpowerError as exc:
            failures.append((transaction, str(exc)))
            continue
        successes.append(response)
    return successes, failures


def print_summary(
    uploaded: list[dict[str, Any]],
    failed: list[tuple[CsvTransaction, str]],
    skipped: list[CsvTransaction],
) -> None:
    print("\nUpload summary:")
    print(f"  Uploaded: {len(uploaded)}")
    print(f"  Failed:   {len(failed)}")
    print(f"  Skipped:  {len(skipped)}")

    if failed:
        print("\nFailures:")
        for transaction, message in failed:
            print(
                f"  Row {transaction.row_number} | {transaction.merchant} | "
                f"{format_decimal(transaction.amount)} | {message}"
            )


def main() -> int:
    args = parse_args()
    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        raise EmpowerError(f"CSV file does not exist: {csv_path}")
    if args.timeout <= 0:
        raise EmpowerError("--timeout must be greater than 0.")

    jsessionid = args.jsessionid or ask_non_empty("Enter Empower JSESSIONID")
    csrf = args.csrf or ask_non_empty("Enter Empower csrf token")
    mapping_file = Path(args.mapping_file)

    transactions = read_transactions(csv_path)
    mapping_store = load_mapping_file(mapping_file)

    client = EmpowerClient(jsessionid=jsessionid, csrf=csrf, timeout=args.timeout)

    print_info("Fetching Empower accounts.")
    accounts = client.get_accounts()
    account = choose_account(accounts)

    print_info("Fetching Empower categories.")
    categories = client.get_categories()
    resolve_categories(transactions, client, categories, mapping_store)

    confirmed_transactions = review_and_confirm(transactions, client, categories)
    skipped_transactions = [transaction for transaction in transactions if transaction.skipped]
    print_info(f"Uploading {len(confirmed_transactions)} transactions to {account.name}.")
    uploaded, failed = upload_transactions(client, account, confirmed_transactions)
    save_json_file(mapping_file, mapping_store)
    print_info(f"Saved category mappings to {mapping_file}.")
    print_summary(uploaded, failed, skipped_transactions)

    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print_error("Interrupted by user.")
        raise SystemExit(130)
    except EmpowerError as exc:
        print_error(str(exc))
        raise SystemExit(1)