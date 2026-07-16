"""
Bitwarden Import Converter
--------------------------

Converts a structured text file (HQ-*.txt or HQ-*.rtf) containing
employee details into a Bitwarden-compatible JSON import file.

The script expects the input file to contain records separated by '---'.
Each record can have sections like [Personal Details], [Work Login], and [Work Card].

Example Input Format:

    [Personal Details]
    Full Name: Cameron Cohen
    Birth Date: 1994-01-01

    [Work Login]
    Username: cameroncohen1994

    ---

    [Personal Details]
    Full Name: Jane Doe
    ...
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _parse_record_sections(record_text: str) -> Dict[str, Dict[str, str]]:
    """Parses a single employee record into its constituent sections."""
    sections: Dict[str, Dict[str, str]] = {}
    # Split by section headers like [Section Name]
    raw_sections = re.split(r"\[([^\]]+)\]", record_text)
    
    # The first element is usually empty, so we start from index 1
    it = iter(raw_sections[1:])
    for section_name in it:
        content = next(it, "").strip()
        details = {}
        for line in content.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                details[key.strip()] = value.strip()
        sections[section_name.strip()] = details
    return sections


def _create_login_item(details: Dict[str, str], full_name: str, password: str) -> Dict[str, Any]:
    """Creates a Bitwarden 'login' item."""
    return {
        "type": 1,
        "name": f"{full_name} — Work Login",
        "notes": None,
        "favorite": False,
        "login": {
            "uris": [],
            "username": details.get("Username"),
            "password": password,
            "totp": None,
        },
    }


def _create_note_item(details: Dict[str, str], full_name: str) -> Dict[str, Any]:
    """Creates a Bitwarden 'secure note' item."""
    note_content = "\n".join(f"{key}: {value}" for key, value in details.items())
    return {
        "type": 2,
        "name": f"{full_name} — Personal Details",
        "notes": note_content,
        "favorite": False,
    }


def _create_card_item(details: Dict[str, str], full_name: str) -> Dict[str, Any]:
    """Creates a Bitwarden 'card' item."""
    exp_month, exp_year = None, None
    if "Expiration" in details:
        match = re.match(r"(\d{2})/(\d{2,4})", details["Expiration"])
        if match:
            exp_month = match.group(1)
            year_part = match.group(2)
            exp_year = f"20{year_part}" if len(year_part) == 2 else year_part

    return {
        "type": 3,
        "name": f"{full_name} — Work Card",
        "notes": None,
        "favorite": False,
        "card": {
            "cardholderName": details.get("Cardholder Name"),
            "brand": "Visa",  # Default or infer from number
            "number": details.get("Card Number"),
            "expMonth": exp_month,
            "expYear": exp_year,
            "code": details.get("CVV"),
        },
    }


def convert_file_to_bitwarden_json(
    source_path: Path, output_path: Path, initial_password: str
) -> Dict[str, Any]:
    """
    Reads a structured text file and converts it to a Bitwarden JSON file.
    Returns statistics about the conversion, including usernames found.
    """
    content = source_path.read_text(encoding="utf-8")
    # Simple RTF stripper
    content = re.sub(r"\{\*?\\[^{}]+}|[{}]|\\\n", "", content)

    records = content.split("---")
    all_items: List[Dict[str, Any]] = []
    usernames: List[Tuple[str, str, str]] = []

    for record in records:
        if not record.strip():
            continue

        sections = _parse_record_sections(record)
        personal_details = sections.get("Personal Details", {})
        full_name = personal_details.get("Full Name")

        if not full_name:
            continue

        if "Work Login" in sections:
            login_details = sections["Work Login"]
            all_items.append(_create_login_item(login_details, full_name, initial_password))
            if login_details.get("Username"):
                usernames.append((full_name, login_details["Username"], initial_password))

        if "Personal Details" in sections:
            all_items.append(_create_note_item(personal_details, full_name))

        if "Work Card" in sections:
            all_items.append(_create_card_item(sections["Work Card"], full_name))

    bw_json: Dict[str, Any] = {"encrypted": False, "folders": [], "items": all_items}
    output_path.write_text(json.dumps(bw_json, indent=2), encoding="utf-8")

    return {"items_created": len(all_items), "usernames": usernames}