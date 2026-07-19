# DOWNLOWd Security Features

## Overview

Security controls for application and Bitwarden authentication, local settings, transaction data, retention, and audit logging.

## Features

### 1. macOS Keychain Integration

- Credentials stored via `keyring` under service `DOWNLOWD`
- On first run after upgrade, plaintext `~/.onboarding_credentials.json` is migrated into Keychain
- After a successful migration the source file is **securely overwritten and deleted** (no `.json.backup` left behind)
- If migration fails mid-way, the original file is left intact and an error is logged

### 2. Application Authentication (PBKDF2 + Bitwarden)

- First launch requires creating a separate app password (minimum 8 characters)
- The app password is stored as a PBKDF2-HMAC-SHA256 hash (200,000 iterations) with a random salt in Keychain
- Correct password verification creates a random Keychain session token with a one-hour timeout
- The main window opens only after app-password verification and a successful `bw login` or `bw unlock`
- The Bitwarden master password is passed to the CLI through a process environment variable and is not persisted by DOWNLOWd
- The returned `BW_SESSION` value is held in process memory and passed only to child `bw` commands
- Failed login/unlock and cancelled 2FA clear the in-memory session
- Authentication success/failure/cancellation events are written to the audit log

Keychain keys: `app_password_hash`, `app_password_salt`, `app_session_token`, `app_session_created_at`.

### 3. Transaction Logging

- Local SQLite at `~/.downlowd_transactions.db`
- File mode is enforced as **`0o600`** before every SQLite connection
- **Not encrypted at rest** (SQLCipher is out of scope for this release; tracked as future work)
- Add / list / export CSV / delete by database id (Treeview `iid`)

### 4. Automated Data Retention

Independent milestone checks (overdue day-15/20 are not blocked by unfinished day-5/10):

| Day | Action |
|-----|--------|
| 5 | GUI prompt: is employee still active? |
| 10 | GUI prompt: approve shredding? |
| 15 | Auto-shred employee transactions |
| 20 | Secure-delete matching log files; scrub employee lines from audit log (fail closed on errors) |

- Scheduler starts after successful app and Bitwarden authentication
- Employees are registered from the onboarding pipeline after a successful convert

### 5. Security Audit Logging

File: `~/.downlowd_audit.log`

Logged events include: authentication, imports, deletions, transaction add/delete, retention actions, collection name config changes.

### 6. Bitwarden CLI Session

- Session key from `bw unlock --raw` / `bw login --raw` is kept on `BitwardenService`
- Subsequent CLI calls set `BW_SESSION` in the subprocess environment
- Session is cleared on failed unlock/login
- Named organization collections must resolve exactly; lookup failures do not fall back to Personal Vault

### 7. Secure Temporary Files

- Import temp files under `~/.downlowd_temp/` (`0o700` dir, `0o600` files)
- Multi-pass overwrite before unlink

### 8. Bitwarden-Synced Employee Profiles

- Versioned, owner-only metadata at `~/.downlowd_profiles.json`, keyed by immutable employee UUID
- Local records contain display metadata and vault item references only—never passwords, card numbers, CVVs, SSNs, or DOB
- New imports include hidden employee-ID and record-role fields, then reconcile actual Bitwarden item IDs after `bw sync`
- Legacy records require one unique exact employee/role match; ambiguous matches remain unresolved
- Identity edits reload and compare `revisionDate` before saving and preserve unknown item fields
- Identity, Email Login, Hyatt, Marriott, and Work Card values load only when selected and are cleared from the viewer on close, sync, or session expiry
- Profile deletion trashes only bound item IDs. Restore remains available for two days; permanent purge occurs only after the deadline with an unlocked vault
- Partial trash, restore, and purge failures remain visible and retryable. Audit entries contain employee UUIDs and redacted item IDs, not vault values

## File layout

| Path | Role |
|------|------|
| `integrations.py` | Keychain settings, PBKDF2 app auth, and Bitwarden gateway |
| `onboarding.py` | Pipeline orchestrator |
| `bw_import_converter.py` | HQ → Bitwarden JSON (single converter source) |
| `transaction_db.py` | SQLite transactions |
| `data_retention.py` | Retention schedule + shred |
| `employee_profiles.py` | UUID profile metadata, reconciliation, edit, trash/restore/purge |
| `audit_logger.py` | Audit trail |
| `account_automation.py` | Partner Selenium prefill + clipboard/browser fallback |

## Dependencies

- `keyring>=24.0.0`
- `requests`, `selenium`, `msal`, `tkinterdnd2-universal` (DnD optional; falls back on Python builds without Tk DnD)
- **No** `pysqlcipher` / SQLCipher in this release

## Best practices

1. Use separate strong app and Bitwarden passwords; enable Bitwarden 2FA
2. Review `~/.downlowd_audit.log` periodically
3. Treat `~/.downlowd_transactions.db` as sensitive — **FileVault required for production**; the app warns at launch if FileVault is Off
4. Respond to retention prompts promptly

## Future work

- Full SQLCipher (or equivalent) encryption at rest
- Biometric unlock
- Deeper anti-bot partner enrollment automation

## Version

- **0.2.2** — Selenium partner prefill, tracked day-20 logs, FileVault launch warning, Python 3.14 Tk DnD fallback
- Compatibility: macOS 10.15+, Python 3.8+
