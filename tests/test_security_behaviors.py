import json
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import integrations
import onboarding
import data_retention
from employee_profiles import (
    EMPLOYEE_ID_FIELD,
    RECORD_ROLE_FIELD,
    EmployeeProfileStore,
    ProfileSyncService,
)
from bw_import_converter import BitwardenConverter
from data_retention import DataRetentionManager
from integrations import (
    APP_PASSWORD_HASH_KEY,
    APP_SESSION_CREATED_KEY,
    APP_SESSION_TOKEN_KEY,
    BitwardenService,
    CredentialStore,
    SessionManager,
)
from gui import Dashboard
from onboarding import BitwardenConfig, Onboarding, OnboardingConfig
from transaction_db import TransactionDatabase


class FakeAudit:
    def __init__(self):
        self.events = []

    def log_retention_action(self, *args):
        self.events.append(("retention", args))

    def log_deletion(self, *args, **kwargs):
        self.events.append(("deletion", args, kwargs))

    def log_security_event(self, *args):
        self.events.append(("security", args))

    def log_import_operation(self, *args):
        self.events.append(("import", args))


class FakeTransactionDatabase:
    def __init__(self, delete_result=True):
        self.delete_result = delete_result

    def delete_employee_transactions(self, _employee):
        return self.delete_result


class MemoryCredentialStore:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def update(self, new_values):
        self.values.update(new_values)


class FakeBitwarden:
    def resolve_collection(self, _name):
        return None

    def import_json(self, _payload, _collection):
        return None


class FakeAccountCreator:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.reset_count = 0

    def _record(self, service, _personal_data, account_name):
        self.calls.append((service, account_name))
        return {"service": service, "filled_fields": ["email"]}

    def create_outlook_account(self, personal_data, account_name):
        return self._record("Outlook", personal_data, account_name)

    def create_hyatt_account(self, personal_data, account_name):
        return self._record("Hyatt", personal_data, account_name)

    def create_marriott_account(self, personal_data, account_name):
        return self._record("Marriott", personal_data, account_name)

    def close_browser(self):
        self.closed = True

    def reset_browser_session(self):
        self.reset_count += 1


def make_retention_manager(delete_result=True):
    manager = DataRetentionManager.__new__(DataRetentionManager)
    manager.transaction_db = FakeTransactionDatabase(delete_result)
    manager.audit = FakeAudit()
    manager.retention_data = {"employees": {}, "last_check": None}
    manager.prompt_callback = None
    manager._running = False
    manager._scheduler_thread = None
    manager._save_retention_data = lambda: None
    return manager


class BitwardenSessionTests(unittest.TestCase):
    def test_empty_instance_session_removes_inherited_session(self):
        service = BitwardenService()
        with mock.patch.dict(os.environ, {"BW_SESSION": "stale"}):
            self.assertNotIn("BW_SESSION", service._env_with_session())

    def test_instance_session_is_propagated(self):
        service = BitwardenService()
        service.session_key = "current"
        self.assertEqual(service._env_with_session()["BW_SESSION"], "current")

    def test_missing_named_collection_does_not_fall_back_to_personal(self):
        service = BitwardenService()
        result = subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
        with mock.patch.object(service, "_run_bw", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "Select 'Personal Vault' explicitly"):
                service.resolve_collection("Employee Onboarding")

    def test_duplicate_named_collections_are_rejected(self):
        service = BitwardenService()
        result = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                '[{"id":"one","name":"Shared","organizationId":"org-a"},'
                '{"id":"two","name":"Shared","organizationId":"org-b"}]'
            ),
            stderr="",
        )
        with mock.patch.object(service, "_run_bw", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "ambiguous across organizations"):
                service.resolve_collection("Shared")


