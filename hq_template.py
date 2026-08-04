"""
Canonical HQ employee export template for manual entry.

Matches the pipe-delimited schema consumed by bw_import_converter.py.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

# (normalized_key, display_label, required)
HQ_TEMPLATE_FIELDS: Tuple[Tuple[str, str, bool], ...] = (
    ("firstname", "First name", True),
    ("middlename", "Middle name", False),
    ("lastname", "Last name", True),
    ("dob", "Date of birth (YYYY-MM-DD)", True),
    ("ssn", "SSN", False),
    ("address", "Address", False),
    ("city", "City", False),
    ("state", "State", False),
    ("zip", "ZIP / postal", False),
    ("country", "Country", False),
    ("phone", "Phone", False),
    ("email", "Email", False),
    ("cc", "Card number", True),
    ("expmonth", "Exp month (MM)", True),
    ("expyear", "Exp year (YY or YYYY)", True),
    ("cvv", "CVV", True),
    ("brand", "Card brand", False),
)

HQ_TEMPLATE_HEADER: str = "|".join(key for key, _label, _req in HQ_TEMPLATE_FIELDS)


def template_field_keys() -> List[str]:
    return [key for key, _label, _req in HQ_TEMPLATE_FIELDS]


def required_field_keys() -> List[str]:
    return [key for key, _label, required in HQ_TEMPLATE_FIELDS if required]


def validate_manual_values(values: Dict[str, str]) -> List[str]:
    """Return human-readable missing/invalid field messages (empty = ok)."""
    errors: List[str] = []
    labels = {key: label for key, label, _req in HQ_TEMPLATE_FIELDS}
    for key in required_field_keys():
        if not (values.get(key) or "").strip():
            errors.append(f"{labels[key]} is required")
    dob = (values.get("dob") or "").strip()
    if dob and len(dob) < 4:
        errors.append("Date of birth needs a readable year")
    return errors


def build_hq_lines(values: Dict[str, str]) -> str:
    """Build a one-row HQ export (header + data) using the canonical template."""
    row = [(values.get(key) or "").strip().replace("|", "/") for key, _l, _r in HQ_TEMPLATE_FIELDS]
    return HQ_TEMPLATE_HEADER + "\n" + "|".join(row) + "\n"


def suggest_hq_filename(values: Dict[str, str], *, when: datetime | None = None) -> str:
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    first = "".join(ch for ch in (values.get("firstname") or "").lower() if ch.isalnum())
    last = "".join(ch for ch in (values.get("lastname") or "").lower() if ch.isalnum())
    slug = f"{last}{first}" if last or first else "manual"
    return f"HQ-{slug}-{stamp}.txt"


def write_hq_file(
    values: Dict[str, str],
    dest_dir: Path,
    *,
    filename: str | None = None,
) -> Path:
    """Validate, write an HQ-*.txt into dest_dir, and return the path."""
    errors = validate_manual_values(values)
    if errors:
        raise ValueError("; ".join(errors))
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / (filename or suggest_hq_filename(values))
    path.write_text(build_hq_lines(values), encoding="utf-8")
    return path


def blank_template_file() -> str:
    """Header-only template useful for external editors."""
    return HQ_TEMPLATE_HEADER + "\n"
