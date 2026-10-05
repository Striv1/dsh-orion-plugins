"""Explicit JSON conversion for typed query values in runtime evidence."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any


def query_json_scalar(value: Any) -> Any:
    """Preserve date ISO syntax and decimal precision; reject opaque objects."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite decimal in query evidence")
        return str(value)
    raise TypeError(f"Unsupported query evidence type: {type(value).__name__}")
