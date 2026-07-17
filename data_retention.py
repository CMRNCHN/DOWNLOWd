"""
Data Retention Module
Handles automated data lifecycle management with 5/10/15/20 day schedule.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from audit_logger import AUDIT_LOG_FILE, get_audit_logger

RETENTION_DATA_FILE = Path.home() / ".downlowd_retention.json"
LOGS_DIR = Path.cwd() / "logs"


class DataRetentionManager:
    """Manages automated data lifecycle for employee and transaction data."""

    def __init__(self, transaction_db, prompt_callback: Optional[Callable[[Dict], None]] = None):
        self.transaction_db = transaction_db
        self.retention_data = self._load_retention_data()
        self._scheduler_thread = None
        self._running = False
        # Called from scheduler thread for day 5/10 actions that need UI prompts
        self.prompt_callback = prompt_callback
        self.audit = get_audit_logger()

    def _load_retention_data(self) -> Dict:
        if RETENTION_DATA_FILE.exists():
            try:
                return json.loads(RETENTION_DATA_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, IOError) as e:
                logging.warning("Could not load retention data: %s", e)
                return {"employees": {}, "last_check": None}
        return {"employees": {}, "last_check": None}

    def _save_retention_data(self):
        try:
            RETENTION_DATA_FILE.write_text(
                json.dumps(self.retention_data, indent=2),
                encoding="utf-8",
            )
            if RETENTION_DATA_FILE.exists():
                os.chmod(RETENTION_DATA_FILE, 0o600)
        except IOError as e:
            logging.error("Failed to save retention data: %s", e)

    def register_employee(
        self,
        employee_name: str,
        file_date: str,
        associated_logs: Optional[List[str]] = None,
    ):
        """Register employee and optionally bind exact log file paths for day-20 shredding."""
        existing = self.retention_data["employees"].get(employee_name, {})
        merged_logs: List[str] = list(existing.get("associated_logs") or [])
        for path in associated_logs or []:
            p = str(Path(path).resolve())
            if p not in merged_logs:
                merged_logs.append(p)

        # Dedicated per-employee log dir (created eagerly so day-20 has a deterministic target)
        emp_dir = LOGS_DIR / "employees" / self._employee_slug(employee_name)
        emp_dir.mkdir(parents=True, exist_ok=True)

        self.retention_data["employees"][employee_name] = {
            "file_date": file_date,
            "registered_date": existing.get("registered_date") or datetime.now().isoformat(),
            "status": existing.get("status") or "active",
            "day5_audit": existing.get("day5_audit", False),
            "day10_audit": existing.get("day10_audit", False),
            "day15_shredded": existing.get("day15_shredded", False),
            "day20_logs_shredded": existing.get("day20_logs_shredded", False),
            "associated_logs": merged_logs,
            "employee_log_dir": str(emp_dir.resolve()),
        }
        self._save_retention_data()
        logging.info("Registered employee for retention: %s", employee_name)
        self.audit.log_retention_action(employee_name, 0, "registered")

    @staticmethod
    def _employee_slug(employee_name: str) -> str:
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in employee_name.strip())
        return slug.strip("_")[:80] or "unknown"

    def check_retention_schedule(self) -> List[Dict]:
        """Return all due retention actions. Independent ifs so overdue milestones are not blocked."""
        actions_needed = []
        now = datetime.now()

        for employee_name, data in self.retention_data["employees"].items():
            if data["status"] == "shredded" and data.get("day20_logs_shredded"):
                continue

            try:
                registered_date = datetime.fromisoformat(data["registered_date"])
                days_elapsed = (now - registered_date).days

                # Day 5: Audit prompt - employee active status check
                if days_elapsed >= 5 and not data["day5_audit"] and data["status"] != "shredded":
                    actions_needed.append({
                        "employee": employee_name,
                        "day": 5,
                        "action": "audit_employee_status",
                        "message": f"Day 5: Is {employee_name} still an active employee?",
                    })

                # Day 10: Audit prompt - file shredding confirmation
                if days_elapsed >= 10 and not data["day10_audit"] and data["status"] != "shredded":
                    actions_needed.append({
                        "employee": employee_name,
                        "day": 10,
                        "action": "confirm_shredding",
                        "message": f"Day 10: Should files for {employee_name} be shredded?",
                    })

                # Day 15: Automatic shredding of employee files
                if days_elapsed >= 15 and not data["day15_shredded"]:
                    actions_needed.append({
                        "employee": employee_name,
                        "day": 15,
                        "action": "auto_shred",
                        "message": f"Day 15: Auto-shredding data for {employee_name}",
                    })

                # Day 20: Automatic shredding of logs
                if days_elapsed >= 20 and not data["day20_logs_shredded"]:
                    actions_needed.append({
                        "employee": employee_name,
                        "day": 20,
                        "action": "shred_logs",
                        "message": f"Day 20: Shredding logs for {employee_name}",
                    })

            except (ValueError, KeyError) as e:
                logging.error("Error processing retention data for %s: %s", employee_name, e)

        return actions_needed

    def process_audit_response(self, employee_name: str, day: int, response: str):
        if employee_name not in self.retention_data["employees"]:
            logging.error("Employee not found in retention data: %s", employee_name)
            return False

        employee_data = self.retention_data["employees"][employee_name]

        if day == 5:
            if response.lower() in ["yes", "active", "true"]:
                employee_data["status"] = "active"
                employee_data["day5_audit"] = True
                logging.info("Day 5 audit: %s marked as active", employee_name)
                self.audit.log_retention_action(employee_name, 5, "active")
            else:
                employee_data["status"] = "inactive"
                employee_data["day5_audit"] = True
                logging.info("Day 5 audit: %s marked as inactive", employee_name)
                self.audit.log_retention_action(employee_name, 5, "inactive")

        elif day == 10:
            if response.lower() in ["yes", "shred", "true"]:
                employee_data["day10_audit"] = True
                logging.info("Day 10 audit: %s approved for shredding", employee_name)
                self.audit.log_retention_action(employee_name, 10, "approved_shred")
            else:
                employee_data["status"] = "keep"
                employee_data["day10_audit"] = True
                logging.info("Day 10 audit: %s files to be kept", employee_name)
                self.audit.log_retention_action(employee_name, 10, "keep")

        self._save_retention_data()
        return True

    def execute_auto_shred(self, employee_name: str) -> bool:
        if employee_name not in self.retention_data["employees"]:
            return False

        try:
            if self.transaction_db.delete_employee_transactions(employee_name):
                logging.info("Shredded transactions for %s", employee_name)

            self.retention_data["employees"][employee_name]["day15_shredded"] = True
            self.retention_data["employees"][employee_name]["status"] = "shredded"
            self._save_retention_data()

            logging.info("Day 15: Auto-shredding completed for %s", employee_name)
            self.audit.log_retention_action(employee_name, 15, "auto_shred")
            self.audit.log_deletion("employee_transactions", employee_name, method="auto_shred")
            return True

        except Exception as e:
            logging.error("Failed to auto-shred data for %s: %s", employee_name, e)
            return False

    def _secure_delete_file(self, file_path: Path) -> None:
        if not file_path.exists():
            return
        file_size = file_path.stat().st_size or 1
        with open(file_path, "wb") as f:
            for _ in range(3):
                f.write(os.urandom(file_size))
                f.flush()
                os.fsync(f.fileno())
        file_path.unlink()

    def execute_log_shredding(self, employee_name: str) -> bool:
        """Secure-delete tracked employee logs; scrub shared logs by line (no whole-file heuristic)."""
        if employee_name not in self.retention_data["employees"]:
            return False

        data = self.retention_data["employees"][employee_name]
        deleted_any = False
        errors: List[str] = []

        try:
            # 1) Exact paths recorded at registration / processing time
            for path_str in list(data.get("associated_logs") or []):
                path = Path(path_str)
                try:
                    if path.exists() and path.is_file():
                        # Shared session logs: scrub lines instead of deleting the whole file
                        if path.parent.resolve() == LOGS_DIR.resolve() and path.name.startswith("onboarding_"):
                            if self._scrub_employee_lines(path, employee_name):
                                deleted_any = True
                        else:
                            self._secure_delete_file(path)
                            deleted_any = True
                            logging.info("Shredded tracked log %s for %s", path.name, employee_name)
                except Exception as e:
                    errors.append(f"{path}: {e}")
                    logging.error("Failed to shred tracked log %s: %s", path, e)

            # 2) Dedicated per-employee directory
            emp_dir = Path(data.get("employee_log_dir") or (LOGS_DIR / "employees" / self._employee_slug(employee_name)))
            if emp_dir.exists() and emp_dir.is_dir():
                for log_file in emp_dir.rglob("*"):
                    if log_file.is_file():
                        try:
                            self._secure_delete_file(log_file)
                            deleted_any = True
                        except Exception as e:
                            errors.append(f"{log_file}: {e}")
                try:
                    # Remove empty dirs bottom-up
                    for child in sorted(emp_dir.rglob("*"), reverse=True):
                        if child.is_dir():
                            child.rmdir()
                    emp_dir.rmdir()
                except OSError:
                    pass

            # 3) Scrub audit log lines mentioning this employee (never delete whole audit file)
            if AUDIT_LOG_FILE.exists():
                try:
                    if self._scrub_employee_lines(AUDIT_LOG_FILE, employee_name):
                        deleted_any = True
                except Exception as e:
                    errors.append(f"audit_log: {e}")
                    logging.error("Failed to scrub audit log for %s: %s", employee_name, e)

            if errors:
                logging.error(
                    "Log shredding for %s incomplete (%d errors); not marking complete",
                    employee_name,
                    len(errors),
                )
                self.audit.log_retention_action(employee_name, 20, "shred_logs_failed")
                return False

            self.retention_data["employees"][employee_name]["day20_logs_shredded"] = True
            self._save_retention_data()
            logging.info(
                "Day 20: Log shredding completed for %s (touched=%s)",
                employee_name,
                deleted_any,
            )
            self.audit.log_retention_action(employee_name, 20, "shred_logs")
            return True

        except Exception as e:
            logging.error("Failed to shred logs for %s: %s", employee_name, e)
            return False

    def _scrub_employee_lines(self, file_path: Path, employee_name: str) -> bool:
        """Remove lines containing employee_name from a shared log; overwrite in place."""
        original = file_path.read_text(encoding="utf-8", errors="ignore")
        lines = original.splitlines(keepends=True)
        needle = employee_name.lower()
        kept = [ln for ln in lines if needle not in ln.lower()]
        if len(kept) == len(lines):
            return False
        # Secure-ish rewrite: overwrite with remaining content then truncate
        new_text = "".join(kept)
        with open(file_path, "r+b") as f:
            data = new_text.encode("utf-8")
            f.write(data)
            f.truncate(len(data))
            f.flush()
            os.fsync(f.fileno())
        logging.info("Scrubbed %d lines for %s from %s", len(lines) - len(kept), employee_name, file_path.name)
        return True

    def process_actions(self, actions: Optional[List[Dict]] = None) -> None:
        """Execute or queue retention actions. Auto actions run immediately; prompts go to callback."""
        if actions is None:
            actions = self.check_retention_schedule()

        for action in actions:
            kind = action.get("action")
            employee = action["employee"]

            if kind == "auto_shred":
                self.execute_auto_shred(employee)
            elif kind == "shred_logs":
                self.execute_log_shredding(employee)
            elif kind in ("audit_employee_status", "confirm_shredding"):
                if self.prompt_callback:
                    self.prompt_callback(action)
                else:
                    logging.info("Retention prompt (no UI callback): %s", action["message"])

    def start_scheduler(self, check_interval_hours: int = 24):
        if self._running:
            logging.warning("Retention scheduler already running")
            return

        self._running = True
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            args=(check_interval_hours * 3600,),
            daemon=True,
        )
        self._scheduler_thread.start()
        logging.info("Retention scheduler started (checks every %s hours)", check_interval_hours)
        # Run an immediate check shortly after start
        threading.Thread(target=self._initial_check, daemon=True).start()

    def _initial_check(self):
        time.sleep(2)
        try:
            self.process_actions()
        except Exception as e:
            logging.error("Error in initial retention check: %s", e)

    def _scheduler_loop(self, interval_seconds: int):
        while self._running:
            try:
                actions = self.check_retention_schedule()
                if actions:
                    logging.info("Retention check found %d actions needed", len(actions))
                    self.process_actions(actions)

                self.retention_data["last_check"] = datetime.now().isoformat()
                self._save_retention_data()

            except Exception as e:
                logging.error("Error in retention scheduler: %s", e)

            time.sleep(interval_seconds)

    def stop_scheduler(self):
        self._running = False
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)
        logging.info("Retention scheduler stopped")

    def get_employee_status(self, employee_name: str) -> Optional[Dict]:
        return self.retention_data["employees"].get(employee_name)

    def get_all_employees_status(self) -> Dict:
        return self.retention_data["employees"]
