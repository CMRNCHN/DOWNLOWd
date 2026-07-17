"""
Credential storage (Keychain), app session auth (PBKDF2), and Bitwarden CLI gateway.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import keyring
from keyring.errors import KeyringError

CREDENTIALS_FILE = Path.home() / ".onboarding_credentials.json"
KEYRING_SERVICE = "DOWNLOWD"

# App auth Keychain keys
APP_PASSWORD_HASH_KEY = "app_password_hash"
APP_PASSWORD_SALT_KEY = "app_password_salt"
APP_SESSION_TOKEN_KEY = "app_session_token"
APP_SESSION_CREATED_KEY = "app_session_created_at"

PBKDF2_ITERATIONS = 200_000

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean_cli_text(text: str) -> str:
    """Strip ANSI / cursor noise from Bitwarden CLI stderr for UI display."""
    if not text:
        return ""
    cleaned = _ANSI_RE.sub("", text)
    cleaned = cleaned.replace("\r", "\n")
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("? Master password")]
    return "\n".join(lines).strip()


class CredentialStore:
    def __init__(self):
        self._migrate_to_keychain()

    def _secure_delete_file(self, file_path: Path) -> None:
        """Overwrite then unlink a file; raise if unlink fails after overwrite."""
        try:
            if not file_path.exists():
                return
            file_size = file_path.stat().st_size or 1
            with open(file_path, "wb") as f:
                for _ in range(3):
                    f.write(os.urandom(file_size))
                    f.flush()
                    os.fsync(f.fileno())
            file_path.unlink()
            logging.debug("Securely deleted file: %s", file_path)
        except Exception as e:
            logging.error("Failed to securely delete file %s: %s", file_path, e)
            raise

    def _migrate_to_keychain(self):
        """Migrate existing plaintext credentials to Keychain, then shred the source file."""
        if not CREDENTIALS_FILE.exists():
            return
        try:
            old_creds = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
            for key, value in old_creds.items():
                if value and not keyring.get_password(KEYRING_SERVICE, key):
                    keyring.set_password(KEYRING_SERVICE, key, str(value))
                    logging.info("Migrated credential '%s' to Keychain", key)
            # Shred original; do not leave a plaintext .backup
            self._secure_delete_file(CREDENTIALS_FILE)
            logging.info("Credentials migrated to Keychain; source file securely deleted")
        except (json.JSONDecodeError, KeyringError, OSError) as e:
            # Leave original intact on failure
            logging.error("Could not migrate credentials (original file left intact): %s", e)


    def get(self, key: str, default: Any = None) -> Any:
        try:
            value = keyring.get_password(KEYRING_SERVICE, key)
            return value if value is not None else default
        except KeyringError as e:
            logging.error("Keychain error for '%s': %s", key, e)
            return default

    def get_all(self) -> Dict[str, str]:
        return {}

    def update(self, new_creds: Dict[str, str]):
        for key, value in new_creds.items():
            try:
                if value:
                    keyring.set_password(KEYRING_SERVICE, key, value)
                else:
                    try:
                        keyring.delete_password(KEYRING_SERVICE, key)
                    except keyring.errors.PasswordDeleteError:
                        pass
            except KeyringError as e:
                logging.error("Failed to store credential '%s': %s", key, e)


class SessionManager:
    """PBKDF2 app-password auth with a random Keychain session token (1-hour timeout)."""

    def __init__(self, credential_store: CredentialStore):
        self.credential_store = credential_store
        self._session_timeout = 3600  # 1 hour

    def has_password(self) -> bool:
        return bool(self.credential_store.get(APP_PASSWORD_HASH_KEY))

    def _hash_password(self, password: str, salt: bytes) -> str:
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            PBKDF2_ITERATIONS,
        )
        return digest.hex()

    def set_password(self, password: str) -> bool:
        """First-run: store PBKDF2 hash + salt in Keychain."""
        if not password:
            return False
        try:
            salt = secrets.token_bytes(16)
            password_hash = self._hash_password(password, salt)
            self.credential_store.update({
                APP_PASSWORD_SALT_KEY: salt.hex(),
                APP_PASSWORD_HASH_KEY: password_hash,
            })
            return True
        except KeyringError:
            return False

    def verify_password(self, password: str) -> bool:
        stored_hash = self.credential_store.get(APP_PASSWORD_HASH_KEY)
        salt_hex = self.credential_store.get(APP_PASSWORD_SALT_KEY)
        if not stored_hash or not salt_hex:
            return False
        try:
            salt = bytes.fromhex(salt_hex)
            candidate = self._hash_password(password, salt)
            return secrets.compare_digest(candidate, stored_hash)
        except (ValueError, TypeError):
            return False

    def is_authenticated(self) -> bool:
        try:
            token = self.credential_store.get(APP_SESSION_TOKEN_KEY)
            created_raw = self.credential_store.get(APP_SESSION_CREATED_KEY)
            if not token or not created_raw:
                return False
            created = float(created_raw)
            return (time.time() - created) < self._session_timeout
        except (KeyringError, ValueError, TypeError):
            return False

    def create_session(self, password: str) -> bool:
        """Verify password, then mint a random session token."""
        if not self.verify_password(password):
            return False
        try:
            token = secrets.token_urlsafe(32)
            self.credential_store.update({
                APP_SESSION_TOKEN_KEY: token,
                APP_SESSION_CREATED_KEY: str(time.time()),
            })
            return True
        except KeyringError:
            return False

    def invalidate_session(self) -> None:
        try:
            self.credential_store.update({
                APP_SESSION_TOKEN_KEY: "",
                APP_SESSION_CREATED_KEY: "",
            })
        except KeyringError:
            pass


class BitwardenService:
    """Gateway for Bitwarden CLI interactions with session key propagation."""

    def __init__(self):
        self.session_key: Optional[str] = None

    def _env_with_session(self) -> Dict[str, str]:
        env = os.environ.copy()
        if self.session_key:
            env["BW_SESSION"] = self.session_key
        return env

    def _run_bw(self, args: List[str], **kwargs) -> subprocess.CompletedProcess:
        """Run a bw CLI command with BW_SESSION set when available."""
        return subprocess.run(
            ["bw", *args],
            env=self._env_with_session(),
            **kwargs,
        )

    def clear_session(self) -> None:
        self.session_key = None

    def get_status(self) -> str:
        """Return 'unlocked', 'locked', or 'unauthenticated'."""
        logging.info("Checking Bitwarden CLI status...")
        try:
            proc = self._run_bw(
                ["status", "--raw"],
                capture_output=True,
                text=True,
                check=True,
            )
            status = json.loads(proc.stdout).get("status")
            return status
        except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as e:
            logging.error("Failed to check Bitwarden status. Is 'bw' CLI installed? Details: %s", e)
            raise

    def unlock(self, password: str) -> bool:
        """Unlock vault and store session key for subsequent CLI calls."""
        logging.info("Attempting to unlock Bitwarden vault...")
        try:
            env = self._env_with_session()
            env["BW_PASSWORD"] = password
            proc = subprocess.run(
                ["bw", "unlock", "--passwordenv", "BW_PASSWORD", "--raw"],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=True,
            )
            self.session_key = proc.stdout.strip() or None
            if not self.session_key:
                logging.error("Unlock succeeded but no session key returned.")
                return False
            logging.info("Bitwarden vault unlocked successfully.")
            return True
        except subprocess.CalledProcessError as e:
            self.clear_session()
            logging.error(
                "Failed to unlock Bitwarden. The password may be incorrect. Details: %s",
                (e.stderr or e.stdout or "").strip(),
            )
            return False

    def login(self, email: str, password: str, two_factor_code: Optional[str] = None) -> Dict[str, Any]:
        """Log into Bitwarden CLI and store session key (non-interactive)."""
        logging.info("Attempting to log into Bitwarden CLI for %s...", email)
        command = ["bw", "login", email, "--passwordenv", "BW_PASSWORD", "--raw"]
        if two_factor_code:
            command.extend(["--method", "0", "--code", str(two_factor_code)])

        try:
            env = self._env_with_session()
            env["BW_PASSWORD"] = password
            proc = subprocess.run(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=True,
            )
            self.session_key = proc.stdout.strip() or None
            if not self.session_key:
                logging.error("Login succeeded but no session key returned.")
                return {"success": False, "two_factor_required": False, "error": "No session key"}
            logging.info("Bitwarden login successful.")
            return {"success": True, "two_factor_required": False}
        except subprocess.CalledProcessError as e:
            self.clear_session()
            stderr = (e.stderr or e.stdout or "")
            stderr_l = stderr.lower()
            if "two-step login is required" in stderr_l or "two-step" in stderr_l:
                return {"success": False, "two_factor_required": True}
            logging.error("Failed to log into Bitwarden. Details: %s", stderr.strip())
            return {
                "success": False,
                "two_factor_required": False,
                "error": _clean_cli_text(stderr) or "Login failed.",
            }

    def get_collection_id(self, collection_name: str) -> str:
        logging.info("Fetching Collection ID for '%s'...", collection_name)
        try:
            proc = self._run_bw(
                ["get", "collection", collection_name],
                capture_output=True,
                text=True,
                check=True,
            )
            cid = json.loads(proc.stdout).get("id")
            if not cid or cid == "null":
                raise RuntimeError(f"Could not find Bitwarden Collection named '{collection_name}'.")
            logging.info("Found Collection ID: %s", cid)
            return cid
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            logging.error("Could not find Bitwarden Collection named '%s'. Details: %s", collection_name, e)
            raise

    def import_json(self, bw_json: str, collection_id: str) -> None:
        """Import Bitwarden JSON via a restricted temp file."""
        secure_temp_dir = Path.home() / ".downlowd_temp"
        secure_temp_dir.mkdir(exist_ok=True, mode=0o700)
        temp_file = secure_temp_dir / f"temp_import_{collection_id}.json"

        try:
            temp_file.write_text(bw_json, encoding="utf-8")
            temp_file.chmod(0o600)
            logging.info("Importing data into Bitwarden collection %s...", collection_id)
            self._run_bw(
                ["import", "bitwardenjson", str(temp_file), "--collectionid", collection_id],
                check=True,
            )
        finally:
            if temp_file.exists():
                self._secure_delete_file(temp_file)

    def _secure_delete_file(self, file_path: Path) -> None:
        try:
            file_size = file_path.stat().st_size or 1
            with open(file_path, "wb") as f:
                for _ in range(3):
                    f.write(os.urandom(file_size))
                    f.flush()
                    os.fsync(f.fileno())
            file_path.unlink()
            logging.debug("Securely deleted temporary file: %s", file_path)
        except Exception as e:
            logging.error("Failed to securely delete file %s: %s", file_path, e)
            try:
                file_path.unlink(missing_ok=True)
            except Exception:
                pass

    def list_items(self, collection_id: str) -> List[Dict[str, Any]]:
        proc = self._run_bw(
            ["list", "items", "--collectionid", collection_id, "--raw"],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def delete_item(self, item_id: str) -> None:
        self._run_bw(["delete", "item", item_id], check=True)
