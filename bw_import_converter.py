#!/usr/bin/env python3

import csv
import json
import sys
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, TypedDict

# --- Configuration ---

# Per the spec, header matching is case-insensitive and ignores surrounding whitespace.
# These are the normalized headers for required columns.
REQUIRED_COLUMNS = {'firstname', 'lastname', 'cc', 'expmonth', 'expyear', 'cvv'}

# Common variants for the Date of Birth column header.
DOB_COLUMN_VARIANTS = {'dob', 'dateofbirth', 'birthdate'}

# Mapping of normalized source headers to Bitwarden's native Card fields.
NATIVE_CARD_MAP = {
    'cc': 'number',
    'expmonth': 'expMonth',
    'expyear': 'expYear',
    'cvv': 'code',
    'name': 'cardholderName',  # This is cardholder name from the input file
    'brand': 'brand'
}

# Normalized headers for fields that should be custom fields on the Card item.
CARD_CUSTOM_FIELD_CANDIDATES = {'_id', 'bin', 'base', 'level', 'exp', 'bank', 'type'}

# All fields related to the Card item, to be excluded from the Identity item.
ALL_CARD_FIELDS = set(NATIVE_CARD_MAP.keys()) | CARD_CUSTOM_FIELD_CANDIDATES

# Mapping of normalized source headers to Bitwarden's native Identity fields.
NATIVE_IDENTITY_MAP = {
    'title': 'title',
    'firstname': 'firstName',
    'middlename': 'middleName',
    'lastname': 'lastName',
    'address': 'address1',
    'address1': 'address1',
    'addressline1': 'address1',
    'address2': 'address2',
    'addressline2': 'address2',
    'address3': 'address3',
    'addressline3': 'address3',
    'city': 'city',
    'state': 'state',
    'province': 'state',
    'stateprovince': 'state',
    'postalcode': 'postalCode',
    'zip': 'postalCode',
    'zipcode': 'postalCode',
    'country': 'country',
    'company': 'company',
    'email': 'email',
    'emailaddress': 'email',
    'phone': 'phone',
    'phonenumber': 'phone',
    'ssn': 'ssn',
    'socialsecuritynumber': 'ssn',
    'passportnumber': 'passportNumber',
    'dl': 'licenseNumber',
    'licensenumber': 'licenseNumber',
    'notes': 'notes',
    'note': 'notes'
}

class StatsDict(TypedDict):
    rows_read: int
    items_generated: int
    skipped_rows: Dict[str, List[int]]

class BitwardenFolder(TypedDict):
    id: str
    name: str

class BitwardenExport(TypedDict):
    encrypted: bool
    folders: List[BitwardenFolder]
    items: List[Dict[str, Any]]