class AppSessionTests(unittest.TestCase):
    def test_password_is_hashed_and_wrong_password_is_rejected(self):
        store = MemoryCredentialStore()
        manager = SessionManager(store)
        self.assertTrue(manager.set_password("correct horse"))
        self.assertNotEqual(store.get(APP_PASSWORD_HASH_KEY), "correct horse")
        self.assertTrue(manager.verify_password("correct horse"))
        self.assertFalse(manager.verify_password("wrong password"))

    def test_random_session_is_created_and_expires(self):
        store = MemoryCredentialStore()
        manager = SessionManager(store)
        self.assertTrue(manager.set_password("correct horse"))
        with mock.patch.object(integrations.time, "time", return_value=1000.0):
            self.assertTrue(manager.create_session("correct horse"))
            self.assertTrue(manager.is_authenticated())
        self.assertNotEqual(store.get(APP_SESSION_TOKEN_KEY), "correct horse")
        self.assertEqual(store.get(APP_SESSION_CREATED_KEY), "1000.0")

        with mock.patch.object(integrations.time, "time", return_value=4601.0):
            self.assertFalse(manager.is_authenticated())
        self.assertEqual(store.get(APP_SESSION_TOKEN_KEY), "")


class CredentialMigrationTests(unittest.TestCase):
    def test_verified_migration_removes_plaintext_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "credentials.json"
            source.write_text('{"token": "secret"}', encoding="utf-8")
            keychain = {}

            def set_password(_service, key, value):
                keychain[key] = value

            with (
                mock.patch.object(integrations, "CREDENTIALS_FILE", source),
                mock.patch.object(
                    integrations.keyring,
                    "get_password",
                    side_effect=lambda _service, key: keychain.get(key),
                ),
                mock.patch.object(integrations.keyring, "set_password", side_effect=set_password),
            ):
                CredentialStore()
            self.assertEqual(keychain["token"], "secret")
            self.assertFalse(source.exists())

    def test_failed_keychain_verification_keeps_plaintext_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "credentials.json"
            source.write_text('{"token": "secret"}', encoding="utf-8")
            with (
                mock.patch.object(integrations, "CREDENTIALS_FILE", source),
                mock.patch.object(integrations.keyring, "get_password", return_value=None),
                mock.patch.object(integrations.keyring, "set_password"),
            ):
                with self.assertLogs(level="ERROR"):
                    CredentialStore()
            self.assertTrue(source.exists())
            self.assertEqual(source.read_text(encoding="utf-8"), '{"token": "secret"}')


class TransactionDatabaseTests(unittest.TestCase):
    def test_permissions_and_missing_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "transactions.db"
            database = TransactionDatabase(db_path)
            self.assertEqual(stat.S_IMODE(db_path.stat().st_mode), 0o600)
            with self.assertLogs(level="WARNING"):
                self.assertFalse(database.delete_transaction(999))

    def test_transactions_can_be_linked_to_immutable_employee_id(self):
        with tempfile.TemporaryDirectory() as directory:
            database = TransactionDatabase(Path(directory) / "transactions.db")
            database.add_transaction(
                "2026-07-19",
                12.50,
                "Example",
                "Ada Lovelace",
                "1111",
            )
            self.assertEqual(database.link_employee("Ada Lovelace", "employee-uuid"), 1)
            transactions = database.get_transactions_by_employee_id("employee-uuid")
            self.assertEqual(len(transactions), 1)
            self.assertEqual(transactions[0]["employee_name"], "Ada Lovelace")

    def test_employee_budget_combines_opening_spend_and_transactions(self):
        with tempfile.TemporaryDirectory() as directory:
            database = TransactionDatabase(Path(directory) / "transactions.db")
            self.assertTrue(
                database.set_employee_budget(
                    "employee-uuid",
                    "Ada Lovelace",
                    125.0,
                    1000.0,
                )
            )
            database.add_transaction(
                "2026-07-19",
                75.0,
                "Example",
                "Ada Lovelace",
                "1111",
                employee_id="employee-uuid",
            )
            budget = database.get_employee_budgets()[0]
            self.assertEqual(budget["total_spent"], 200.0)
            self.assertEqual(budget["spend_limit"], 1000.0)


