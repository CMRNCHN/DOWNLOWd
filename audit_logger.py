"""
Audit Logger Module
Handles security audit logging for critical operations.
"""

import logging
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

AUDIT_LOG_FILE = Path.home() / ".downlowd_audit.log"


class AuditLogger:
    """Simple audit logger for security-critical operations."""
    
    def __init__(self):
        self._setup_audit_logger()
    
    def _setup_audit_logger(self):
        """Setup dedicated audit logger."""
        self.audit_logger = logging.getLogger("audit")
        self.audit_logger.setLevel(logging.INFO)
        self.audit_logger.propagate = False
        
        # File handler for audit log
        try:
            handler = logging.FileHandler(AUDIT_LOG_FILE, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s [AUDIT] %(message)s",
                "%Y-%m-%d %H:%M:%S"
            ))
            self.audit_logger.addHandler(handler)
            os.chmod(AUDIT_LOG_FILE, 0o600)
        except IOError as e:
            logging.error(f"Failed to setup audit log: {e}")

    def scrub_entries_containing(self, subject: str) -> bool:
        """Remove subject-bearing lines and safely reopen the audit file handler."""
        if not AUDIT_LOG_FILE.exists():
            return False
        original = AUDIT_LOG_FILE.read_text(encoding="utf-8", errors="ignore")
        lines = original.splitlines(keepends=True)
        kept = [line for line in lines if subject.casefold() not in line.casefold()]
        if len(kept) == len(lines):
            return False

        for handler in list(self.audit_logger.handlers):
            handler.flush()
            handler.close()
            self.audit_logger.removeHandler(handler)

        try:
            file_size = AUDIT_LOG_FILE.stat().st_size or 1
            with open(AUDIT_LOG_FILE, "r+b") as stream:
                for _ in range(3):
                    stream.seek(0)
                    stream.write(os.urandom(file_size))
                    stream.truncate(file_size)
                    stream.flush()
                    os.fsync(stream.fileno())
                retained = "".join(kept).encode("utf-8")
                stream.seek(0)
                stream.write(retained)
                stream.truncate(len(retained))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(AUDIT_LOG_FILE, 0o600)
            return True
        finally:
            self._setup_audit_logger()
    
    def log_authentication(self, success: bool, method: str = "password"):
        """Log authentication attempt."""
        status = "SUCCESS" if success else "FAILURE"
        self.audit_logger.info(f"AUTHENTICATION {status} - Method: {method}")
    
    def log_import_operation(self, employee_count: int, collection_name: str):
        """Log employee data import operation."""
        self.audit_logger.info(
            f"IMPORT - Employees: {employee_count}, Collection: {collection_name}"
        )
    
    def log_deletion(self, target_type: str, target_name: str, method: str = "manual"):
        """Log deletion operation."""
        self.audit_logger.info(
            f"DELETION - Type: {target_type}, Target: {target_name}, Method: {method}"
        )
    
    def log_transaction_added(self, employee: str, amount: float, merchant: str):
        """Log transaction addition."""
        self.audit_logger.info(
            f"TRANSACTION_ADDED - Employee: {employee}, Amount: ${amount:.2f}, Merchant: {merchant}"
        )
    
    def log_retention_action(self, employee: str, day: int, action: str):
        """Log data retention action."""
        self.audit_logger.info(
            f"RETENTION - Employee: {employee}, Day: {day}, Action: {action}"
        )
    
    def log_config_change(self, setting: str, old_value: str, new_value: str):
        """Log configuration change."""
        self.audit_logger.info(
            f"CONFIG_CHANGE - Setting: {setting}, Old: {old_value}, New: {new_value}"
        )
    
    def log_security_event(self, event_type: str, details: str):
        """Log general security event."""
        self.audit_logger.info(f"SECURITY_EVENT - Type: {event_type}, Details: {details}")
    
    def get_recent_audit_entries(self, limit: int = 100) -> list:
        """Retrieve recent audit log entries."""
        try:
            if not AUDIT_LOG_FILE.exists():
                return []
            
            entries = []
            with open(AUDIT_LOG_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip() and "[AUDIT]" in line:
                        entries.append(line.strip())
            
            return entries[-limit:]
        except IOError as e:
            logging.error(f"Failed to read audit log: {e}")
            return []
    
    def clear_audit_log(self) -> bool:
        """Clear the audit log (use with caution)."""
        try:
            if AUDIT_LOG_FILE.exists():
                AUDIT_LOG_FILE.unlink()
                logging.info("Audit log cleared")
                return True
            return False
        except IOError as e:
            logging.error(f"Failed to clear audit log: {e}")
            return False


# Global audit logger instance
_audit_logger = None

def get_audit_logger() -> AuditLogger:
    """Get the global audit logger instance."""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger
