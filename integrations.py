"""Keychain-backed application auth and the Bitwarden CLI gateway."""

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

APP_PASSWORD_HASH_KEY = "app_password_hash"
APP_PASSWORD_SALT_KEY = "app_password_salt"
APP_SESSION_TOKEN_KEY = "app_session_token"
APP_SESSION_CREATED_KEY = "app_session_created_at"
PBKDF2_ITERATIONS = 200_000
APP_SESSION_TIMEOUT_SECONDS = 3600

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
            with open(file_path, "r+b") as f:
                for _ in range(3):
                    f.seek(0)
                    f.write(os.urandom(file_size))
                    f.truncate(file_size)
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
                if not value:
                    continue
                expected = str(value)
                existing = keyring.get_password(KEYRING_SERVICE, key)
                if existing is None:
                    keyring.set_password(KEYRING_SERVICE, key, expected)
                    existing = keyring.get_password(KEYRING_SERVICE, key)
                if existing != expected:
                    raise KeyringError(f"Keychain verification failed for '{key}'")
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
    """PBKDF2 app-password authentication with a one-hour Keychain session."""

    def __init__(
        self,
        credential_store: CredentialStore,
        session_timeout: int = APP_SESSION_TIMEOUT_SECONDS,
    ):
        self.credential_store = credential_store
        self.session_timeout = session_timeout

    @staticmethod
    def _hash_password(password: str, salt: bytes) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            PBKDF2_ITERATIONS,
        ).hex()

    def has_password(self) -> bool:
        return bool(
            self.credential_store.get(APP_PASSWORD_HASH_KEY)
            and self.credential_store.get(APP_PASSWORD_SALT_KEY)
        )

    def set_password(self, password: str) -> bool:
        if len(password) < 8:
            return False
        salt = secrets.token_bytes(16)
        expected_hash = self._hash_password(password, salt)
        self.credential_store.update(
            {
                APP_PASSWORD_SALT_KEY: salt.hex(),
                APP_PASSWORD_HASH_KEY: expected_hash,
            }
        )
        return bool(
            secrets.compare_digest(
                str(self.credential_store.get(APP_PASSWORD_HASH_KEY, "")),
                expected_hash,
            )
            and secrets.compare_digest(
                str(self.credential_store.get(APP_PASSWORD_SALT_KEY, "")),
                salt.hex(),
            )
        )

    def verify_password(self, password: str) -> bool:
        stored_hash = self.credential_store.get(APP_PASSWORD_HASH_KEY)
        salt_hex = self.credential_store.get(APP_PASSWORD_SALT_KEY)
        if not stored_hash or not salt_hex:
            return False
        try:
            salt = bytes.fromhex(str(salt_hex))
        except ValueError:
            return False
        candidate = self._hash_password(password, salt)
        return secrets.compare_digest(candidate, str(stored_hash))

    def create_session(self, password: str) -> bool:
        if not self.verify_password(password):
            return False
        token = secrets.token_urlsafe(32)
        created_at = str(time.time())
        self.credential_store.update(
            {
                APP_SESSION_TOKEN_KEY: token,
                APP_SESSION_CREATED_KEY: created_at,
            }
        )
        return bool(
            secrets.compare_digest(
                str(self.credential_store.get(APP_SESSION_TOKEN_KEY, "")),
                token,
            )
            and secrets.compare_digest(
                str(self.credential_store.get(APP_SESSION_CREATED_KEY, "")),
                created_at,
            )
        )

    def is_authenticated(self) -> bool:
        if not self.has_password():
            self.invalidate_session()
            return False
        token = self.credential_store.get(APP_SESSION_TOKEN_KEY)
        created_at = self.credential_store.get(APP_SESSION_CREATED_KEY)
        if not token or not created_at:
            return False
        try:
            active = 0 <= time.time() - float(created_at) < self.session_timeout
        except (TypeError, ValueError):
            active = False
        if not active:
            self.invalidate_session()
        return active

    def invalidate_session(self) -> None:
        self.credential_store.update(
            {
                APP_SESSION_TOKEN_KEY: "",
                APP_SESSION_CREATED_KEY: "",
            }
        )