class RetentionTests(unittest.TestCase):
    def test_all_overdue_milestones_are_returned_independently(self):
        manager = make_retention_manager()
        manager.retention_data["employees"]["Example Employee"] = {
            "registered_date": (datetime.now() - timedelta(days=25)).isoformat(),
            "status": "active",
            "day5_audit": False,
            "day10_audit": False,
            "day15_shredded": False,
            "day20_logs_shredded": False,
        }
        self.assertEqual(
            {action["day"] for action in manager.check_retention_schedule()},
            {5, 10, 15, 20},
        )

    def test_failed_transaction_deletion_does_not_mark_day15_complete(self):
        manager = make_retention_manager(delete_result=False)
        manager.retention_data["employees"]["Example Employee"] = {
            "day15_shredded": False,
            "status": "active",
        }
        with self.assertLogs(level="ERROR"):
            self.assertFalse(manager.execute_auto_shred("Example Employee"))
        employee = manager.retention_data["employees"]["Example Employee"]
        self.assertFalse(employee["day15_shredded"])
        self.assertEqual(employee["status"], "active")

    def test_shared_log_scrub_removes_subject_and_restricts_permissions(self):
        manager = make_retention_manager()
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "shared.log"
            log_path.write_text(
                "keep this line\nExample Employee sensitive line\n",
                encoding="utf-8",
            )
            self.assertTrue(manager._scrub_employee_lines(log_path, "Example Employee"))
            self.assertEqual(log_path.read_text(encoding="utf-8"), "keep this line\n")
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)

    def test_registration_tracks_username_and_email_aliases(self):
        manager = make_retention_manager()
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(data_retention, "LOGS_DIR", Path(directory)):
                manager.register_employee(
                    "Example Employee",
                    "2026-07-18",
                    aliases=["exampleemployee1980", "example@outlook.com"],
                    profile={
                        "first_name": "Example",
                        "last_name": "Employee",
                        "username": "exampleemployee1980",
                        "email": "example@outlook.com",
                    },
                )
        self.assertEqual(
            manager.retention_data["employees"]["Example Employee"]["aliases"],
            ["exampleemployee1980", "example@outlook.com"],
        )
        profile = manager.get_employee_profile("Example Employee")
        self.assertEqual(profile["username"], "exampleemployee1980")
        self.assertEqual(
            profile["accounts"],
            {"email": "pending", "hyatt": "pending", "marriott": "pending"},
        )
        self.assertTrue(
            manager.update_account_status("Example Employee", "email", "created")
        )
        self.assertEqual(
            manager.get_employee_profile("Example Employee")["accounts"]["email"],
            "created",
        )

    def test_scheduler_starts_only_one_immediate_worker(self):
        manager = make_retention_manager()
        thread = mock.Mock()
        with mock.patch.object(data_retention.threading, "Thread", return_value=thread) as thread_cls:
            manager.start_scheduler(check_interval_hours=24)
        self.assertEqual(thread_cls.call_count, 1)
        thread.start.assert_called_once_with()


