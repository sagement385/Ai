from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlparse


ALLOWED_ACTUAL_SOURCE_TYPES = {
    "api",
    "official_report",
    "public_dataset",
    "field_observation",
    "verified_gis",
    "engineering_report",
}


def _has_http_url(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _parse_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def source_metadata(record: dict[str, Any]) -> dict[str, Any]:
    if isinstance(record.get("source"), dict):
        return record["source"]
    return {
        "source_name": record.get("source_name"),
        "source_url": record.get("source_url"),
        "source_type": record.get("source_type"),
    }


def validate_source(
    record: dict[str, Any],
    *,
    require_observed_at: bool = True,
    require_unit: bool = False,
    numeric_field: str | None = None,
    strict_actual_source: bool = True,
) -> dict[str, Any]:
    source = source_metadata(record)
    if not source.get("source_name"):
        return _invalid("missing_source_name")
    if not _has_http_url(source.get("source_url")):
        return _invalid("missing_source_url")
    source_type = source.get("source_type")
    if not source_type:
        return _invalid("missing_source_type")
    if strict_actual_source and source_type not in ALLOWED_ACTUAL_SOURCE_TYPES:
        return _invalid("source_type_not_allowed_for_actual_mode")
    if require_observed_at and not _parse_datetime(record.get("observed_at")):
        return _invalid("missing_or_invalid_observed_at")
    if require_unit and not record.get("unit"):
        return _invalid("missing_unit")
    if numeric_field:
        number = _parse_number(record.get(numeric_field))
        if number is None:
            return _invalid("numeric_value_not_parseable")
    return {
        "valid": True,
        "reason": "ok",
        "action": "include",
        "source": source,
    }


def validate_sourced_value(
    value_obj: Any,
    *,
    field_name: str,
    strict_actual_source: bool = True,
) -> dict[str, Any]:
    if not isinstance(value_obj, dict):
        return _invalid(f"{field_name}_missing")
    record = {
        "value": value_obj.get("value"),
        "unit": value_obj.get("unit"),
        "observed_at": value_obj.get("observed_at", value_obj.get("source_observed_at")),
        "source": value_obj.get("source", value_obj),
    }
    return validate_source(
        record,
        require_observed_at=False,
        require_unit=True,
        numeric_field="value",
        strict_actual_source=strict_actual_source,
    )


def usable_number(value_obj: Any) -> float | None:
    if isinstance(value_obj, dict):
        return _parse_number(value_obj.get("value"))
    return _parse_number(value_obj)


def _invalid(reason: str) -> dict[str, Any]:
    return {
        "valid": False,
        "reason": reason,
        "action": "exclude_from_risk_calculation",
    }

