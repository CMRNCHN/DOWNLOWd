# DOWNLOWD - Employee Onboarding Appliance

An automated desktop tool for streamlining new employee onboarding tasks. The application monitors the Downloads folder for specific employee data files, converts them for Bitwarden import, optionally opens partner signup pages, and includes local transaction logging for company card expenses.

## Security Features

- **Two authentication gates** — a separate PBKDF2-protected app password, then Bitwarden login/unlock
- **macOS Keychain** — stores settings and remembered Bitwarden email
- **Transaction Logging** — local SQLite with owner-only (`0o600`) permissions — **not encrypted at rest** (FileVault recommended)
- **Local disposal modes** — standard unlink, overwrite-then-delete, or best-effort secure erase (APFS/SSD: FileVault is the real protection)
- **Automated Data Retention** — 5/10/15/20 day lifecycle
- **Audit logging** — auth, imports, transactions, retention, config

See [SECURITY_FEATURES.md](SECURITY_FEATURES.md) for details.

## Onboarding Workflow

1. **Unlock DOWNLOWd**, then sign in to Bitwarden
2. **Intake** — drop/browse `HQ-*.txt` / `HQ-*.rtf` into the queue
3. **Shared passphrase** — one passphrase for every new employee login (they change it later)
4. **Run full onboarding** — convert → Bitwarden import → all Outlook signups → all Hyatt signups → all Marriott signups → dispose local files
5. **Usernames** — `firstnamelastnameYEAR` (birth year)
6. **Profiles workspace** — live Identity, Email Login, Hyatt, Marriott, and Work Card records are fetched from Bitwarden on selection; secrets are masked by default
7. **Resume or edit** — resume missing accounts or revision-safely edit the native Identity record without rewriting unknown vault fields
8. **Recoverable deletion** — trash only the selected profile’s bound item IDs, restore during a two-day window, then permanently purge on the next unlocked scheduler run

### First launch

1. Create a separate DOWNLOWd app password (minimum 8 characters)
2. Sign in with Bitwarden (email + master password, + 2FA if enabled)
3. Enter the **shared employee passphrase** and choose the Bitwarden destination:
   **Personal Vault** for personal accounts, or an exact organization collection name.
   Missing/unavailable organization collections stop the run; they never silently fall back to a personal vault.
4. Queue HQ files, then **Run full onboarding**
5. Configure disposal / partner toggles under **Settings**

## Developer Setup

### Automated (macOS)

```bash
chmod +x setup.sh
./setup.sh
```

### Manual

1. Python 3.8+ with Tkinter (`brew install python-tk` on macOS)
2. Bitwarden CLI
3. Clone and install:

```bash
git clone https://github.com/CMRNCHN/DOWNLOWd.git
cd DOWNLOWd
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Run

```bash
source .venv/bin/activate   # or: .venv/bin/python run.py
python3 run.py
```

App-password and Bitwarden authentication happen inside the app. The Bitwarden CLI session key is kept in memory and passed to subsequent `bw` calls via `BW_SESSION`.

**Launch checklist**

1. Bitwarden CLI on PATH (`bw --version`) and vault unlockable
2. Shared employee passphrase ready (8+ chars)
3. For ⌘1–⌘6 paste into signup fields: grant **Accessibility** to Terminal/Python (or the DOWNLOWd app) in System Settings → Privacy & Security
4. Optional Selenium prefill: install `chromedriver` on PATH (or set `DOWNLOWD_CHROMEDRIVER`). Without it, signup uses system-browser handoff + the assist panel (recommended default when sites bot-block automation)

### Build installer

```bash
pip install '.[dev]'
chmod +x build.sh
./build.sh
```

## Honest limitations

- Transaction DB is **plaintext SQLite** with `chmod 600` — enable **FileVault** on macOS (the app warns at launch if FileVault is Off). Full SQLCipher encryption is future work.
- Partner signup is **assisted**, not fully automated: DOWNLOWd opens the page, prefills what it can (or hands off to the system browser on bot blocks), and shows an in-app Account assist panel with per-field Copy/Paste plus ⌘1–⌘6 hotkeys. CAPTCHA and final submit always stay with you.
- Outlook must be marked Done before Hyatt/Marriott for that employee; Skip leaves accounts pending; Retry recreates the signup attempt.
- Structured clipboard payloads (`key: value` lines) are Keysmith-ready if you want an optional overlay macro — Keysmith is not required, and there is no KeyCue integration.
- Day-20 log retention shreds **tracked** log paths and per-employee `logs/employees/<name>/` dirs; shared session logs are line-scrubbed (not whole-file deleted).
- No Microsoft Graph email provisioning in this build
