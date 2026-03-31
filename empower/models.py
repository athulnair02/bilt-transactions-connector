"""Domain models for Empower transaction uploads."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal


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