class OnboardingTests(unittest.TestCase):
    def test_resume_accounts_skips_completed_services_and_persists_progress(self):
        retention = make_retention_manager()
        retention.retention_data["employees"]["Example Employee"] = {
            "profile": {
                "first_name": "Example",
                "last_name": "Employee",
                "username": "exampleemployee1980",
                "email": "exampleemployee1980@outlook.com",
            },
            "accounts": {
                "email": "created",
                "hyatt": "pending",
                "marriott": "pending",
            },
        }
        account_creator = FakeAccountCreator()
        pipeline = Onboarding(
            FakeBitwarden(),
            retention_manager=retention,
            account_creator=account_creator,
        )
        pipeline.resume_accounts(
            "Example Employee",
            "shared passphrase",
            OnboardingConfig(bw=BitwardenConfig("Personal Vault")),
            account_confirmation_callback=lambda _service, _employee, _result: True,
        )
        self.assertEqual(
            account_creator.calls,
            [
                ("Hyatt", "exampleemployee1980"),
                ("Marriott", "exampleemployee1980"),
            ],
        )
        accounts = retention.get_employee_profile("Example Employee")["accounts"]
        self.assertEqual(accounts["email"], "created")
        self.assertEqual(accounts["hyatt"], "created")
        self.assertEqual(accounts["marriott"], "created")

    def test_accounts_run_in_global_dependency_stages(self):
        audit = FakeAudit()
        account_creator = FakeAccountCreator()
        confirmations = []
        employees = [
            {
                "full_name": "Alpha Person",
                "first_name": "Alpha",
                "last_name": "Person",
                "username": "alphaperson1980",
                "email": "alphaperson1980@outlook.com",
            },
            {
                "full_name": "Beta Person",
                "first_name": "Beta",
                "last_name": "Person",
                "username": "betaperson1981",
                "email": "betaperson1981@outlook.com",
            },
        ]

        def convert(_source, output, _password):
            output.write_text('{"items": []}', encoding="utf-8")
            return {"items_generated": 6, "employees": employees}

        def confirm(service, employee, _result):
            confirmations.append((service, employee["username"]))
            return True

        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            temp_dir = downloads / "secure-temp"
            (downloads / "HQ-123.txt").write_text("sample", encoding="utf-8")
            with (
                mock.patch.object(onboarding, "get_audit_logger", return_value=audit),
                mock.patch.object(onboarding, "TEMP_DIR", temp_dir),
                mock.patch.object(onboarding, "convert_file_to_bitwarden_json", side_effect=convert),
                mock.patch.object(
                    onboarding,
                    "secure_delete_file",
                    side_effect=lambda path, mode: Path(path).unlink(),
                ),
            ):
                pipeline = Onboarding(FakeBitwarden(), account_creator=account_creator)
                pipeline.run(
                    downloads,
                    "shared passphrase",
                    OnboardingConfig(bw=BitwardenConfig("Personal Vault")),
                    account_confirmation_callback=confirm,
                )

        expected = [
            ("Outlook", "alphaperson1980"),
            ("Outlook", "betaperson1981"),
            ("Hyatt", "alphaperson1980"),
            ("Hyatt", "betaperson1981"),
            ("Marriott", "alphaperson1980"),
            ("Marriott", "betaperson1981"),
        ]
        self.assertEqual(account_creator.calls, expected)
        self.assertEqual(confirmations, expected)
        self.assertEqual(account_creator.reset_count, len(expected))
        self.assertTrue(account_creator.closed)

    def test_lockdown_disposes_exact_source_and_generated_json_paths(self):
        audit = FakeAudit()
        disposed = []

        def convert(source, output, _password):
            output.write_text('{"items": []}', encoding="utf-8")
            return {"items_generated": 3, "employees": []}

        def dispose(path, mode):
            disposed.append((Path(path), mode))
            Path(path).unlink()

        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            temp_dir = downloads / "secure-temp"
            source = downloads / "HQ-123.txt"
            source.write_text("sample", encoding="utf-8")
            with (
                mock.patch.object(onboarding, "get_audit_logger", return_value=audit),
                mock.patch.object(onboarding, "TEMP_DIR", temp_dir),
                mock.patch.object(onboarding, "convert_file_to_bitwarden_json", side_effect=convert),
                mock.patch.object(onboarding, "secure_delete_file", side_effect=dispose),
            ):
                pipeline = Onboarding(FakeBitwarden())
                pipeline.run(
                    downloads,
                    "shared passphrase",
                    OnboardingConfig(
                        bw=BitwardenConfig("Personal Vault"),
                        provision_outlook=False,
                        provision_hyatt=False,
                        provision_marriott=False,
                    ),
                )

            disposed_paths = {path for path, _mode in disposed}
            self.assertIn(source, disposed_paths)
            generated = disposed_paths - {source}
            self.assertEqual(len(generated), 1)
            self.assertEqual(next(iter(generated)).parent, temp_dir)

    def test_converter_output_is_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            output = root / "output.json"
            source.write_text("unused", encoding="utf-8")
            converter = BitwardenConverter(source, output, "password")
            converter._write_output_file([])
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_converter_tags_all_records_with_one_employee_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "HQ-123.txt"
            source.write_text(
                "firstname|lastname|dob|cc|cvv|expmonth|expyear\n"
                "Ada|Lovelace|12/10/1815|4111111111111111|123|04|2030\n",
                encoding="utf-8",
            )
            converter = BitwardenConverter(source, root / "output.json", "password")
            items, employees = converter._process_input_file()
            employee_id = employees[0]["employee_id"]
            roles = set()
            for item in items:
                fields = {field["name"]: field["value"] for field in item["fields"]}
                self.assertEqual(fields[EMPLOYEE_ID_FIELD], employee_id)
                roles.add(fields[RECORD_ROLE_FIELD])
            self.assertEqual(roles, {"email_login", "identity", "work_card"})
            identity = next(item for item in items if item["type"] == 4)
            self.assertEqual(identity["identity"]["firstName"], "Ada")
            self.assertEqual(identity["identity"]["lastName"], "Lovelace")
            self.assertIn(
                "Date of Birth",
                {field["name"] for field in identity["fields"]},
            )

    def test_failed_import_disposes_generated_json_but_keeps_source(self):
        audit = FakeAudit()
        disposed = []

        def convert(_source, output, _password):
            output.write_text('{"items": []}', encoding="utf-8")
            return {"items_generated": 3, "employees": []}

        def dispose(path, mode):
            disposed.append(Path(path))
            Path(path).unlink()

        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory)
            temp_dir = downloads / "secure-temp"
            source = downloads / "HQ-123.txt"
            source.write_text("sample", encoding="utf-8")
            bitwarden = FakeBitwarden()
            bitwarden.import_json = mock.Mock(side_effect=RuntimeError("import failed"))
            with (
                mock.patch.object(onboarding, "get_audit_logger", return_value=audit),
                mock.patch.object(onboarding, "TEMP_DIR", temp_dir),
                mock.patch.object(onboarding, "convert_file_to_bitwarden_json", side_effect=convert),
                mock.patch.object(onboarding, "secure_delete_file", side_effect=dispose),
            ):
                pipeline = Onboarding(bitwarden)
                with self.assertRaisesRegex(RuntimeError, "import failed"):
                    pipeline.run(
                        downloads,
                        "shared passphrase",
                        OnboardingConfig(
                            bw=BitwardenConfig("Personal Vault"),
                            provision_outlook=False,
                            provision_hyatt=False,
                            provision_marriott=False,
                        ),
                    )

            self.assertTrue(source.exists())
            self.assertEqual(len(disposed), 1)
            self.assertEqual(disposed[0].parent, temp_dir)


