# DOWNLOWD - Employee Onboarding Appliance

An automated desktop tool for streamlining new employee onboarding tasks. The application monitors the Downloads folder for specific employee data files, converts them for Bitwarden import, optionally opens partner signup pages, and includes local transaction logging for company card expenses.

## Security Features

- **Bitwarden is the gate** — unlock the app with your Bitwarden email + master password (no separate app password)
- **macOS Keychain** — stores settings and remembered Bitwarden email
- **Transaction Logging** — local SQLite with owner-only (`0o600`) permissions — **not encrypted at rest** (FileVault recommended)
- **Local disposal modes** — standard unlink, overwrite-then-delete, or best-effort secure erase (APFS/SSD: FileVault is the real protection)
- **Automated Data Retention** — 5/10/15/20 day lifecycle
- **Audit logging** — auth, imports, transactions, retention, config

See [SECURITY_FEATURES.md](SECURITY_FEATURES.md) for details.

## Onboarding Workflow

1. **Sign in** with Bitwarden
2. **Intake** — drop/browse `HQ-*.txt` / `HQ-*.rtf` into the queue
3. **Shared passphrase** — one passphrase for every new employee login (they change it later)
4. **Run full onboarding** — convert → Bitwarden import → Outlook/Hyatt/Marriott autofill → dispose local files
5. **Usernames** — `firstnamelastnameYEAR` (birth year)

### First launch

1. Sign in with Bitwarden (email + master password, + 2FA if enabled)
2. Enter the **shared employee passphrase** and confirm the Bitwarden **collection**
3. Queue HQ files, then **Run full onboarding**
4. Configure disposal / partner toggles under **Settings**

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
python3 run.py
```

Unlock/login happens inside the app; the CLI session key is kept in memory and passed to subsequent `bw` calls via `BW_SESSION`.

### Build installer

```bash
pip install '.[dev]'
chmod +x build.sh
./build.sh
```

## Honest limitations

- Transaction DB is **plaintext SQLite** with `chmod 600` — enable **FileVault** on macOS (the app warns at launch if FileVault is Off). Full SQLCipher encryption is future work.
- Partner provisioning: Outlook uses clipboard + browser handoff; Hyatt/Marriott attempt Selenium form prefill (Chrome) then leave the window open for captcha/submit.
- Day-20 log retention shreds **tracked** log paths and per-employee `logs/employees/<name>/` dirs; shared session logs are line-scrubbed (not whole-file deleted).
- Partner provisioning is **browser handoff**, not fully automated signup
- No Microsoft Graph email provisioning in this build
