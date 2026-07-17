# DOWNLOWd Security Features

## Overview

Security controls for local credential storage, application unlock, Bitwarden CLI session handling, transaction data, retention, and audit logging.

## Features

### 1. macOS Keychain Integration

- Credentials stored via `keyring` under service `DOWNLOWD`
- On first run after upgrade, plaintext `~/.onboarding_credentials.json` is migrated into Keychain
- After a successful migration the source file is **securely overwritten and deleted** (no `.json.backup` left behind)
- If migration fails mid-way, the original file is left intact and an error is logged

### 2. Application Authentication (PBKDF2)

- First launch: set password (confirm required; minimum 8 characters)
- Password stored as **PBKDF2-HMAC-SHA256** (200,000 iterations) with a random salt in Keychain
- Later launches: verify password against the stored hash; mismatches are rejected
- On success, a random `secrets.token_urlsafe(32)` session token is stored with a created-at timestamp
- Session timeout: **1 hour**
- Auth success/failure events are written to the audit log

Keychain keys: `app_password_hash`, `app_password_salt`, `app_session_token`, `app_session_created_at`

### 3. Transaction Logging

- Local SQLite at `~/.downlowd_transactions.db`
- File mode set to **`0o600`** after create/open
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

- Scheduler starts after successful app auth
- Employees are registered from the onboarding pipeline after a successful convert

### 5. Security Audit Logging

File: `~/.downlowd_audit.log`

Logged events include: authentication, imports, deletions, transaction add/delete, retention actions, collection name config changes.

### 6. Bitwarden CLI Session

- Session key from `bw unlock --raw` / `bw login --raw` is kept on `BitwardenService`
- Subsequent CLI calls set `BW_SESSION` in the subprocess environment
- Session is cleared on failed unlock/login

### 7. Secure Temporary Files

- Import temp files under `~/.downlowd_temp/` (`0o700` dir, `0o600` files)
- Multi-pass overwrite before unlink

## File layout

| Path | Role |
|------|------|
| `integrations.py` | Keychain, PBKDF2 auth, Bitwarden gateway |
| `onboarding.py` | Pipeline orchestrator |
| `bw_import_converter.py` | HQ → Bitwarden JSON (single converter source) |
| `transaction_db.py` | SQLite transactions |
| `data_retention.py` | Retention schedule + shred |
| `audit_logger.py` | Audit trail |
| `account_automation.py` | Partner Selenium prefill + clipboard/browser fallback |

## Dependencies

- `keyring>=24.0.0`
- `requests`, `selenium`, `msal`, `tkinterdnd2-universal` (DnD optional; falls back on Python builds without Tk DnD)
- **No** `pysqlcipher` / SQLCipher in this release

## Best practices

1. Use a strong unique application password
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
