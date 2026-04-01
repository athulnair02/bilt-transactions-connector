#!/usr/bin/env python3
"""Fetch Empower transactions and delete selected manual transactions."""

from __future__ import annotations

import argparse
import datetime as dt
import logging

from .client import DEFAULT_TIMEOUT_SECONDS, EmpowerClient
from utils.helpers import ask_non_empty, format_decimal, prompt_date_range
from utils.errors import EmpowerError
from .models import EmpowerTransaction

LOG_FORMAT = "[%(levelname)s] %(message)s"

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retrieve Empower transactions for a date range and delete selected manual transactions."
    )
    parser.add_argument("-j", "--jsessionid", help="Empower JSESSIONID cookie value.")
    parser.add_argument("-c", "--csrf", help="Empower csrf token value.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    return parser.parse_args()


def truncate(value: str, *, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    if max_len <= 3:
        return value[:max_len]
    return f"{value[:max_len - 3]}..."


def print_transactions(transactions: list[EmpowerTransaction]) -> None:
    if not transactions:
        print("\nNo transactions found for the requested date range.")
        return

    headers = ["No.", "Date", "Amount", "Type", "Category", "Manual?"]
    rows: list[list[str]] = []
    for index, transaction in enumerate(transactions, start=1):
        rows.append(
            [
                str(index),
                transaction.transaction_date.isoformat(),
                format_decimal(transaction.amount),
                truncate(transaction.transaction_type or "", max_len=16),
                truncate(transaction.category_name or "(uncategorized)", max_len=20),
                "YES" if transaction.is_manual else "NO",
            ]
        )

    col_widths = [len(header) for header in headers]
    for row in rows:
        for idx, value in enumerate(row):
            col_widths[idx] = max(col_widths[idx], len(value))

    print("\nTransactions")
    print("  " + " | ".join(header.ljust(col_widths[idx]) for idx, header in enumerate(headers)))
    print("  " + "-+-".join("-" * width for width in col_widths))
    for row in rows:
        print("  " + " | ".join(value.ljust(col_widths[idx]) for idx, value in enumerate(row)))


def parse_selection(raw: str, count: int) -> list[int]:
    if not raw.strip():
        raise EmpowerError("Selection cannot be empty.")

    selected: set[int] = set()
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            if not start_text.strip().isdigit() or not end_text.strip().isdigit():
                raise EmpowerError(f"Invalid range token: {token!r}")
            start = int(start_text)
            end = int(end_text)
            if start > end:
                raise EmpowerError(f"Invalid range order: {token!r}")
            for value in range(start, end + 1):
                if not 1 <= value <= count:
                    raise EmpowerError(f"Selection index out of range: {value}")
                selected.add(value)
            continue

        if not token.isdigit():
            raise EmpowerError(f"Invalid selection token: {token!r}")
        value = int(token)
        if not 1 <= value <= count:
            raise EmpowerError(f"Selection index out of range: {value}")
        selected.add(value)

    if not selected:
        raise EmpowerError("Selection cannot be empty.")
    return sorted(selected)


def confirm_deletion(selected: list[EmpowerTransaction]) -> bool:
    print("\nSelected manual transactions for deletion:")
    for index, transaction in enumerate(selected, start=1):
        print(
            "  "
            f"{index}. {transaction.transaction_date.isoformat()} | "
            f"{format_decimal(transaction.amount)} | "
            f"{transaction.transaction_type or '(no type)'} | "
            f"{transaction.category_name or '(no category)'} | "
            f"id={transaction.user_transaction_id}"
        )

    confirmation = input(f"Delete these {len(selected)} transaction(s)? [y/N]: ").strip().lower()
    return confirmation == "y"


def delete_selected(
    client: EmpowerClient,
    selected: list[EmpowerTransaction],
    account_id: str,
) -> tuple[list[EmpowerTransaction], list[tuple[EmpowerTransaction, str]]]:
    deleted: list[EmpowerTransaction] = []
    failed: list[tuple[EmpowerTransaction, str]] = []

    for transaction in selected:
        try:
            client.delete_transaction(
                account_id=account_id,
                transaction_id=str(transaction.user_transaction_id),
            )
        except EmpowerError as exc:
            failed.append((transaction, str(exc)))
            continue
        deleted.append(transaction)

    return deleted, failed


def print_summary(
    deleted: list[EmpowerTransaction],
    failed: list[tuple[EmpowerTransaction, str]],
    skipped_non_manual: list[EmpowerTransaction],
) -> None:
    print("\nSummary")
    print(f"  Deleted: {len(deleted)}")
    print(f"  Failed: {len(failed)}")
    print(f"  Skipped (not manual): {len(skipped_non_manual)}")

    if skipped_non_manual:
        print("\nSkipped transactions (not manual):")
        for transaction in skipped_non_manual:
            print(
                "  "
                f"- {transaction.transaction_date.isoformat()} | "
                f"{format_decimal(transaction.amount)} | "
                f"{transaction.transaction_type or '(no type)'} | "
                f"{transaction.category_name or '(no category)'} | "
                f"id={transaction.user_transaction_id}"
            )

    if failed:
        print("\nDelete failures:")
        for transaction, error_text in failed:
            print(
                "  "
                f"- {transaction.transaction_date.isoformat()} | "
                f"{format_decimal(transaction.amount)} | "
                f"{transaction.transaction_type or '(no type)'} | "
                f"{transaction.category_name or '(no category)'} | "
                f"id={transaction.user_transaction_id} -> {error_text}"
            )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    args = parse_args()

    jsessionid = (args.jsessionid or "").strip() or ask_non_empty("Empower JSESSIONID")
    csrf = (args.csrf or "").strip() or ask_non_empty("Empower csrf")
    today = dt.date.today()
    start_date, end_date = prompt_date_range(
        default_start=today.replace(day=1),
        default_end=today,
    )

    client = EmpowerClient(jsessionid=jsessionid, csrf=csrf, timeout=args.timeout)

    accounts = client.get_accounts()
    account = client.choose_account(accounts)

    logger.info("Fetching categories...")
    categories = client.get_categories()

    logger.info("Fetching transactions from %s to %s...", start_date, end_date)
    transactions = client.get_transactions(account.account_id, start_date.isoformat(), end_date.isoformat(), categories=categories)

    if not transactions:
        print("\nNo transactions found in this date range.")
        return 0

    print_transactions(transactions)

    while True:
        raw_selection = input("\nEnter row numbers to delete (example: 1,3,5-7), or 'q' to quit: ").strip()
        if raw_selection.lower() == "q":
            print("No transactions deleted.")
            return 0

        try:
            selected_indices = parse_selection(raw_selection, len(transactions))
        except EmpowerError as exc:
            logger.warning("%s", exc)
            continue

        selected_transactions = [transactions[index - 1] for index in selected_indices]
        manual_selected = [transaction for transaction in selected_transactions if transaction.is_manual]
        skipped_non_manual = [transaction for transaction in selected_transactions if not transaction.is_manual]

        if not manual_selected:
            logger.warning("None of the selected rows are manual transactions. Please pick at least one manual row.")
            continue

        if skipped_non_manual:
            logger.warning(
                "%s selected transaction(s) are not manual and will be skipped.",
                len(skipped_non_manual),
            )

        if not confirm_deletion(manual_selected):
            print("Deletion cancelled. Choose another selection or type 'q' to quit.")
            continue

        deleted, failed = delete_selected(client, manual_selected, account_id=account.account_id)
        print_summary(deleted, failed, skipped_non_manual)
        return 0


def run() -> int:
    try:
        return main()
    except EmpowerError as exc:
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        return 130


if __name__ == "__main__":
    raise SystemExit(run())
