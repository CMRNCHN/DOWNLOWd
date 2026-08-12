"""Isolated Chrome profile for DOWNLOWd partner-account creation.

Keeps employee signup browsing out of the operator's personal Chrome profile,
disables Chrome password/autofill (Bitwarden owns secrets), and opens a setup
desk for privacy + account-creation extensions.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROFILE_ROOT = Path.home() / ".downlowd" / "chrome-ops-profile"
SETUP_PAGE_NAME = "downlowd-ops-setup.html"

# Well-known Chrome Web Store IDs — privacy + account-creation helpers.
RECOMMENDED_EXTENSIONS: Tuple[Dict[str, str], ...] = (
    {
        "id": "nngceckbapebfimnlniiiahkandclblb",
        "name": "Bitwarden",
        "why": "Autofill the temporary DOWNLOWd signup profiles (and nothing else).",
        "category": "account_creation",
    },
    {
        "id": "cjpalhdlnbpafiamejdnhcphjbkeiagm",
        "name": "uBlock Origin",
        "why": "Block trackers/ads on signup pages so less of the session leaks out.",
        "category": "identity_protection",
    },
    {
        "id": "lckanjgmijmafbedllaakclkaicjfmnk",
        "name": "ClearURLs",
        "why": "Strip tracking parameters from links before they hitch a ride on signup URLs.",
        "category": "identity_protection",
    },
    {
        "id": "pkehgijcmpdhfbdbbnkilgnmqnhapjdo",
        "name": "Privacy Badger",
        "why": "Learn and block invisible third-party trackers during enroll flows.",
        "category": "identity_protection",
    },
    {
        "id": "gnoaanpbfnjakaddkecnnamnfkebhgle",
        "name": "Cookie Guardian",
        "why": "Auto-delete cookies when tabs close so employee signup sessions do not linger.",
        "category": "identity_protection",
    },
)


def find_chrome_binary() -> Optional[str]:
    env = os.environ.get("DOWNLOWD_CHROME_BIN") or os.environ.get("CHROME_BIN")
    if env and Path(env).exists():
        return env
    candidates = []
    if sys.platform == "darwin":
        candidates.append(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )
        candidates.append(
            "/Applications/Chromium.app/Contents/MacOS/Chromium"
        )
    elif sys.platform.startswith("linux"):
        candidates.extend(
            [
                "/usr/bin/google-chrome-stable",
                "/usr/bin/google-chrome",
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
                "/snap/bin/chromium",
            ]
        )
    elif sys.platform.startswith("win"):
        candidates.extend(
            [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]
        )
    from shutil import which

    for path in candidates:
        if Path(path).exists():
            return path
    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        found = which(name)
        if found:
            return found
    return None


def store_url(extension_id: str) -> str:
    return f"https://chromewebstore.google.com/detail/{extension_id}"


class ChromeOpsProfile:
    """Dedicated Chrome user-data-dir for partner signup work."""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else PROFILE_ROOT
        self.default_dir = self.root / "Default"
        self.setup_page = self.root / SETUP_PAGE_NAME

    def ensure(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.default_dir.mkdir(parents=True, exist_ok=True)
        self._write_preferences()
        self._write_setup_page()
        self._write_first_run_sentinel()
        return self.root

    def _prefs_path(self) -> Path:
        return self.default_dir / "Preferences"

    def _write_preferences(self) -> None:
        """Privacy-leaning defaults: no Chrome password vault, no Google autofill."""
        prefs: Dict[str, Any] = {}
        path = self._prefs_path()
        if path.exists():
            try:
                prefs = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(prefs, dict):
                    prefs = {}
            except (json.JSONDecodeError, OSError):
                prefs = {}

        profile = prefs.setdefault("profile", {})
        profile["name"] = "DOWNLOWd Ops"
        profile["password_manager_enabled"] = False
        # Discourage signing this profile into a personal Google account.
        profile["exit_type"] = "Normal"

        prefs["credentials_enable_service"] = False
        prefs["credentials_enable_autosignin"] = False
        autofill = prefs.setdefault("autofill", {})
        autofill["profile_enabled"] = False
        autofill["credit_card_enabled"] = False
        signin = prefs.setdefault("signin", {})
        signin["allowed"] = False
        browser = prefs.setdefault("browser", {})
        browser["has_seen_welcome_page"] = True
        session = prefs.setdefault("session", {})
        session["restore_on_startup"] = 5  # Open New Tab Page

        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(prefs, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _write_setup_page(self) -> None:
        rows = []
        for ext in RECOMMENDED_EXTENSIONS:
            badge = (
                "Account creation"
                if ext["category"] == "account_creation"
                else "Identity protection"
            )
            rows.append(
                f"""
<article class="card">
  <div class="badge">{badge}</div>
  <h2>{ext['name']}</h2>
  <p>{ext['why']}</p>
  <a class="btn" href="{store_url(ext['id'])}" target="_blank" rel="noreferrer">
    Install {ext['name']}
  </a>