class BitwardenConverter:
    """
    Converts a pipe-delimited employee export into a Bitwarden JSON import file.
    """

    def __init__(self, input_path: Path, output_path: Path, password: str):
        self.input_path = input_path
        self.output_path = output_path
        self.password: str = password
        self.stats: StatsDict = {
            "rows_read": 0,
            "items_generated": 0,
            "skipped_rows": defaultdict(list)
        }
        self.generated_usernames: Set[str] = set()

    def run(self, delete_input: bool = False, raise_on_error: bool = False) -> Dict[str, Any]:
        """Orchestrates the conversion process. Returns conversion stats."""
        print(f"\n--- Processing file: {self.input_path.name} ---")
        if self.input_path.suffix.lower() == '.rtf':
            print("Warning: Treating '.rtf' file as plain text. This may not work if it contains rich text formatting.")

        successful_conversion = False
        employees: List[Dict[str, str]] = []

        try:
            items, employees = self._process_input_file()

            if not items:
                print("\nWarning: No valid items were generated.", file=sys.stderr)
                self._print_summary()
                if raise_on_error:
                    raise ValueError(f"No valid items generated from {self.input_path.name}")
                sys.exit(1)

            self._write_output_file(items)
            successful_conversion = True
            self._print_summary()

        except Exception as e:
            if isinstance(e, ValueError) and raise_on_error:
                raise
            if isinstance(e, FileNotFoundError):
                print(f"Error: Input file not found at '{self.input_path}'", file=sys.stderr)
            else:
                print(f"\nAn unexpected error occurred: {e}", file=sys.stderr)
            if raise_on_error:
                raise
            sys.exit(1)
        finally:
            if delete_input and successful_conversion:
                try:
                    print(f"\n--> Deleting input file as requested: '{self.input_path}'")
                    self.input_path.unlink()
                    print(f"--> Successfully deleted input file.")
                except Exception as e:
                    print(f"\nError: Could not delete input file '{self.input_path}': {e}", file=sys.stderr)

        return {
            "rows_read": self.stats["rows_read"],
            "items_generated": self.stats["items_generated"],
            "items_created": self.stats["items_generated"],
            "employees": employees,
            "usernames": [(e["full_name"], e["username"], self.password) for e in employees],
            "output_path": str(self.output_path),
        }

    def _process_input_file(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
        """Reads and processes the source CSV, returning Bitwarden items and employee metadata."""
        items: List[Dict[str, Any]] = []
        employees: List[Dict[str, str]] = []
        with self.input_path.open(mode='r', encoding='utf-8', newline='') as infile:
            reader = csv.reader(infile, delimiter='|')
            
            try:
                original_header_list = next(reader)
            except StopIteration:
                print("Error: Input file is empty.", file=sys.stderr)
                return [], []

            header = [self._normalize_header(h) for h in original_header_list]

            if not self._validate_schema(header):
                return [], []

            header_map = {norm: orig for norm, orig in zip(header, original_header_list)}

            for i, row in enumerate(reader, start=2):
                self.stats["rows_read"] += 1
                if not any(row):
                    continue

                row_data: Dict[str, str] = dict(zip(header, row))
                
                first_name = row_data.get('firstname', '').strip()
                last_name = row_data.get('lastname', '').strip()

                if not all(row_data.get(col, '').strip() for col in REQUIRED_COLUMNS):
                    self.stats["skipped_rows"]["Missing required values"].append(i)
                    continue

                dob_str, dob_header_key = self._find_dob_value(row_data)
                if not dob_str or not dob_header_key:
                    self.stats["skipped_rows"]["Missing required values"].append(i)
                    continue

                birth_year = self._parse_birth_year(dob_str)
                if not birth_year:
                    self.stats["skipped_rows"]["Unreadable DOB"].append(i)
                    continue

                username = f"{first_name.replace(' ', '')}{last_name.replace(' ', '')}{birth_year}".lower()
                if username in self.generated_usernames:
                    self.stats["skipped_rows"]["Duplicate username"].append(i)
                    continue
                self.generated_usernames.add(username)

                full_name = f"{first_name} {last_name}"
                
                login_item = self._generate_login_item(full_name, username)
                identity_item = self._generate_identity_item(full_name, row_data, header_map, dob_str, dob_header_key)
                card_item = self._generate_card_item(full_name, row_data, header_map)
                
                items.extend([login_item, identity_item, card_item])
                self.stats["items_generated"] += 3
                employees.append({
                    "full_name": full_name,
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": f"{username}@outlook.com",
                })

        return items, employees

    def _normalize_header(self, header: str) -> str:
        """Lowercase, strip whitespace, and remove spaces for consistent matching."""
        return header.strip().lower().replace(' ', '')

    def _validate_schema(self, header: List[str]) -> bool:
        """Checks if all required columns are present in the header."""
        header_set = set(header)
        if not REQUIRED_COLUMNS.issubset(header_set):
            missing = REQUIRED_COLUMNS - header_set
            print(f"Error: Input file is missing required columns: {', '.join(missing)}", file=sys.stderr)
            return False
        if not DOB_COLUMN_VARIANTS.intersection(header_set):
            print(f"Error: Input file must contain a date of birth column. Accepted variants: {', '.join(DOB_COLUMN_VARIANTS)}", file=sys.stderr)
            return False
        return True

    def _find_dob_value(self, row_data: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
        """Finds the DOB value and its corresponding key from the row data."""
        for key in DOB_COLUMN_VARIANTS:
            if key in row_data and row_data[key].strip():
                return row_data[key].strip(), key
        return None, None

    def _parse_birth_year(self, dob_string: str) -> Optional[str]:
        """Tries to parse a date string and return a four-digit year."""
        formats = [
            "%Y-%m-%d",
            "%m/%d/%Y",
            "%m-%d-%Y",
            "%d-%b-%Y",
            "%B %d, %Y",
            "%Y",
        ]
        for fmt in formats:
            try:
                return str(datetime.strptime(dob_string, fmt).year)
            except ValueError:
                continue
        if dob_string.isdigit() and len(dob_string) == 4:
            return dob_string
        return None

    def _create_base_item(self, item_type: int, name: str) -> Dict[str, Any]:
        """Creates the common structure for a Bitwarden item."""
        return {
            "id": str(uuid.uuid4()),
            "organizationId": None,
            "folderId": None,
            "type": item_type,
            "reprompt": 0,
            "name": name,
            "notes": None,
            "favorite": False,
            "collectionIds": None,
        }

    def _generate_login_item(self, full_name: str, username: str) -> Dict[str, Any]:
        """Generates the Bitwarden 'Login' item dictionary."""
        item = self._create_base_item(1, f"{full_name} — Work Login")
        item["login"] = {
            "uris": [],
            "username": username,
            "password": self.password,
            "totp": None
        }
        item["fields"] = [{
            "name": "Email",
            "value": f"{username}@outlook.com",
            "type": 0
        }]
        return item

    def _generate_identity_item(self, full_name: str, row_data: Dict[str, str], header_map: Dict[str, str], dob_str: str, dob_header_key: str) -> Dict[str, Any]:
        """Generates the Bitwarden 'Identity' item dictionary."""
        item = self._create_base_item(2, f"{full_name} — Work Identity")
        identity_details: Dict[str, Any] = {}
        custom_fields: List[Dict[str, Any]] = []
        
        custom_fields.append({
            "name": "Date of Birth",
            "value": dob_str,
            "type": 0
        })

        processed_native_keys: Set[str] = set()

        for norm_header, value in row_data.items():
            if not value.strip():
                continue

            if norm_header in ALL_CARD_FIELDS:
                continue

            if norm_header in NATIVE_IDENTITY_MAP and norm_header not in processed_native_keys:
                bw_field = NATIVE_IDENTITY_MAP[norm_header]
                identity_details[bw_field] = value
                processed_native_keys.add(norm_header)
            elif norm_header not in NATIVE_IDENTITY_MAP and norm_header != dob_header_key:
                original_header_name = header_map.get(norm_header, norm_header)
                custom_fields.append({
                    "name": original_header_name,
                    "value": value,
                    "type": 0
                })

        item["identity"] = identity_details
        item["fields"] = custom_fields
        return item

    def _generate_card_item(self, full_name: str, row_data: Dict[str, str], header_map: Dict[str, str]) -> Dict[str, Any]:
        """Generates the Bitwarden 'Card' item dictionary."""
        item = self._create_base_item(3, f"{full_name} — Work Card")
        card_details: Dict[str, Any] = {}
        custom_fields: List[Dict[str, Any]] = []

        processed_native_keys: Set[str] = set()

        for norm_header, value in row_data.items():
            if not value.strip():
                continue

            if norm_header in NATIVE_CARD_MAP and norm_header not in processed_native_keys:
                bw_field = NATIVE_CARD_MAP[norm_header]
                card_details[bw_field] = value
                processed_native_keys.add(norm_header)
            elif norm_header in CARD_CUSTOM_FIELD_CANDIDATES:
                original_header_name = header_map.get(norm_header, norm_header)
                custom_fields.append({
                    "name": original_header_name,
                    "value": value,
                    "type": 0
                })

        item["card"] = card_details
        item["fields"] = custom_fields
        return item

    def _write_output_file(self, items: List[Dict[str, Any]]) -> None:
        """Writes the generated items to the target JSON file in Bitwarden format."""
        export_data: BitwardenExport = {
            "encrypted": False,
            "folders": [],
            "items": items
        }
        with self.output_path.open(mode='w', encoding='utf-8') as outfile:
            json.dump(export_data, outfile, indent=2)

    def _print_summary(self) -> None:
        """Prints a summary of the conversion process execution."""
        print("\n=== Conversion Summary ===")
        print(f"Rows Read:         {self.stats['rows_read']}")
        print(f"Items Generated:   {self.stats['items_generated']}")
        if self.stats["skipped_rows"]:
            print("\nSkipped Rows:")
            for reason, rows in self.stats["skipped_rows"].items():
                print(f"  - {reason}: {len(rows)} row(s) (indices: {rows[:10]}{'...' if len(rows) > 10 else ''})")
        print("==========================\n")


def convert_file_to_bitwarden_json(
    input_path,
    output_path,
    password: str,
    delete_input: bool = False,
) -> Dict[str, Any]:
    """Convert an HQ employee export into Bitwarden JSON. Raises on failure."""
    converter = BitwardenConverter(Path(input_path), Path(output_path), password)
    return converter.run(delete_input=delete_input, raise_on_error=True)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: ./bw_converter.py <input_file> <output_file> <default_password> [--delete-input]", file=sys.stderr)
        sys.exit(1)

    input_file_path = Path(sys.argv[1])
    output_file_path = Path(sys.argv[2])
    default_pw = sys.argv[3]
    should_delete = "--delete-input" in sys.argv

    converter = BitwardenConverter(input_file_path, output_file_path, default_pw)
    converter.run(delete_input=should_delete)