class BitwardenItemApiTests(unittest.TestCase):
    def test_create_item_encodes_payload_without_writing_it_to_disk(self):
        service = BitwardenService()
        calls = []

        def run(args, **kwargs):
            calls.append((args, kwargs))
            if args == ["encode"]:
                return subprocess.CompletedProcess(args, 0, stdout="encoded-value\n", stderr="")
            return subprocess.CompletedProcess(
                args,
                0,
                stdout='{"id":"item-1","revisionDate":"revision-1"}',
                stderr="",
            )

        with mock.patch.object(service, "_run_bw", side_effect=run):
            created = service.create_item({"type": 1, "name": "Record"})

        self.assertEqual(created["id"], "item-1")
        self.assertEqual(json.loads(calls[0][1]["input"])["name"], "Record")
        self.assertEqual(calls[1][0], ["create", "item", "encoded-value"])

    def test_item_lifecycle_commands_are_session_aware(self):
        service = BitwardenService()
        service.session_key = "session"
        results = [
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout='{"id":"item-1"}', stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]
        with mock.patch.object(service, "_run_bw", side_effect=results) as run:
            service.trash_item("item-1")
            service.restore_item("item-1")
            service.delete_item_permanently("item-1")
        self.assertEqual(run.call_args_list[0].args[0], ["delete", "item", "item-1"])
        self.assertEqual(run.call_args_list[1].args[0], ["restore", "item", "item-1"])
        self.assertEqual(
            run.call_args_list[2].args[0],
            ["delete", "item", "item-1", "--permanent"],
        )


class FakeProfileVault:
    def __init__(self, items=None):
        self.items = {item["id"]: dict(item) for item in (items or [])}
        self.synced = 0
        self.trashed = []
        self.restored = []
        self.deleted = []
        self.fail_trash = set()
        self.fail_delete = set()

    def sync(self):
        self.synced += 1

    def list_items(self):
        return list(self.items.values())

    def get_item(self, item_id):
        return dict(self.items[item_id])

    def create_item(self, payload):
        item = {
            **payload,
            "id": f"created-{len(self.items) + 1}",
            "revisionDate": "created-revision",
        }
        self.items[item["id"]] = item
        return dict(item)

    def edit_item(self, item_id, payload):
        item = {**payload, "id": item_id, "revisionDate": "new-revision"}
        self.items[item_id] = item
        return dict(item)

    def trash_item(self, item_id):
        if item_id in self.fail_trash:
            raise RuntimeError("trash failed")
        self.trashed.append(item_id)

    def restore_item(self, item_id):
        self.restored.append(item_id)
        return {"id": item_id}

    def delete_item_permanently(self, item_id):
        if item_id in self.fail_delete:
            raise RuntimeError("delete failed")
        self.deleted.append(item_id)