</article>
"""
            )
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>DOWNLOWd Ops Chrome — extension desk</title>
  <style>
    :root {{
      --bg: #e4e8e2; --card: #f7f8f5; --ink: #1a1f1a;
      --muted: #667066; --accent: #3a5f48; --border: #c5cdc0;
    }}
    body {{
      margin: 0; font-family: "Avenir Next", "Segoe UI", sans-serif;
      background: linear-gradient(160deg, #e4e8e2, #d5dbd3 55%, #eef1ec);
      color: var(--ink); padding: 40px 24px 64px;
    }}
    main {{ max-width: 880px; margin: 0 auto; }}
    h1 {{ font-size: 28px; margin: 0 0 8px; }}
    .lede {{ color: var(--muted); max-width: 60ch; line-height: 1.45; }}
    .grid {{
      display: grid; gap: 16px;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      margin-top: 28px;
    }}
    .card {{
      background: var(--card); border-radius: 16px; padding: 18px 18px 20px;
      box-shadow: 0 1px 0 rgba(26,31,26,.06);
    }}
    .badge {{
      display: inline-block; font-size: 11px; letter-spacing: .04em;
      text-transform: uppercase; color: var(--accent); margin-bottom: 8px;
    }}
    h2 {{ margin: 0 0 8px; font-size: 18px; }}
    p {{ margin: 0 0 14px; color: var(--muted); line-height: 1.4; font-size: 14px; }}
    .btn {{
      display: inline-block; background: var(--accent); color: #f7f8f5;
      text-decoration: none; border-radius: 10px; padding: 10px 14px;
      font-size: 13px; font-weight: 600;
    }}
    .rules {{
      margin-top: 28px; background: var(--card); border-radius: 16px;
      padding: 18px 20px; color: var(--muted); line-height: 1.5; font-size: 14px;
    }}
    .rules strong {{ color: var(--ink); }}
  </style>
</head>
<body>
  <main>
    <h1>DOWNLOWd Ops Chrome</h1>
    <p class="lede">
      This browser profile is only for partner account creation.
      Keep it signed out of your personal Google account. Install the
      extensions below, then use Bitwarden Auto-fill for temporary signup items.
    </p>
    <div class="grid">
      {''.join(rows)}
    </div>
    <div class="rules">
      <p><strong>Identity rules for this profile</strong></p>
      <ul>
        <li>Do not sync a personal Google account here.</li>
        <li>Chrome password manager and autofill stay off — Bitwarden owns secrets.</li>
        <li>Use Cookie Guardian so employee signup cookies do not linger.</li>
        <li>After each employee, close extra tabs; DOWNLOWd can also reset site data.</li>
      </ul>
    </div>
  </main>
</body>
</html>
"""
        self.setup_page.write_text(html, encoding="utf-8")

    def _write_first_run_sentinel(self) -> None:
        marker = self.root / ".downlowd_ops_ready"
        if not marker.exists():
            marker.write_text("ready\n", encoding="utf-8")

    def needs_extension_setup(self) -> bool:
        """True until the operator opens the setup desk at least once."""
        return not (self.root / ".extensions_setup_opened").exists()

    def mark_extension_setup_opened(self) -> None:
        (self.root / ".extensions_setup_opened").write_text("1\n", encoding="utf-8")

    def launch_args(
        self,
        *urls: str,
        extra_args: Optional[Sequence[str]] = None,
        new_window: bool = True,
    ) -> List[str]:
        chrome = find_chrome_binary()
        if not chrome:
            raise RuntimeError("Google Chrome / Chromium not found.")
        self.ensure()
        args = [
            chrome,
            f"--user-data-dir={self.root}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-features=ChromeWhatsNewUI,PasswordManagerOnboarding",
        ]
        if new_window:
            args.append("--new-window")
        if extra_args:
            args.extend(extra_args)
        args.extend(urls)
        return args

    def open_urls(self, urls: Sequence[str], *, setup_if_needed: bool = True) -> Dict[str, Any]:
        self.ensure()
        open_list = list(urls)
        if setup_if_needed and self.needs_extension_setup():
            open_list.insert(0, self.setup_page.as_uri())
            self.mark_extension_setup_opened()
        try:
            args = self.launch_args(*open_list)
        except RuntimeError as exc:
            return {"ok": False, "detail": str(exc)}
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {
                "ok": True,
                "detail": f"Opened ops Chrome ({len(open_list)} tab(s)).",
                "profile": str(self.root),
                "args": args,
            }
        except OSError as exc:
            return {"ok": False, "detail": str(exc)}

    def open_setup_desk(self) -> Dict[str, Any]:
        self.ensure()
        self.mark_extension_setup_opened()
        return self.open_urls([self.setup_page.as_uri()], setup_if_needed=False)

    def open_empty(self) -> Dict[str, Any]:
        return self.open_urls([], setup_if_needed=True)

    def selenium_options_kwargs(self) -> Dict[str, Any]:
        """Args to feed Chrome Options for AccountCreator."""
        self.ensure()
        return {
            "user_data_dir": str(self.root),
            "profile_directory": "Default",
            "binary": find_chrome_binary(),
        }

    def clear_site_data(self) -> Dict[str, Any]:
        """
        Best-effort wipe of cookies/local storage for a clean next employee.

        Leaves installed extensions and profile prefs intact. Close Chrome first.
        """
        self.ensure()
        removed: List[str] = []
        targets = [
            self.default_dir / "Cookies",
            self.default_dir / "Cookies-journal",
            self.default_dir / "Network" / "Cookies",
            self.default_dir / "Network" / "Cookies-journal",
            self.default_dir / "Local Storage",
            self.default_dir / "Session Storage",
            self.default_dir / "Service Worker",
            self.default_dir / "Cache",
            self.default_dir / "Code Cache",
            self.default_dir / "GPUCache",
            self.default_dir / "History",
            self.default_dir / "History-journal",
            self.default_dir / "Visited Links",
            self.default_dir / "Web Data",
            self.default_dir / "Web Data-journal",
        ]
        for path in targets:
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                    removed.append(str(path.name))
                elif path.exists():
                    path.unlink()
                    removed.append(str(path.name))
            except OSError as exc:
                logging.warning("Could not clear %s: %s", path, exc)
        return {"ok": True, "removed": removed, "profile": str(self.root)}


def open_ops_browser(url: str, *, setup_if_needed: bool = False) -> Dict[str, Any]:
    """Public helper used by account handoff."""
    return ChromeOpsProfile().open_urls([url], setup_if_needed=setup_if_needed)