class BitwardenService:
    """Gateway for Bitwarden CLI interactions with session key propagation."""

    def __init__(self):
        self.session_key: Optional[str] = None

    def _env_with_session(self) -> Dict[str, str]:
        env = os.environ.copy()
        if self.session_key:
            env["BW_SESSION"] = self.session_key
        else:
            env.pop("BW_SESSION", None)
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
        except OSError as e:
            self.clear_session()
            logging.error("Could not run Bitwarden CLI: %s", e)
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
        except OSError as e:
            self.clear_session()
            logging.error("Could not run Bitwarden CLI: %s", e)
            return {
                "success": False,
                "two_factor_required": False,
                "error": "Bitwarden CLI is unavailable.",
            }

    def resolve_collection(self, collection_name: str) -> Optional[Dict[str, Any]]:
        """Resolve an exact organization collection; return None for personal vault.

        Bitwarden collections only exist inside organizations. Personal accounts
        import directly into the personal vault and do not have a collection ID.
        """
        name = (collection_name or "").strip()
        if not name or name.lower() in {"personal", "personal vault", "my vault"}:
            logging.info("Using personal Bitwarden vault (no organization collection).")
            return None

        logging.info("Looking for Bitwarden organization collection '%s'...", name)
        try:
            proc = self._run_bw(
                ["list", "collections", "--search", name, "--raw"],
                capture_output=True,
                text=True,
                check=True,
            )
            collections = json.loads(proc.stdout or "[]")
            exact_matches = [
                collection
                for collection in collections
                if str(collection.get("name", "")).casefold() == name.casefold()
            ]
            if len(exact_matches) > 1:
                organization_ids = sorted(
                    {
                        str(collection.get("organizationId") or "unknown")
                        for collection in exact_matches
                    }
                )
                raise RuntimeError(
                    f"Bitwarden collection '{name}' is ambiguous across organizations "
                    f"({', '.join(organization_ids)}). Use a unique collection name."
                )
            if exact_matches:
                exact = exact_matches[0]
                logging.info(
                    "Using organization collection '%s' (%s).",
                    exact.get("name"),
                    exact.get("id"),
                )
                return exact

            raise RuntimeError(
                f"Bitwarden collection '{name}' does not exist. "
                "Select 'Personal Vault' explicitly to import outside an organization."
            )
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            raise RuntimeError(
                f"Could not resolve Bitwarden collection '{name}'."
            ) from e

    def import_json(
        self,
        bw_json: str,
        collection: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Import Bitwarden JSON into personal vault or an organization collection."""
        secure_temp_dir = Path.home() / ".downlowd_temp"
        secure_temp_dir.mkdir(exist_ok=True, mode=0o700)
        collection_id = str(collection.get("id")) if collection else "personal"
        organization_id = (
            str(collection.get("organizationId"))
            if collection and collection.get("organizationId")
            else None
        )
        temp_file = secure_temp_dir / f"temp_import_{collection_id}.json"

        try:
            import_payload = bw_json
            if collection:
                # Bitwarden JSON supports organizationId + collectionIds on items.
                # Populate them so organization imports land in the chosen collection.
                parsed = json.loads(bw_json)
                for item in parsed.get("items", []):
                    item["organizationId"] = organization_id
                    item["collectionIds"] = [collection_id]
                import_payload = json.dumps(parsed, indent=2)

            temp_file.write_text(import_payload, encoding="utf-8")
            temp_file.chmod(0o600)
            target_name = collection.get("name") if collection else "Personal Vault"
            logging.info("Importing data into Bitwarden target %s...", target_name)
            args = ["import"]
            if organization_id:
                args.extend(["--organizationid", organization_id])
            args.extend(["bitwardenjson", str(temp_file)])
            try:
                self._run_bw(
                    args,
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                detail = _clean_cli_text(e.stderr or e.stdout or "")
                raise RuntimeError(
                    f"Bitwarden import failed for {target_name}: "
                    f"{detail or 'unknown CLI error'}"
                ) from e
        finally:
            if temp_file.exists():
                self._secure_delete_file(temp_file)

    def _secure_delete_file(self, file_path: Path) -> None:
        try:
            file_size = file_path.stat().st_size or 1
            with open(file_path, "r+b") as f:
                for _ in range(3):
                    f.seek(0)
                    f.write(os.urandom(file_size))
                    f.truncate(file_size)
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

    def list_items(self, collection_id: Optional[str] = None) -> List[Dict[str, Any]]:
        args = ["list", "items"]
        if collection_id:
            args.extend(["--collectionid", collection_id])
        args.append("--raw")
        proc = self._run_bw(
            args,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def sync(self) -> None:
        self._run_bw(
            ["sync"],
            capture_output=True,
            text=True,
            check=True,
        )

    def get_item(self, item_id: str) -> Dict[str, Any]:
        proc = self._run_bw(
            ["get", "item", item_id, "--raw"],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def _encode_payload(self, payload: Dict[str, Any]) -> str:
        proc = self._run_bw(
            ["encode"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=True,
        )
        encoded = proc.stdout.strip()
        if not encoded:
            raise RuntimeError("Bitwarden returned an empty encoded payload.")
        return encoded

    def create_item(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        encoded = self._encode_payload(payload)
        proc = self._run_bw(
            ["create", "item", encoded],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def edit_item(self, item_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        encoded = self._encode_payload(payload)
        proc = self._run_bw(
            ["edit", "item", item_id, encoded],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def trash_item(self, item_id: str) -> None:
        self._run_bw(
            ["delete", "item", item_id],
            capture_output=True,
            text=True,
            check=True,
        )

    def restore_item(self, item_id: str) -> Dict[str, Any]:
        proc = self._run_bw(
            ["restore", "item", item_id],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def delete_item_permanently(self, item_id: str) -> None:
        self._run_bw(
            ["delete", "item", item_id, "--permanent"],
            capture_output=True,
            text=True,
            check=True,
        )

    def delete_item(self, item_id: str) -> None:
        """Backward-compatible trash operation."""
        self.trash_item(item_id)
