"""
Transaction Database Module
Local SQLite storage for company card transactions (owner-only permissions; not encrypted at rest).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path.home() / ".downlowd_transactions.db"


class TransactionDatabase:
    """SQLite database for transaction storage with owner-only file permissions."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        try:
            return sqlite3.connect(self.db_path)
        except sqlite3.Error as e:
            logging.error("Database connection error: %s", e)
            raise

    def _ensure_permissions(self) -> None:
        """Restrict DB file to owner read/write only (0o600)."""
        try:
            if self.db_path.exists():
                os.chmod(self.db_path, 0o600)
        except OSError as e:
            logging.warning("Could not set DB permissions on %s: %s", self.db_path, e)

    def _init_db(self):
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    merchant TEXT NOT NULL,
                    employee_name TEXT NOT NULL,
                    card_number TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    employee_file_date TEXT
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_employee
                ON transactions(employee_name)
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_date
                ON transactions(date)
            """)

            conn.commit()
            conn.close()
            self._ensure_permissions()
            logging.info("Transaction database initialized")
        except sqlite3.Error as e:
            logging.error("Database initialization error: %s", e)
            raise

    def add_transaction(
        self,
        date: str,
        amount: float,
        merchant: str,
        employee_name: str,
        card_number: str,
        employee_file_date: Optional[str] = None,
    ) -> bool:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            created_at = datetime.now().isoformat()
            cursor.execute(
                """
                INSERT INTO transactions
                (date, amount, merchant, employee_name, card_number, created_at, employee_file_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (date, amount, merchant, employee_name, card_number, created_at, employee_file_date),
            )

            conn.commit()
            conn.close()
            self._ensure_permissions()
            logging.info("Added transaction: %s - $%.2f for %s", merchant, amount, employee_name)
            return True
        except sqlite3.Error as e:
            logging.error("Failed to add transaction: %s", e)
            return False

    def get_all_transactions(self) -> List[Dict[str, Any]]:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("""
                SELECT id, date, amount, merchant, employee_name, card_number, created_at, employee_file_date
                FROM transactions
                ORDER BY date DESC
            """)

            transactions = []
            for row in cursor.fetchall():
                transactions.append({
                    "id": row[0],
                    "date": row[1],
                    "amount": row[2],
                    "merchant": row[3],
                    "employee_name": row[4],
                    "card_number": row[5],
                    "created_at": row[6],
                    "employee_file_date": row[7],
                })

            conn.close()
            return transactions
        except sqlite3.Error as e:
            logging.error("Failed to retrieve transactions: %s", e)
            return []

    def get_transactions_by_employee(self, employee_name: str) -> List[Dict[str, Any]]:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT id, date, amount, merchant, employee_name, card_number, created_at, employee_file_date
                FROM transactions
                WHERE employee_name = ?
                ORDER BY date DESC
                """,
                (employee_name,),
            )

            transactions = []
            for row in cursor.fetchall():
                transactions.append({
                    "id": row[0],
                    "date": row[1],
                    "amount": row[2],
                    "merchant": row[3],
                    "employee_name": row[4],
                    "card_number": row[5],
                    "created_at": row[6],
                    "employee_file_date": row[7],
                })

            conn.close()
            return transactions
        except sqlite3.Error as e:
            logging.error("Failed to retrieve employee transactions: %s", e)
            return []

    def get_employee_names(self) -> List[str]:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("""
                SELECT DISTINCT employee_name
                FROM transactions
                ORDER BY employee_name
            """)

            employees = [row[0] for row in cursor.fetchall()]
            conn.close()
            return employees
        except sqlite3.Error as e:
            logging.error("Failed to retrieve employee names: %s", e)
            return []

    def delete_transaction(self, transaction_id: int) -> bool:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
            conn.commit()
            conn.close()
            logging.info("Deleted transaction ID: %s", transaction_id)
            return True
        except sqlite3.Error as e:
            logging.error("Failed to delete transaction: %s", e)
            return False

    def delete_employee_transactions(self, employee_name: str) -> bool:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("DELETE FROM transactions WHERE employee_name = ?", (employee_name,))
            conn.commit()
            conn.close()
            logging.info("Deleted all transactions for employee: %s", employee_name)
            return True
        except sqlite3.Error as e:
            logging.error("Failed to delete employee transactions: %s", e)
            return False

    def get_spending_summary(self) -> Dict[str, float]:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("""
                SELECT employee_name, SUM(amount) as total
                FROM transactions
                GROUP BY employee_name
                ORDER BY total DESC
            """)

            summary = {row[0]: row[1] for row in cursor.fetchall()}
            conn.close()
            return summary
        except sqlite3.Error as e:
            logging.error("Failed to get spending summary: %s", e)
            return {}

    def secure_delete(self) -> bool:
        try:
            if self.db_path.exists():
                file_size = self.db_path.stat().st_size or 1
                with open(self.db_path, "wb") as f:
                    f.write(os.urandom(file_size))
                self.db_path.unlink()
                logging.info("Transaction database securely deleted")
                return True
            return False
        except Exception as e:
            logging.error("Failed to securely delete database: %s", e)
            return False
