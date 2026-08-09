# AGENTS.md

## Cursor Cloud specific instructions

DOWNLOWd is a **single desktop GUI application** (Python + Tkinter/`customtkinter`) that automates
employee onboarding: it reads `HQ-*.txt/.rtf` files from `~/Downloads`, converts them to Bitwarden
import JSON, imports them into a Bitwarden vault, and (optionally) assists with partner signups. It is
written for macOS but has cross-platform fallbacks and runs on this Linux VM under the X display `:1`.

There is only one service. Standard commands (setup/run/build) live in `README.md`; notes below are the
non-obvious caveats for running it here.

### Environment / running
- The update script creates `.venv` and runs `pip install -e .`. The system package **`python3-tk`**
  (Tkinter) is required and is baked into the VM image — it is a system dep, not a pip dep, so it is not
  in the update script. If `import tkinter` fails, reinstall it with `sudo apt-get install -y python3-tk`.
- Run the GUI with a display: `DISPLAY=:1 .venv/bin/python run.py`. Without `DISPLAY` it cannot open a window.
- `keyring` has **no working backend** on this headless Linux (no Secret Service / gnome-keyring). The app
  tolerates this (it catches `KeyringError`), but settings such as the shared passphrase, remembered
  Bitwarden email, and vault-collection name **do not persist across launches**. Re-enter them in
  **Settings** during each session.

### Tests / lint / build
- Tests: `.venv/bin/python -m unittest discover -s tests -v` (41 tests, all mocked — no Bitwarden or
  display needed). They run in well under a second.
- Lint: no linter is configured in this repo. Use `.venv/bin/python -m py_compile *.py tests/*.py` as a
  syntax smoke check.
- Build: `build.sh` produces a macOS `.app`/`.pkg` via PyInstaller + `pkgbuild` and **only works on macOS**;
  it cannot be run on this Linux VM.

### Running the full GUI end-to-end (reaching the dashboard)
- The entire app is gated behind a **Bitwarden CLI login/unlock**. The `bw` CLI must be on `PATH` and a
  reachable Bitwarden (or compatible) server + account is required to get past the login screen.
- To exercise the app without any real Bitwarden credentials, run a local **Vaultwarden** server (Bitwarden-
  compatible). The `bw` CLI (2026.x) **requires HTTPS**, so serve Vaultwarden with TLS (e.g. `ROCKET_TLS`
  with a self-signed cert) and point the launching shell at the cert via `NODE_EXTRA_CA_CERTS`. Configure
  the CLI with `bw config server https://<host>` and create the account, then log in through the GUI with
  the same email/master password.
- Partner provisioning (**Create Outlook / Hyatt / Marriott**) defaults to **ON** and, when the pipeline
  reaches the accounts stage, drives Selenium / system-browser handoff and shows an in-app assist panel
  that **blocks the pipeline** waiting for interaction. For a headless "convert → import into vault only"
  run, turn all three OFF in Settings first.
- HQ intake/output directory is `~/Downloads`; the **Manual** employee form writes an `HQ-*.txt` there,
  and **Run**/`Save & Run` imports it into the configured vault collection (default `Personal Vault`).
