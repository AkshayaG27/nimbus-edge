"""
Nimbus Schema Validation Helper (Aligned with vaibhav3000/nimbus/contracts/)
Validates Leases, Escalations, and Robot Sub-tasks against team wire contracts.
Enforces cross-field rules: deadline_ts <= expiry_ts, zone boundary checks, version checks.
"""

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Dict, Tuple
import jsonschema

SCHEMA_DIR = Path(__file__).parent


def _load_schema(filename: str) -> Dict[str, Any]:
    path = SCHEMA_DIR / filename
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


LEASE_SCHEMA = _load_schema("lease.schema.json")
ESCALATION_SCHEMA = _load_schema("escalation.schema.json")
ROBOT_SUBTASK_SCHEMA = _load_schema("robot_subtask.schema.json")


def _parse_iso(ts_str: str) -> datetime:
    """Parses ISO 8601 timestamp string into datetime."""
    # Replace Z with +00:00 for fromisoformat compatibility in python 3.10
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    return datetime.fromisoformat(ts_str)


def validate_lease(lease_dict: Dict[str, Any]) -> Tuple[bool, str]:
    """Validates lease dictionary against schema and cross-field constraints."""
    try:
        # Create validator with local schema resolver for $ref
        resolver = jsonschema.RefResolver(
            base_uri=f"{SCHEMA_DIR.as_uri()}/",
            referrer=LEASE_SCHEMA,
            store={"robot_subtask.schema.json": ROBOT_SUBTASK_SCHEMA}
        )
        validator = jsonschema.Draft202012Validator(LEASE_SCHEMA, resolver=resolver)
        errors = list(validator.iter_errors(lease_dict))
        if errors:
            return False, f"Schema error: {errors[0].message}"

        # Cross-field constraint: deadline_ts <= expiry_ts
        deadline = _parse_iso(lease_dict["deadline_ts"])
        expiry = _parse_iso(lease_dict["expiry_ts"])
        if deadline > expiry:
            return False, f"deadline_ts ({deadline}) cannot exceed expiry_ts ({expiry})"

        return True, "Valid"
    except Exception as e:
        return False, f"Validation exception: {e}"


def validate_escalation(escalation_dict: Dict[str, Any]) -> Tuple[bool, str]:
    """Validates escalation dictionary against schema."""
    try:
        validator = jsonschema.Draft202012Validator(ESCALATION_SCHEMA)
        errors = list(validator.iter_errors(escalation_dict))
        if errors:
            return False, f"Schema error: {errors[0].message}"
        return True, "Valid"
    except Exception as e:
        return False, f"Validation exception: {e}"


def validate_robot_subtask(subtask_dict: Dict[str, Any]) -> Tuple[bool, str]:
    """Validates robot sub-task command against schema."""
    try:
        validator = jsonschema.Draft202012Validator(ROBOT_SUBTASK_SCHEMA)
        errors = list(validator.iter_errors(subtask_dict))
        if errors:
            return False, f"Schema error: {errors[0].message}"
        return True, "Valid"
    except Exception as e:
        return False, f"Validation exception: {e}"


def is_coordinate_in_allowed_zones(
    zone_name: str,
    allowed_zones: list,
    forbidden_zones: list
) -> bool:
    """Verifies that a target zone is permitted and not forbidden (FR-5)."""
    if zone_name in forbidden_zones:
        return False
    return zone_name in allowed_zones
