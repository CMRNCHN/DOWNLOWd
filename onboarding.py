#!/usr/bin/env python3
"""
Onboarding orchestrator: convert HQ exports → Bitwarden import →
partner account autofill → local lockdown.
"""

from __future__ import annotations

import logging
import os
import uuid
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

TEMP_DIR = Path.home() / ".downlowd_temp"


@dataclass
class BitwardenConfig:
    collection_name: str = "Personal Vault"


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
        profile_store: Optional[Any] = None,
        profile_sync: Optional[Any] = None,
    ):
        self.bw_service = bw_service
        self.retention_manager = retention_manager
        self.account_creator = account_creator or AccountCreator()
        self.profile_store = profile_store
        self.profile_sync = profile_sync
        self.audit = get_audit_logger()

    def run(
        self,
        downloads: Path,
        password: str,
        config: OnboardingConfig,
        session_log_path: Optional[Path] = None,
        progress_callback: Optional[Any] = None,
        account_confirmation_callback: Optional[Any] = None,
    ) -> None:
        """Run the pipeline and always audit terminal failures."""
        generated_json_files: List[Path] = []
        try:
            self._run_pipeline(
                downloads,
                password,
                config,
                session_log_path=session_log_path,
                progress_callback=progress_callback,
                account_confirmation_callback=account_confirmation_callback,
                generated_json_files=generated_json_files,
            )
        except Exception as exc:
            self.audit.log_security_event(
                "import_failed",
                f"Target '{config.bw.collection_name}': {type(exc).__name__}",
            )
            for generated_file in generated_json_files:
                if not generated_file.exists():
                    continue
                try:
                    self._dispose_local(generated_file, config.local_delete_mode)
                except Exception:
                    logging.exception(
                        "Emergency cleanup failed for generated vault file %s",
                        generated_file,
                    )
            raise

    def _run_pipeline(
        self,
        downloads: Path,
        password: str,
        config: OnboardingConfig,
        session_log_path: Optional[Path] = None,
        progress_callback: Optional[Any] = None,
        account_confirmation_callback: Optional[Any] = None,
        generated_json_files: Optional[List[Path]] = None,
    ) -> None:
        generated_json_files = generated_json_files if generated_json_files is not None else []

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
        collection = self.bw_service.resolve_collection(config.bw.collection_name)
        json_files: List[Path] = []
        source_by_json: Dict[Path, Path] = {}
        all_employees: List[Dict[str, str]] = []
        associated_logs = [str(session_log_path)] if session_log_path else []

        self.audit.log_security_event(
            "import_start",
            f"Processing {len(files)} file(s) into collection '{config.bw.collection_name}'",
        )

        # 1) Convert HQ → Bitwarden JSON
        TEMP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(TEMP_DIR, 0o700)
        for src in files:
            out = TEMP_DIR / f"{src.stem}-{uuid.uuid4().hex}.bw.json"
            generated_json_files.append(out)
            progress("convert", src.name)
            try:
                stats = convert_file_to_bitwarden_json(src, out, password)
            except ValueError as exc:
                logging.error("Skipping %s: %s", src.name, exc)
                progress("convert", f"skipped {src.name}: {exc}")
                continue
            if not stats.get("items_generated"):
                logging.error("Skipping %s: no valid items generated", src.name)
                progress("convert", f"skipped {src.name}: no valid items")
                continue
            json_files.append(out)
            source_by_json[out] = src
            for emp in stats.get("employees") or []:
                all_employees.append(emp)
                if self.profile_store is not None:
                    profile_record = self.profile_store.upsert(
                        employee_id=emp.get("employee_id"),
                        display_name=emp["full_name"],
                        first_name=emp.get("first_name", ""),
                        last_name=emp.get("last_name", ""),
                        username=emp.get("username", ""),
                        email=emp.get("email", ""),
                    )
                    transaction_db = getattr(self.retention_manager, "transaction_db", None)
                    link_employee = getattr(transaction_db, "link_employee", None)
                    if link_employee:
                        link_employee(
                            emp["full_name"],
                            profile_record["employee_id"],
                        )
                if self.retention_manager is not None:
                    file_date = datetime.fromtimestamp(src.stat().st_mtime).date().isoformat()
                    self.retention_manager.register_employee(
                        emp["full_name"],
                        file_date,
                        associated_logs=associated_logs,
                        aliases=[
                            emp.get("username", ""),
                            emp.get("email", ""),
                        ],
                        profile={
                            "first_name": emp.get("first_name", ""),
                            "last_name": emp.get("last_name", ""),
                            "username": emp.get("username", ""),
                            "email": emp.get("email", ""),
                        },
                    )

        if not json_files:
            raise RuntimeError("No valid employee export files could be converted.")

        # 2) Import into Bitwarden
        progress("bitwarden", f"Importing {len(json_files)} file(s)")
        for jf in json_files:
            bw_json = jf.read_text(encoding="utf-8")
            self.bw_service.import_json(bw_json, collection)
        if self.profile_sync is not None:
            progress("bitwarden", "Syncing employee profile references")
            self.profile_sync.sync_profiles()

        # 3) Partner accounts in dependency order: all Outlook, then Hyatt, then Marriott.
        account_records: List[Dict[str, Any]] = []
        for emp in all_employees:
            personal_data = {
                "full_name": emp.get("full_name", ""),
                "first_name": emp.get("first_name", ""),
                "last_name": emp.get("last_name", ""),
                "username": emp.get("username", ""),
                "email": emp.get("email", ""),
                "password": password,
            }
            account_name = emp.get("username") or emp.get("full_name", "unknown")
            account_records.append(
                {
                    "employee": emp,
                    "personal_data": personal_data,
                    "account_name": account_name,
                }
            )

        def confirmed(service: str, record: Dict[str, Any], result: Dict[str, Any]) -> bool:
            if account_confirmation_callback is None:
                return True
            return bool(
                account_confirmation_callback(
                    service,
                    record["employee"],
                    result,
                )
            )

        def reset_browser_session() -> None:
            reset_browser = getattr(self.account_creator, "reset_browser_session", None)
            if reset_browser:
                reset_browser()

        email_ready = list(account_records) if not config.provision_outlook else []
        try:
            if config.provision_outlook:
                for index, record in enumerate(account_records, start=1):
                    progress(
                        "accounts",
                        f"Outlook {index}/{len(account_records)} — {record['account_name']}",
                    )
                    result = self.account_creator.create_outlook_account(
                        record["personal_data"],
                        record["account_name"],
                    )
                    is_confirmed = confirmed("Outlook", record, result)
                    self._save_account_progress(
                        record["employee"]["full_name"],
                        "email",
                        is_confirmed,
                    )
                    reset_browser_session()
                    if is_confirmed:
                        email_ready.append(record)
                    else:
                        logging.warning(
                            "Outlook not confirmed for %s; skipping dependent hotel accounts",
                            record["account_name"],
                        )

            if config.provision_hyatt:
                for index, record in enumerate(email_ready, start=1):
                    progress(
                        "accounts",
                        f"Hyatt {index}/{len(email_ready)} — {record['account_name']}",
                    )
                    result = self.account_creator.create_hyatt_account(
                        record["personal_data"],
                        record["account_name"],
                    )
                    hyatt_confirmed = confirmed("Hyatt", record, result)
                    self._save_account_progress(
                        record["employee"]["full_name"],
                        "hyatt",
                        hyatt_confirmed,
                    )
                    if hyatt_confirmed:
                        self._bind_created_account(
                            record["employee"],
                            "hyatt_login",
                            "Hyatt",
                            password,
                            "https://www.hyatt.com/",
                        )
                    reset_browser_session()

            if config.provision_marriott:
                for index, record in enumerate(email_ready, start=1):
                    progress(
                        "accounts",
                        f"Marriott {index}/{len(email_ready)} — {record['account_name']}",
                    )
                    result = self.account_creator.create_marriott_account(
                        record["personal_data"],
                        record["account_name"],
                    )
                    marriott_confirmed = confirmed("Marriott", record, result)
                    self._save_account_progress(
                        record["employee"]["full_name"],
                        "marriott",
                        marriott_confirmed,
                    )
                    if marriott_confirmed:
                        self._bind_created_account(
                            record["employee"],
                            "marriott_login",
                            "Marriott",
                            password,
                            "https://www.marriott.com/",
                        )
                    reset_browser_session()
        finally:
            close_browser = getattr(self.account_creator, "close_browser", None)
            if close_browser:
                close_browser()

        # 4) Lockdown — dispose local artifacts
        progress("lockdown", config.local_delete_mode)
        for jf in json_files:
            self._dispose_local(jf, config.local_delete_mode)
            src = source_by_json[jf]
            if src.exists():
                self._dispose_local(src, config.local_delete_mode)

        if config.bw_shred_mode != "off":
            collection_id = collection.get("id") if collection else None
            self._shred_bitwarden(collection_id, config.bw_shred_mode)

        employee_count = len(all_employees)
        self.audit.log_import_operation(employee_count, config.bw.collection_name)
        self.audit.log_security_event(
            "import_complete",
            f"Imported {employee_count} employee(s) into '{config.bw.collection_name}'",
        )
        progress("done", f"{employee_count} employee(s)")
        logging.info("--- Onboarding pipeline complete (%d employees). ---", employee_count)

    def _save_account_progress(
        self,
        employee_name: str,
        service: str,
        confirmed: bool,
    ) -> None:
        if self.retention_manager is None:
            return
        self.retention_manager.update_account_status(
            employee_name,
            service,
            "created" if confirmed else "pending",
        )

    def _bind_created_account(
        self,
        employee: Dict[str, str],
        role: str,
        service_name: str,
        password: str,
        uri: str,
    ) -> None:
        if self.profile_store is None or self.profile_sync is None:
            return
        profile = self.profile_store.find(
            display_name=employee.get("full_name", ""),
            username=employee.get("username", ""),
            email=employee.get("email", ""),
        )
        if profile is None or role in (profile.get("vault_refs") or {}):
            return
        self.profile_sync.create_login(
            profile["employee_id"],
            role,
            service_name,
            employee.get("email") or employee.get("username", ""),
            password,
            uri,
        )

    def resume_accounts(
        self,
        employee_name: str,
        password: str,
        config: OnboardingConfig,
        progress_callback: Optional[Any] = None,
        account_confirmation_callback: Optional[Any] = None,
    ) -> None:
        """Resume only missing partner accounts without re-importing Bitwarden items."""
        profile = (
            self.profile_store.find(display_name=employee_name)
            if self.profile_store is not None
            else None
        )
        if profile is not None:
            refs = profile.get("vault_refs") or {}
            profile["accounts"] = {
                "email": "created" if "email_login" in refs else "pending",
                "hyatt": "created" if "hyatt_login" in refs else "pending",
                "marriott": "created" if "marriott_login" in refs else "pending",
            }
        elif self.retention_manager is not None:
            profile = self.retention_manager.get_employee_profile(employee_name)
        if profile is None:
            raise RuntimeError(f"Employee profile '{employee_name}' was not found.")

        employee = {
            "full_name": employee_name,
            "first_name": profile.get("first_name", ""),
            "last_name": profile.get("last_name", ""),
            "username": profile.get("username", ""),
            "email": profile.get("email", ""),
        }
        personal_data = {
            **employee,
            "password": password,
        }
        account_name = employee.get("username") or employee_name
        accounts = profile.get("accounts") or {}

        def progress(service: str) -> None:
            detail = f"{service} — {account_name}"
            logging.info("accounts %s", detail)
            if progress_callback:
                progress_callback("accounts", detail)

        def confirm(service: str, result: Dict[str, Any]) -> bool:
            if account_confirmation_callback is None:
                return True
            return bool(account_confirmation_callback(service, employee, result))

        def reset_browser() -> None:
            callback = getattr(self.account_creator, "reset_browser_session", None)
            if callback:
                callback()

        try:
            email_created = accounts.get("email") == "created"
            if config.provision_outlook and not email_created:
                progress("Outlook")
                result = self.account_creator.create_outlook_account(
                    personal_data,
                    account_name,
                )
                email_created = confirm("Outlook", result)
                self._save_account_progress(employee_name, "email", email_created)
                reset_browser()

            if not email_created:
                raise RuntimeError(
                    "Confirm the Outlook account before creating Hyatt or Marriott."
                )

            if config.provision_hyatt and accounts.get("hyatt") != "created":
                progress("Hyatt")
                result = self.account_creator.create_hyatt_account(
                    personal_data,
                    account_name,
                )
                created = confirm("Hyatt", result)
                self._save_account_progress(employee_name, "hyatt", created)
                if created:
                    self._bind_created_account(
                        employee,
                        "hyatt_login",
                        "Hyatt",
                        password,
                        "https://www.hyatt.com/",
                    )
                reset_browser()

            if config.provision_marriott and accounts.get("marriott") != "created":
                progress("Marriott")
                result = self.account_creator.create_marriott_account(
                    personal_data,
                    account_name,
                )
                created = confirm("Marriott", result)
                self._save_account_progress(employee_name, "marriott", created)
                if created:
                    self._bind_created_account(
                        employee,
                        "marriott_login",
                        "Marriott",
                        password,
                        "https://www.marriott.com/",
                    )
                reset_browser()
        finally:
            close_browser = getattr(self.account_creator, "close_browser", None)
            if close_browser:
                close_browser()

    def _dispose_local(self, path: Path, mode: str) -> None:
        try:
            secure_delete_file(path, mode=mode)
            self.audit.log_deletion("local_file", path.name, method=mode)
            logging.info("Disposed local file %s (%s)", path.name, mode)
        except Exception as e:
            logging.error("Failed to dispose %s: %s", path, e)
            self.audit.log_security_event(
                "local_disposal_failed",
                f"File '{path.name}', mode '{mode}'",
            )
            raise

    def _shred_bitwarden(self, collection_id: Optional[str], mode: str) -> None:
        if mode == "all_collection" and not collection_id:
            raise RuntimeError(
                "Cannot shred an entire collection when importing to Personal Vault. "
                "Choose 'onboarding items only' or disable Bitwarden shredding."
            )
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
