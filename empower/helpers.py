"""Reusable helpers for Empower upload workflows."""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from models import EmpowerError

logger = logging.getLogger(__name__)


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


def ask_non_empty(prompt: str, default: str | None = None) -> str:
    while True:
        default_note = f" [{default}]" if default else ""
        value = input(f"{prompt}{default_note}: ").strip()
        if not value and default is not None:
            return default
        if value:
            return value
        logger.warning("This value cannot be empty.")