class EmployeeProfileTests(unittest.TestCase):
    def _store(self, directory):
        return EmployeeProfileStore(Path(directory) / "profiles.json")

    @staticmethod
    def _tagged_item(item_id, employee_id, role, name, revision="r1"):
        return {
            "id": item_id,
            "type": 4 if role == "identity" else 1,
            "name": name,
            "revisionDate": revision,
            "fields": [
                {"name": EMPLOYEE_ID_FIELD, "value": employee_id, "type": 1},
                {"name": RECORD_ROLE_FIELD, "value": role, "type": 1},
            ],
        }

    def test_store_migrates_legacy_metadata_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            store.migrate_retention(
                {
                    "employees": {
                        "Ada Lovelace": {
                            "profile": {
                                "first_name": "Ada",
                                "last_name": "Lovelace",
                                "email": "ada@example.com",
                            },
                            "accounts": {"email": "created"},
                        }
                    }
                }
            )
            profile = store.list_profiles()[0]
            self.assertEqual(profile["email"], "ada@example.com")
            self.assertEqual(profile["accounts"]["email"], "created")
            self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
            self.assertNotIn("password", store.path.read_text(encoding="utf-8").lower())

    def test_tagged_items_reconcile_to_immutable_employee_id(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            profile = store.upsert(display_name="Ada Lovelace")
            item = self._tagged_item(
                "identity-1",
                profile["employee_id"],
                "identity",
                "Ada Lovelace — Work Identity",
            )
            service = ProfileSyncService(FakeProfileVault([item]), store)
            service.sync_profiles()
            self.assertEqual(
                store.get(profile["employee_id"])["vault_refs"]["identity"]["item_id"],
                "identity-1",
            )

    def test_one_stale_vault_reference_does_not_blank_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            profile = store.upsert(display_name="Ada Lovelace")
            for role, item_id, item_type in (
                ("identity", "identity-1", 2),
                ("work_card", "missing-card", 3),
            ):
                store.bind_vault_ref(
                    profile["employee_id"],
                    role,
                    {"id": item_id, "type": item_type, "revisionDate": "r1"},
                )
            vault = FakeProfileVault()

            def get_item(item_id):
                if item_id == "missing-card":
                    raise RuntimeError("not found")
                return {
                    "id": item_id,
                    "type": 4,
                    "identity": {"firstName": "Ada"},
                }

            vault.get_item = get_item
            bundle = ProfileSyncService(vault, store).get_bundle(profile["employee_id"])
            self.assertEqual(bundle["identity"]["identity"]["firstName"], "Ada")
            self.assertEqual(bundle["work_card"]["_load_error"], "RuntimeError")

    def test_ambiguous_legacy_records_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            profile = store.upsert(display_name="Ada Lovelace")
            items = [
                {
                    "id": item_id,
                    "name": "Ada Lovelace — Work Identity",
                    "type": 4,
                    "fields": [],
                }
                for item_id in ("one", "two")
            ]
            ProfileSyncService(FakeProfileVault(items), store).sync_profiles()
            updated = store.get(profile["employee_id"])
            self.assertNotIn("identity", updated["vault_refs"])
            self.assertIn("Ambiguous", updated["sync_error"])

    def test_revision_conflict_prevents_identity_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            profile = store.upsert(display_name="Ada Lovelace")
            item = {
                **self._tagged_item(
                    "identity-1",
                    profile["employee_id"],
                    "identity",
                    "Ada Lovelace — Work Identity",
                    revision="server-revision",
                ),
                "identity": {"firstName": "Ada"},
                "unknown": {"preserve": True},
            }
            vault = FakeProfileVault([item])
            store.bind_vault_ref(profile["employee_id"], "identity", item)
            service = ProfileSyncService(vault, store)
            with self.assertRaisesRegex(RuntimeError, "reload"):
                service.edit_identity(
                    profile["employee_id"],
                    {"firstName": "Augusta"},
                    "stale-revision",
                )
            self.assertEqual(vault.items["identity-1"]["identity"]["firstName"], "Ada")

    def test_login_creation_binds_real_returned_item(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            profile = store.upsert(
                display_name="Ada Lovelace",
                email="ada@example.com",
            )
            service = ProfileSyncService(FakeProfileVault(), store)
            item = service.create_login(
                profile["employee_id"],
                "hyatt_login",
                "Hyatt",
                "ada@example.com",
                "memory-only-password",
                "https://hyatt.com/",
            )
            updated = store.get(profile["employee_id"])
            self.assertEqual(updated["vault_refs"]["hyatt_login"]["item_id"], item["id"])
            self.assertNotIn(
                "memory-only-password",
                store.path.read_text(encoding="utf-8"),
            )

    def test_partial_trash_remains_retryable_and_restore_clears_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            profile = store.upsert(display_name="Ada Lovelace")
            vault = FakeProfileVault()
            service = ProfileSyncService(vault, store)
            for role, item_id, item_type in (
                ("identity", "one", 4),
                ("work_card", "two", 3),
            ):
                store.bind_vault_ref(
                    profile["employee_id"],
                    role,
                    {"id": item_id, "type": item_type, "revisionDate": "r1"},
                )
            vault.fail_trash.add("two")
            result = service.trash_bundle(profile["employee_id"])
            self.assertEqual(result["failed"], ["two"])
            self.assertEqual(
                store.get(profile["employee_id"])["deletion"]["status"],
                "partial",
            )
            restored = service.restore_bundle(profile["employee_id"])
            self.assertEqual(restored["failed"], [])
            self.assertIsNone(store.get(profile["employee_id"])["deletion"])

    def test_purge_waits_for_deadline_and_all_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            profile = store.upsert(display_name="Ada Lovelace")
            store.bind_vault_ref(
                profile["employee_id"],
                "identity",
                {"id": "one", "type": 4, "revisionDate": "r1"},
            )
            vault = FakeProfileVault()
            service = ProfileSyncService(vault, store)
            service.trash_bundle(profile["employee_id"])
            deletion = store.data["profiles"][profile["employee_id"]]["deletion"]
            deletion["purge_after"] = (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat()
            store._save()
            self.assertEqual(service.purge_due(), [])
            vault.fail_delete.add("one")
            results = service.purge_due(datetime.now(timezone.utc) + timedelta(hours=2))
            self.assertEqual(results[0]["failed"], ["one"])
            self.assertEqual(
                store.get(profile["employee_id"])["deletion"]["status"],
                "purge_failed",
            )
            vault.fail_delete.clear()
            service.purge_due(datetime.now(timezone.utc) + timedelta(hours=2))
            self.assertEqual(
                store.get(profile["employee_id"])["deletion"]["status"],
                "purged",
            )

    def test_profile_viewer_clears_loaded_secrets_and_reveal_state(self):
        dashboard = Dashboard.__new__(Dashboard)
        dashboard.profile_bundle = {
            "email_login": {"login": {"password": "memory-only-secret"}}
        }
        dashboard._revealed_profile_values = {("email_login", "Password")}
        dashboard._clear_profile_secrets()
        self.assertEqual(dashboard.profile_bundle, {})
        self.assertEqual(dashboard._revealed_profile_values, set())

    def test_identity_viewer_includes_native_and_custom_fields(self):
        rows = Dashboard._identity_view_rows(
            {
                "name": "Ada Lovelace — Work Identity",
                "identity": {
                    "firstName": "Ada",
                    "lastName": "Lovelace",
                    "city": "London",
                },
                "fields": [
                    {"name": "Date of Birth", "value": "12/10/1815"},
                    {"name": EMPLOYEE_ID_FIELD, "value": "hidden"},
                    {"name": RECORD_ROLE_FIELD, "value": "identity"},
                ],
            }
        )
        values = {label: (value, sensitive) for label, value, sensitive in rows}
        self.assertEqual(values["First name"][0], "Ada")
        self.assertEqual(values["City"][0], "London")
        self.assertTrue(values["Date of Birth"][1])
        self.assertNotIn(EMPLOYEE_ID_FIELD, values)


if __name__ == "__main__":
    unittest.main()
