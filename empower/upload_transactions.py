#!/usr/bin/env python3
"""Upload exported Bilt transactions into Empower."""

from __future__ import annotations

import argparse
import csv
import logging
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from client import EmpowerClient
from helpers import (
    ask_non_empty,
    format_decimal,
    load_mapping_file,
    normalize_text,
    parse_created_at,
    parse_decimal,
    save_json_file,
)
from models import CsvTransaction, EmpowerAccount, EmpowerCategory, EmpowerError

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAPPING_FILE = Path(__file__).with_name("category_mappings.json")
LOG_FORMAT = "[%(levelname)s] %(message)s"

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload Bilt transactions CSV rows into Empower.")
    parser.add_argument("csv_path", help="Path to a Bilt-exported CSV file.")
    parser.add_argument("-j", "--jsessionid", help="Empower JSESSIONID cookie value.")
    parser.add_argument("-c", "--csrf", help="Empower csrf token value.")
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
            logger.warning("Please enter a numeric account choice.")
            continue

        selection = int(raw) - 1
        if 0 <= selection < len(accounts):
            return accounts[selection]
        logger.warning("Choice out of range.")


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
            logger.warning("No categories matched that search. Try another term.")
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
            logger.warning("Choice out of range.")
            continue
        if raw:
            query = raw
            continue
        logger.warning("Please choose a category number or enter a search term.")


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
        logger.warning("Please enter 'm', 'c', or 's'.")


def create_new_category_flow(client: EmpowerClient, categories: list[EmpowerCategory]) -> EmpowerCategory:
    while True:
        name = ask_non_empty("New Empower category name")
        try:
            category = client.add_category(name)
        except EmpowerError as exc:
            logger.warning("%s", exc)
            retry = input("Try a different category name? [y/N]: ").strip().lower()
            if retry != "y":
                raise
            continue
        categories.append(category)
        logger.info("Created Empower category '%s' with id %s.", category.name, category.category_id)
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
                logger.warning("%s", exc)
                continue
            assign_transaction_category(transaction, chosen_category)
            if save_mapping and mapping_store is not None:
                remember_mapping(mapping_store, transaction.bilt_category, chosen_category)
                logger.info(
                    "Saved mapping for Bilt category '%s' -> '%s'.",
                    transaction.bilt_category,
                    chosen_category.name,
                )
            else:
                logger.info(
                    "Updated row %s to Empower category '%s'.",
                    transaction.row_number,
                    chosen_category.name,
                )
            return False

        if action == "c":
            try:
                chosen_category = create_new_category_flow(client, categories)
            except EmpowerError as exc:
                logger.warning("%s", exc)
                continue
            assign_transaction_category(transaction, chosen_category)
            if save_mapping and mapping_store is not None:
                remember_mapping(mapping_store, transaction.bilt_category, chosen_category)
                logger.info(
                    "Saved mapping for Bilt category '%s' -> '%s'.",
                    transaction.bilt_category,
                    chosen_category.name,
                )
            else:
                logger.info(
                    "Updated row %s to new Empower category '%s'.",
                    transaction.row_number,
                    chosen_category.name,
                )
            return True

        skip_transaction(transaction)
        if save_mapping:
            logger.info("Skipping row %s.", transaction.row_number)
        else:
            logger.info("Row %s will be skipped from upload.", transaction.row_number)
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
            logger.info(
                "Used saved mapping for Bilt category '%s' -> '%s'.",
                transaction.bilt_category,
                mapped_category.name,
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

    logger.info(
        "Review contains %s upload candidates and %s skipped transactions.",
        len(included),
        len(skipped),
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
            logger.warning("Please enter 'a', 'e', or 'q'.")
            continue

        raw = input("Enter the review number of the transaction to edit: ").strip()
        if not raw.isdigit():
            logger.warning("Please enter a numeric review number.")
            continue
        selection = int(raw) - 1
        if not (0 <= selection < len(included)):
            logger.warning("Review number out of range.")
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
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

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

    logger.info("Fetching Empower accounts.")
    accounts = client.get_accounts()
    account = choose_account(accounts)

    logger.info("Fetching Empower categories.")
    categories = client.get_categories()
    resolve_categories(transactions, client, categories, mapping_store)

    confirmed_transactions = review_and_confirm(transactions, client, categories)
    skipped_transactions = [transaction for transaction in transactions if transaction.skipped]
    logger.info("Uploading %s transactions to %s.", len(confirmed_transactions), account.name)
    uploaded, failed = upload_transactions(client, account, confirmed_transactions)
    save_json_file(mapping_file, mapping_store)
    logger.info("Saved category mappings to %s.", mapping_file)
    print_summary(uploaded, failed, skipped_transactions)

    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logger.error("Interrupted by user.")
        raise SystemExit(130)
    except EmpowerError as exc:
        logger.error("%s", exc)
        raise SystemExit(1)
