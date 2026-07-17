#!/usr/bin/env python3
"""
Onboarding orchestrator: convert HQ exports → Bitwarden import →
partner account autofill → local lockdown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from account_automation import AccountCreator
from audit_logger import get_audit_logger
from bw_import_converter import convert_file_to_bitwarden_json
from data_retention import DataRetentionManager
from secure_delete import (
    DEFAULT_BW_SHRED_MODE,
    DEFAULT_LOCAL_DELETE_MODE,
    secure_delete_file,
)


@dataclass
class BitwardenConfig:
    collection_name: str = "Employee Onboarding"


@dataclass
class OnboardingConfig:
    bw: BitwardenConfig
    local_delete_mode: str = DEFAULT_LOCAL_DELETE_MODE
    bw_shred_mode: str = DEFAULT_BW_SHRED_MODE
    provision_outlook: bool = True
    provision_hyatt: bool = True
    provision_marriott: bool = True


class Onboarding:
    """Runs the employee onboarding pipeline against Downloads HQ-* files."""

    def __init__(
        self,
        bw_service: Any,
        retention_manager: Optional[DataRetentionManager] = None,
        account_creator: Optional[AccountCreator] = None,
    ):
        self.bw_service = bw_service
        self.retention_manager = retention_manager
        self.account_creator = account_creator or AccountCreator()
        self.audit = get_audit_logger()

    def run(
        self,
        downloads: Path,
        password: str,
        config: OnboardingConfig,
        session_log_path: Optional[Path] = None,
        progress_callback: Optional[Any] = None,
    ) -> None:
        def progress(step: str, detail: str = "") -> None:
            logging.info("%s %s", step, detail)
            if progress_callback:
                try:
                    progress_callback(step, detail)
                except Exception:
                    pass

        files = sorted(
            p
            for p in downloads.iterdir()
            if p.is_file() and p.name.startswith("HQ-") and p.suffix in {".txt", ".rtf"}
        )
        if not files:
            logging.warning("No matching employee export files in %s", downloads)
            return

        progress("intake", f"{len(files)} file(s)")
        collection_id = self.bw_service.get_collection_id(config.bw.collection_name)
        json_files: List[Path] = []
        all_employees: List[Dict[str, str]] = []
        associated_logs = [str(session_log_path)] if session_log_path else []

        self.audit.log_security_event(
            "import_start",
            f"Processing {len(files)} file(s) into collection '{config.bw.collection_name}'",
        )

        # 1) Convert HQ → Bitwarden JSON
        for src in files:
            out = src.with_suffix(".bw.json")
            progress("convert", src.name)
            stats = convert_file_to_bitwarden_json(src, out, password)
            json_files.append(out)
            for emp in stats.get("employees") or []:
                all_employees.append(emp)
                if self.retention_manager is not None:
                    file_date = datetime.fromtimestamp(src.stat().st_mtime).date().isoformat()
                    self.retention_manager.register_employee(
                        emp["full_name"],
                        file_date,
                        associated_logs=associated_logs,
                    )

        # 2) Import into Bitwarden
        progress("bitwarden", f"Importing {len(json_files)} file(s)")
        for jf in json_files:
            bw_json = jf.read_text(encoding="utf-8")
            self.bw_service.import_json(bw_json, collection_id)

        # 3) Partner account autofill (shared passphrase + firstnamelastnameYOB)
        progress("accounts", f"{len(all_employees)} employee(s)")
        for emp in all_employees:
            personal_data = {
                "full_name": emp.get("full_name", ""),
                "first_name": emp.get("first_name", ""),
                "last_name": emp.get("last_name", ""),
                "email": emp.get("email", ""),
                "password": password,
            }
            account_name = emp.get("username") or emp.get("full_name", "unknown")
            if config.provision_outlook:
                self.account_creator.create_outlook_account(personal_data, account_name)
            if config.provision_hyatt:
                self.account_creator.create_hyatt_account(personal_data, account_name)
            if config.provision_marriott:
                self.account_creator.create_marriott_account(personal_data, account_name)

        # 4) Lockdown — dispose local artifacts
        progress("lockdown", config.local_delete_mode)
        for jf in json_files:
            self._dispose_local(jf, config.local_delete_mode)
            for suffix in (".txt", ".rtf"):
                src = jf.with_suffix(suffix)
                if src.exists():
                    self._dispose_local(src, config.local_delete_mode)

        if config.bw_shred_mode != "off":
            self._shred_bitwarden(collection_id, config.bw_shred_mode)

        employee_count = len(all_employees)
        self.audit.log_import_operation(employee_count, config.bw.collection_name)
        self.audit.log_security_event(
            "import_complete",
            f"Imported {employee_count} employee(s) into '{config.bw.collection_name}'",
        )
        progress("done", f"{employee_count} employee(s)")
        logging.info("--- Onboarding pipeline complete (%d employees). ---", employee_count)

    def _dispose_local(self, path: Path, mode: str) -> None:
        try:
            secure_delete_file(path, mode=mode)
            self.audit.log_deletion("local_file", path.name, method=mode)
            logging.info("Disposed local file %s (%s)", path.name, mode)
        except Exception as e:
            logging.error("Failed to dispose %s: %s", path, e)
            path.unlink(missing_ok=True)

    def _shred_bitwarden(self, collection_id: str, mode: str) -> None:
        logging.warning("Shredding Bitwarden items (mode=%s)...", mode)
        items = self.bw_service.list_items(collection_id)
        for item in items:
            name = item.get("name", "Unknown Item")
            if mode == "onboarding_items":
                keep = not (
                    name.endswith("— Work Login")
                    or name.endswith("— Work Identity")
                    or name.endswith("— Work Card")
                    or name.endswith("— Personal Details")
                )
                if keep:
                    continue
            logging.info("Deleting Bitwarden item: %s", name)
            self.bw_service.delete_item(item["id"])
            self.audit.log_deletion("bitwarden_item", name, method=mode)
