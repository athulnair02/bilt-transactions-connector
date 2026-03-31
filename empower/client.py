"""Empower API client and endpoint configuration."""

from __future__ import annotations

import logging
from typing import Any

import requests

from helpers import format_decimal, parse_decimal, sanitize_jsessionid
from models import CsvTransaction, EmpowerAccount, EmpowerCategory, EmpowerError

EMPOWER_API_BASE_URL = "https://pc-api.empower-retirement.com/api"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_CATEGORY_TYPE = "EXPENSE"
API_CLIENT = "WEB"

GET_TRANSACTIONS_ENDPOINT = "/transaction/getUserTransactions"
# TODO: Example format: "/transaction/deleteUserTransaction"
DELETE_TRANSACTION_ENDPOINT = ""

logger = logging.getLogger(__name__)


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
        logger.info("%s %s returned %s.", method, endpoint, response.status_code)

        try:
            payload = response.json()
        except ValueError as exc:
            raise EmpowerError(f"{method} {endpoint} did not return JSON.") from exc

        if not isinstance(payload, dict):
            raise EmpowerError(f"{method} {endpoint} returned a non-object JSON response.")

        sp_header = payload.get("spHeader")
        if isinstance(sp_header, dict):
            errors = sp_header.get("errors")
            if isinstance(errors, list) and errors:
                first_error = errors[0]
                if isinstance(first_error, dict):
                    err_msg = first_error.get("message")
                    if isinstance(err_msg, str) and err_msg.strip():
                        raise EmpowerError(
                            f"{method} {endpoint} returned an error. Response: {err_msg.strip()}"
                        )

        return payload

    def _resolve_endpoint(self, endpoint: str | None, configured: str, *, method_name: str) -> str:
        resolved = endpoint if endpoint is not None else configured
        if not resolved:
            raise EmpowerError(
                f"{method_name} endpoint is not configured. Set the constant in client.py or pass endpoint=..."
            )
        if not resolved.startswith("/"):
            raise EmpowerError(f"{method_name} endpoint must start with '/': {resolved}")
        return resolved

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

    def get_transactions(self, account_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        if start_date is None or end_date is None:
            raise EmpowerError("start_date and end_date cannot be None.")

        payload = self._request_json(
            "POST",
            "/transaction/getUserTransactions",
            data={
                "startDate": start_date,
                "endDate": end_date,
                "csrf": self.csrf,
                "apiClient": API_CLIENT,
            },
        )
        categories = payload.get("spData")
        if not isinstance(categories, list):
            raise EmpowerError("Category response does not contain a spData list.")
        
        sp_data = payload.get("spData")

        if not isinstance(sp_data, dict):
            raise EmpowerError("Get-transactions response does not contain a spData object.")
        
        transactions = sp_data.get("transactions")
        if not isinstance(transactions, list):
            raise EmpowerError("Get-transactions response spData does not contain a transactions list.")
        
        # The accountId returned in each transaction is in the format "XXX_XXX_12345", but we want to match just the "12345" part
        filtered_transactions = [tx for tx in transactions if str(tx.get("userAccountId")) == account_id]

        return filtered_transactions

    def delete_transaction(
        self,
        account_id: str,
        transaction_id: str,
    ) -> dict[str, Any]:
        if transaction_id is None:
            raise EmpowerError("transaction_id cannot be None.")

        payload = self._request_json(
            "POST",
            "/transaction/deleteManualTransaction",
            data={
                "userTransactionId": transaction_id,
                "userAccountId": account_id,
                "csrf": self.csrf,
                "apiClient": API_CLIENT,
            },
        )

        return payload
