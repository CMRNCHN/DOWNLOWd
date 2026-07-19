"""Bitwarden-backed employee profile metadata and synchronization."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PROFILE_DATA_FILE = Path.home() / ".downlowd_profiles.json"
SCHEMA_VERSION = 2
EMPLOYEE_ID_FIELD = "DOWNLOWD Employee ID"
RECORD_ROLE_FIELD = "DOWNLOWD Record Role"
RECORD_ROLES = (
    "identity",
    "email_login",
    "hyatt_login",
    "marriott_login",
    "work_card",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EmployeeProfileStore:
    """Stores only non-secret profile metadata and durable Bitwarden item IDs."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else PROFILE_DATA_FILE
        self._lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "profiles": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": SCHEMA_VERSION, "profiles": {}}
        data.setdefault("profiles", {})
        data["schema_version"] = SCHEMA_VERSION
        return data

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(".tmp")
            fd = os.open(
                temp_path,
                os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
                0o600,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(self.data, stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, self.path)
                os.chmod(self.path, 0o600)
            finally:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _new_profile(
        display_name: str,
        first_name: str = "",
        last_name: str = "",
        username: str = "",
        email: str = "",
        employee_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        timestamp = _now()
        return {
            "employee_id": employee_id or str(uuid.uuid4()),
            "display_name": display_name,
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
            "email": email,
            "status": "active",
            "accounts": {
                "email": "pending",
                "hyatt": "pending",
                "marriott": "pending",
            },
            "vault_refs": {},
            "deletion": None,
            "sync_error": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def list_profiles(self, include_purged: bool = False) -> List[Dict[str, Any]]:
        profiles = list(self.data["profiles"].values())
        if not include_purged:
            profiles = [
                profile
                for profile in profiles
                if (profile.get("deletion") or {}).get("status") != "purged"
            ]
        return sorted(
            (dict(profile) for profile in profiles),
            key=lambda profile: profile.get("display_name", "").casefold(),
        )

    def get(self, employee_id: str) -> Optional[Dict[str, Any]]:
        profile = self.data["profiles"].get(employee_id)
        return dict(profile) if profile else None

    def find(
        self,
        *,
        display_name: str = "",
        username: str = "",
        email: str = "",
    ) -> Optional[Dict[str, Any]]:
        candidates = self.list_profiles(include_purged=True)
        for profile in candidates:
            if username and profile.get("username", "").casefold() == username.casefold():
                return profile
            if email and profile.get("email", "").casefold() == email.casefold():
                return profile
        matches = [
            profile
            for profile in candidates
            if display_name
            and profile.get("display_name", "").casefold() == display_name.casefold()
        ]
        return matches[0] if len(matches) == 1 else None

    def upsert(
        self,
        *,
        display_name: str,
        first_name: str = "",
        last_name: str = "",
        username: str = "",
        email: str = "",
        employee_id: Optional[str] = None,
        accounts: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            profile = (
                self.data["profiles"].get(employee_id)
                if employee_id
                else self.find(
                    display_name=display_name,
                    username=username,
                    email=email,
                )
            )
            if profile is None:
                profile = self._new_profile(
                    display_name,
                    first_name,
                    last_name,
                    username,
                    email,
                    employee_id,
                )
            for key, value in {
                "display_name": display_name,
                "first_name": first_name,
                "last_name": last_name,
                "username": username,
                "email": email,
            }.items():
                if value:
                    profile[key] = value
            if accounts:
                profile.setdefault("accounts", {}).update(accounts)
            profile["updated_at"] = _now()
            self.data["profiles"][profile["employee_id"]] = profile
            self._save()
            return dict(profile)

    def bind_vault_ref(
        self,
        employee_id: str,
        role: str,
        item: Dict[str, Any],
    ) -> Dict[str, Any]:
        if role not in RECORD_ROLES:
            raise ValueError(f"Unsupported vault role: {role}")
        with self._lock:
            profile = self.data["profiles"][employee_id]
            profile.setdefault("vault_refs", {})[role] = {
                "role": role,
                "item_id": str(item["id"]),
                "item_type": item.get("type"),
                "organization_id": item.get("organizationId"),
                "collection_ids": list(item.get("collectionIds") or []),
                "revision_date": item.get("revisionDate"),
                "trashed": bool(item.get("deletedDate")),
            }
            account_role = {
                "email_login": "email",
                "hyatt_login": "hyatt",
                "marriott_login": "marriott",
            }.get(role)
            if account_role:
                profile.setdefault("accounts", {})[account_role] = "created"
            profile["sync_error"] = None
            profile["updated_at"] = _now()
            self._save()
            return dict(profile)

    def set_sync_error(self, employee_id: str, message: Optional[str]) -> None:
        with self._lock:
            profile = self.data["profiles"][employee_id]
            profile["sync_error"] = message
            profile["updated_at"] = _now()
            self._save()

    def mark_account(self, employee_id: str, service: str, status: str) -> None:
        with self._lock:
            profile = self.data["profiles"][employee_id]
            profile.setdefault("accounts", {})[service] = status
            profile["updated_at"] = _now()
            self._save()

    def start_deletion(
        self,
        employee_id: str,
        trashed_item_ids: Iterable[str],
        failed_item_ids: Iterable[str],
        purge_after: datetime,
    ) -> None:
        with self._lock:
            profile = self.data["profiles"][employee_id]
            failed = list(failed_item_ids)
            profile["deletion"] = {
                "status": "partial" if failed else "pending",
                "trashed_at": _now(),
                "purge_after": purge_after.astimezone(timezone.utc).isoformat(),
                "trashed_item_ids": list(trashed_item_ids),
                "failed_item_ids": failed,
            }
            profile["updated_at"] = _now()
            self._save()

    def clear_deletion(self, employee_id: str) -> None:
        with self._lock:
            profile = self.data["profiles"][employee_id]
            profile["deletion"] = None
            for ref in profile.get("vault_refs", {}).values():
                ref["trashed"] = False
            profile["updated_at"] = _now()
            self._save()

    def mark_purged(
        self,
        employee_id: str,
        failed_item_ids: Iterable[str],
        purged_item_ids: Iterable[str] = (),
    ) -> None:
        with self._lock:
            profile = self.data["profiles"][employee_id]
            failed = list(failed_item_ids)
            deletion = profile.setdefault("deletion", {})
            deletion["status"] = "purge_failed" if failed else "purged"
            deletion["failed_item_ids"] = failed
            already_purged = list(deletion.get("purged_item_ids") or [])
            for item_id in purged_item_ids:
                if item_id not in already_purged:
                    already_purged.append(item_id)
            deletion["purged_item_ids"] = already_purged
            deletion["purged_at"] = _now() if not failed else None
            profile["updated_at"] = _now()
            self._save()

    def migrate_retention(self, retention_data: Dict[str, Any]) -> None:
        for display_name, legacy in (retention_data.get("employees") or {}).items():
            metadata = legacy.get("profile") or {}
            aliases = legacy.get("aliases") or []
            username = metadata.get("username") or next(
                (alias for alias in aliases if "@" not in alias),
                "",
            )
            email = metadata.get("email") or next(
                (alias for alias in aliases if "@" in alias),
                "",
            )
            self.upsert(
                display_name=display_name,
                first_name=metadata.get("first_name", ""),
                last_name=metadata.get("last_name", ""),
                username=username,
                email=email,
                employee_id=legacy.get("employee_id"),
                accounts=legacy.get("accounts"),
            )


class ProfileSyncService:
    """Reconciles live Bitwarden items with non-secret local profile metadata."""

    def __init__(self, bitwarden: Any, store: EmployeeProfileStore):
        self.bitwarden = bitwarden
        self.store = store

    @staticmethod
    def _custom_field(item: Dict[str, Any], name: str) -> Optional[str]:
        for field in item.get("fields") or []:
            if field.get("name") == name and field.get("value"):
                return str(field["value"])
        return None

    @staticmethod
    def tagged_fields(employee_id: str, role: str) -> List[Dict[str, Any]]:
        return [
            {"name": EMPLOYEE_ID_FIELD, "value": employee_id, "type": 1},
            {"name": RECORD_ROLE_FIELD, "value": role, "type": 1},
        ]

    @staticmethod
    def _legacy_role(name: str) -> Optional[str]:
        suffixes = {
            "— Work Identity": "identity",
            "— Work Login": "email_login",
            "— Work Card": "work_card",
            "— Hyatt Login": "hyatt_login",
            "— Marriott Login": "marriott_login",
        }
        return next((role for suffix, role in suffixes.items() if name.endswith(suffix)), None)

    def sync_profiles(self) -> List[Dict[str, Any]]:
        self.bitwarden.sync()
        items = self.bitwarden.list_items()
        profiles = {
            profile["employee_id"]: profile
            for profile in self.store.list_profiles(include_purged=True)
        }
        legacy_matches: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
        for item in items:
            employee_id = self._custom_field(item, EMPLOYEE_ID_FIELD)
            role = self._custom_field(item, RECORD_ROLE_FIELD)
            if employee_id in profiles and role in RECORD_ROLES:
                self.store.bind_vault_ref(employee_id, role, item)
                continue
            legacy_role = self._legacy_role(str(item.get("name") or ""))
            if legacy_role:
                display_name = str(item["name"]).rsplit(" — ", 1)[0]
                legacy_matches.setdefault(
                    (display_name.casefold(), legacy_role),
                    [],
                ).append(item)

        for profile in profiles.values():
            for role in ("identity", "email_login", "work_card"):
                if role in (profile.get("vault_refs") or {}):
                    continue
                matches = legacy_matches.get(
                    (profile["display_name"].casefold(), role),
                    [],
                )
                if len(matches) == 1:
                    self.store.bind_vault_ref(profile["employee_id"], role, matches[0])
                elif len(matches) > 1:
                    self.store.set_sync_error(
                        profile["employee_id"],
                        f"Ambiguous legacy {role} records",
                    )
        return self.store.list_profiles()

    def get_bundle(self, employee_id: str) -> Dict[str, Dict[str, Any]]:
        profile = self.store.get(employee_id)
        if profile is None:
            raise KeyError(employee_id)
        bundle: Dict[str, Dict[str, Any]] = {}
        for role, ref in (profile.get("vault_refs") or {}).items():
            bundle[role] = self.bitwarden.get_item(ref["item_id"])
        return bundle

    def create_login(
        self,
        employee_id: str,
        role: str,
        service_name: str,
        username: str,
        password: str,
        uri: str,
    ) -> Dict[str, Any]:
        profile = self.store.get(employee_id)
        if profile is None:
            raise KeyError(employee_id)
        payload = {
            "type": 1,
            "name": f"{profile['display_name']} — {service_name} Login",
            "notes": None,
            "favorite": False,
            "fields": self.tagged_fields(employee_id, role),
            "login": {
                "username": username,
                "password": password,
                "totp": None,
                "uris": [{"uri": uri, "match": None}],
            },
        }
        item = self.bitwarden.create_item(payload)
        self.store.bind_vault_ref(employee_id, role, item)
        return item

    def edit_identity(
        self,
        employee_id: str,
        updates: Dict[str, str],
        expected_revision: Optional[str],
    ) -> Dict[str, Any]:
        profile = self.store.get(employee_id)
        if profile is None:
            raise KeyError(employee_id)
        ref = (profile.get("vault_refs") or {}).get("identity")
        if not ref:
            raise RuntimeError("Identity record is not bound.")
        self.bitwarden.sync()
        current = self.bitwarden.get_item(ref["item_id"])
        if expected_revision and current.get("revisionDate") != expected_revision:
            raise RuntimeError("Bitwarden identity changed; reload before saving.")
        identity = current.setdefault("identity", {})
        for key, value in updates.items():
            identity[key] = value
        edited = self.bitwarden.edit_item(ref["item_id"], current)
        self.store.bind_vault_ref(employee_id, "identity", edited)
        return edited

    def trash_bundle(self, employee_id: str) -> Dict[str, List[str]]:
        profile = self.store.get(employee_id)
        if profile is None:
            raise KeyError(employee_id)
        trashed: List[str] = []
        failed: List[str] = []
        for ref in (profile.get("vault_refs") or {}).values():
            item_id = ref["item_id"]
            try:
                self.bitwarden.trash_item(item_id)
                trashed.append(item_id)
            except Exception:
                failed.append(item_id)
        self.store.start_deletion(
            employee_id,
            trashed,
            failed,
            datetime.now(timezone.utc) + timedelta(days=2),
        )
        return {"trashed": trashed, "failed": failed}

    def restore_bundle(self, employee_id: str) -> Dict[str, List[str]]:
        profile = self.store.get(employee_id)
        if profile is None:
            raise KeyError(employee_id)
        deletion = profile.get("deletion") or {}
        restored: List[str] = []
        failed: List[str] = []
        for item_id in deletion.get("trashed_item_ids") or []:
            try:
                self.bitwarden.restore_item(item_id)
                restored.append(item_id)
            except Exception:
                failed.append(item_id)
        if not failed:
            self.store.clear_deletion(employee_id)
        return {"restored": restored, "failed": failed}

    def purge_due(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        results: List[Dict[str, Any]] = []
        for profile in self.store.list_profiles(include_purged=True):
            deletion = profile.get("deletion") or {}
            if deletion.get("status") not in {"pending", "purge_failed"}:
                continue
            purge_after = datetime.fromisoformat(deletion["purge_after"])
            if purge_after > current:
                continue
            failed: List[str] = []
            purged: List[str] = []
            item_ids = (
                deletion.get("failed_item_ids")
                if deletion.get("status") == "purge_failed"
                else deletion.get("trashed_item_ids")
            ) or []
            for item_id in item_ids:
                try:
                    self.bitwarden.delete_item_permanently(item_id)
                    purged.append(item_id)
                except Exception:
                    failed.append(item_id)
            self.store.mark_purged(profile["employee_id"], failed, purged)
            results.append(
                {
                    "employee_id": profile["employee_id"],
                    "failed": failed,
                }
            )
        return results
