"""Isolated Chrome profile for DOWNLOWd partner-account creation.

Keeps employee signup browsing out of the operator's personal Chrome profile,
disables Chrome password/autofill (Bitwarden owns secrets), downloads privacy /
fingerprinting extensions, and registers MV3 extensions into the ops profile
via Selenium BiDi (Chrome 137+ no longer honors --load-extension on branded builds).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROFILE_ROOT = Path.home() / ".downlowd" / "chrome-ops-profile"
EXTENSIONS_ROOT = Path.home() / ".downlowd" / "chrome-ops-extensions"
BROWSERS_ROOT = Path.home() / ".downlowd" / "browsers"
SETUP_PAGE_NAME = "downlowd-ops-setup.html"

# Chrome Web Store IDs — account creation + identity / fingerprint protection.
# auto_install=False keeps a store link for MV2-only / unavailable packages.
RECOMMENDED_EXTENSIONS: Tuple[Dict[str, Any], ...] = (
    {
        "id": "nngceckbapebfimnlniiiahkandclblb",
        "name": "Bitwarden",
        "why": "Autofill the temporary DOWNLOWd signup profiles (and nothing else).",
        "category": "account_creation",
        "auto_install": True,
    },
    {
        "id": "ddkjiahejlhfcafbddmgiahcphecmpfh",
        "name": "uBlock Origin Lite",
        "why": "MV3 blocker for trackers/ads on signup pages (classic uBlock is MV2-only now).",
        "category": "identity_protection",
        "auto_install": True,
    },
    {
        "id": "lckanjgmijmafbedllaakclkaicjfmnk",
        "name": "ClearURLs",
        "why": "Strip tracking parameters from links (MV2 — install from the store if Chrome still allows it).",
        "category": "identity_protection",
        "auto_install": False,
    },
    {
        "id": "gnoaanpbfnjakaddkecnnamnfkebhgle",
        "name": "Cookie Guardian",
        "why": "Auto-delete cookies when tabs close so employee signup sessions do not linger.",
        "category": "identity_protection",
        "auto_install": True,
    },
    {
        "id": "ldpochfccmkkmhdbclfhpagapcfdljkj",
        "name": "Decentraleyes",
        "why": "Serve common CDN scripts locally so fewer third parties see signup traffic.",
        "category": "identity_protection",
        "auto_install": True,
    },
    {
        "id": "fihnjjcciajhdojfnbdddfaoknhalnja",
        "name": "I don't care about cookies",
        "why": "Dismiss cookie walls so signup forms stay usable without extra tracking consent clicks.",
        "category": "identity_protection",
        "auto_install": True,
    },
    {
        "id": "lanfdkkpgfjfdikkncbnojekcppdebfp",
        "name": "Canvas Fingerprint Defender",
        "why": "Noise canvas reads so sites cannot sticky-ID this ops browser via canvas fingerprinting.",
        "category": "fingerprinting",
        "auto_install": True,
    },
    {
        "id": "fjkmabmdepjfammlpliljpnbhleegehm",
        "name": "WebRTC Control",
        "why": "Block WebRTC IP leaks that can reveal the real network behind a VPN/proxy.",
        "category": "fingerprinting",
        "auto_install": True,
    },
    {
        "id": "bhchdcejhohfmigjafbampogmaanbfkg",
        "name": "User-Agent Switcher and Manager",
        "why": "Optional UA spoofing when a signup site keys too hard on browser/platform strings.",
        "category": "fingerprinting",
        "auto_install": True,
    },
)

_CATEGORY_LABELS = {
    "account_creation": "Account creation",
    "identity_protection": "Identity protection",
    "fingerprinting": "Fingerprinting",
}

_CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def find_chrome_binary() -> Optional[str]:
    env = os.environ.get("DOWNLOWD_CHROME_BIN") or os.environ.get("CHROME_BIN")
    if env and Path(env).exists():
        return env

    # Chrome for Testing is optional and only used when DOWNLOWD_CHROME_BIN points at it.
    # Branded Chrome still loads extensions already registered into this profile.

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
                "/usr/local/bin/google-chrome",
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


def find_chromedriver() -> Optional[str]:
    env = os.environ.get("DOWNLOWD_CHROMEDRIVER") or os.environ.get("CHROMEDRIVER")
    if env and Path(env).exists():
        return env
    marker = BROWSERS_ROOT / "chromedriver-bin"
    if marker.exists():
        path = marker.read_text(encoding="utf-8").strip()
        if path and Path(path).exists():
            return path
    for candidate in BROWSERS_ROOT.glob("chromedriver*/**/chromedriver"):
        if candidate.is_file():
            return str(candidate)
    from shutil import which

    return which("chromedriver")


def store_url(extension_id: str) -> str:
    return f"https://chromewebstore.google.com/detail/{extension_id}"


def _chrome_major_version() -> str:
    chrome = find_chrome_binary()
    if not chrome:
        return "131.0.0.0"
    try:
        proc = subprocess.run(
            [chrome, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        text = (proc.stdout or proc.stderr or "").strip()
        for token in text.replace(",", " ").split():
            if token[0].isdigit() and "." in token:
                parts = token.split(".")
                if len(parts) >= 1:
                    return f"{parts[0]}.0.0.0"
    except (OSError, subprocess.SubprocessError):
        pass
    return "131.0.0.0"


def crx_download_url(extension_id: str, *, prod_version: Optional[str] = None) -> str:
    version = prod_version or _chrome_major_version()
    return (
        "https://clients2.google.com/service/update2/crx"
        f"?response=redirect&os=linux&arch=x64&os_arch=x86_64&nacl_arch=x86-64"
        f"&prod=chromiumcrx&prodchannel=&prodversion={version}"
        f"&lang=en&acceptformat=crx2,crx3"
        f"&x=id%3D{extension_id}%26installsource%3Dondemand%26uc"
    )


def extract_crx_payload(data: bytes) -> bytes:
    """Return the ZIP bytes embedded in a CRX2/CRX3 package (or passthrough ZIP)."""
    if data[:2] == b"PK":
        return data
    if data[:4] != b"Cr24":
        raise ValueError("Not a Chrome CRX or ZIP package")
    if len(data) < 16:
        raise ValueError("Truncated CRX header")
    version = struct.unpack_from("<I", data, 4)[0]
    if version == 2:
        pubkey_len, sig_len = struct.unpack_from("<II", data, 8)
        zip_start = 16 + pubkey_len + sig_len
    elif version == 3:
        header_size = struct.unpack_from("<I", data, 8)[0]
        zip_start = 12 + header_size
    else:
        raise ValueError(f"Unsupported CRX version: {version}")
    if zip_start >= len(data) or data[zip_start : zip_start + 2] != b"PK":
        raise ValueError("CRX package missing ZIP payload")
    return data[zip_start:]


def download_extension_crx(extension_id: str, dest_crx: Path, *, timeout: float = 60.0) -> Path:
    dest_crx.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    url = crx_download_url(extension_id)
    request = urllib.request.Request(url, headers={"User-Agent": _CHROME_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Chrome Web Store returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not download extension: {exc.reason}") from exc
    if len(payload) < 64:
        raise RuntimeError("Downloaded CRX is empty or truncated")
    tmp = dest_crx.with_suffix(".tmp")
    tmp.write_bytes(payload)
    tmp.replace(dest_crx)
    return dest_crx


def unpack_crx(crx_path: Path, dest_dir: Path) -> Path:
    """Unpack a CRX into dest_dir (replaced atomically). Returns dest_dir."""
    zip_bytes = extract_crx_payload(crx_path.read_bytes())
    parent = dest_dir.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=str(parent)) as tmp:
        staging = Path(tmp) / "unpacked"
        staging.mkdir()
        zip_path = Path(tmp) / "ext.zip"
        zip_path.write_bytes(zip_bytes)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(staging)
        if not (staging / "manifest.json").exists():
            raise RuntimeError("Unpacked extension has no manifest.json")
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.move(str(staging), str(dest_dir))
    return dest_dir


def extension_install_dir(extension_id: str, *, root: Optional[Path] = None) -> Path:
    base = Path(root) if root else EXTENSIONS_ROOT
    return base / extension_id


def is_extension_unpacked(extension_id: str, *, root: Optional[Path] = None) -> bool:
    path = extension_install_dir(extension_id, root=root)
    return path.is_dir() and (path / "manifest.json").is_file()


def extension_manifest_version(extension_id: str, *, root: Optional[Path] = None) -> Optional[int]:
    path = extension_install_dir(extension_id, root=root) / "manifest.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return int(data.get("manifest_version"))
    except (TypeError, ValueError):
        return None


class ChromeOpsProfile:
    """Dedicated Chrome user-data-dir for partner signup work."""

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        extensions_root: Optional[Path] = None,
    ):
        self.root = Path(root) if root else PROFILE_ROOT
        self.extensions_root = (
            Path(extensions_root) if extensions_root else EXTENSIONS_ROOT
        )
        self.default_dir = self.root / "Default"
        self.setup_page = self.root / SETUP_PAGE_NAME

    def ensure(self, *, install_extensions: bool = True) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.extensions_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
            os.chmod(self.extensions_root, 0o700)
        except OSError:
            pass
        self.default_dir.mkdir(parents=True, exist_ok=True)
        self._write_preferences()
        if install_extensions:
            try:
                self.install_extensions(force=False)
            except Exception as exc:  # noqa: BLE001 — never block profile creation
                logging.warning("Ops extension install skipped/failed: %s", exc)
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

        webrtc = prefs.setdefault("webrtc", {})
        webrtc["ip_handling_policy"] = "disable_non_proxied_udp"

        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(prefs, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def installed_extension_paths(self) -> List[Path]:
        paths: List[Path] = []
        for ext in RECOMMENDED_EXTENSIONS:
            if not ext.get("auto_install", True):
                continue
            path = extension_install_dir(ext["id"], root=self.extensions_root)
            if path.is_dir() and (path / "manifest.json").is_file():
                paths.append(path)
        return paths

    def profile_has_registered_extensions(self) -> bool:
        """True when Preferences already lists unpacked ops extensions."""
        path = self._prefs_path()
        if not path.exists():
            return False
        try:
            prefs = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        settings = (prefs.get("extensions") or {}).get("settings") or {}
        roots = {str(p) for p in self.installed_extension_paths()}
        for value in settings.values():
            if not isinstance(value, dict):
                continue
            ext_path = str(value.get("path") or "")
            if ext_path in roots:
                return True
        return False

    def download_extensions(self, *, force: bool = False) -> Dict[str, Any]:
        """Download + unpack CRXs into the local extensions desk (no Chrome launch)."""
        self.extensions_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        results: List[Dict[str, Any]] = []
        for ext in RECOMMENDED_EXTENSIONS:
            dest = extension_install_dir(ext["id"], root=self.extensions_root)
            entry: Dict[str, Any] = {
                "id": ext["id"],
                "name": ext["name"],
                "path": str(dest),
                "auto_install": bool(ext.get("auto_install", True)),
            }
            if not ext.get("auto_install", True):
                entry["ok"] = True
                entry["detail"] = "store-only (not auto-installed)"
                results.append(entry)
                continue
            if not force and is_extension_unpacked(ext["id"], root=self.extensions_root):
                entry["ok"] = True
                entry["detail"] = "already downloaded"
                entry["manifest_version"] = extension_manifest_version(
                    ext["id"], root=self.extensions_root
                )
                results.append(entry)
                continue
            crx_path = self.extensions_root / f"{ext['id']}.crx"
            try:
                download_extension_crx(ext["id"], crx_path)
                unpack_crx(crx_path, dest)
                mv = extension_manifest_version(ext["id"], root=self.extensions_root)
                entry["ok"] = True
                entry["detail"] = "downloaded"
                entry["manifest_version"] = mv
            except Exception as exc:  # noqa: BLE001
                logging.warning("Failed to download %s (%s): %s", ext["name"], ext["id"], exc)
                entry["ok"] = False
                entry["detail"] = str(exc)
            results.append(entry)
        return {
            "ok": all(r.get("ok") for r in results if r.get("auto_install")),
            "detail": "Downloaded ops extension packages",
            "extensions": results,
        }

    def _register_extensions_via_bidi(self, paths: Sequence[Path]) -> Dict[str, Any]:
        """
        Register unpacked MV3 extensions into this profile using Selenium BiDi.

        Chrome 137+ branded builds ignore --load-extension; BiDi webExtension.install
        persists unpacked extensions into the profile Preferences.
        """
        if not paths:
            return {"ok": True, "detail": "No extensions to register", "installs": []}

        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
        except ImportError as exc:
            return {
                "ok": False,
                "detail": f"Selenium not available for BiDi install ({exc})",
                "installs": [],
            }

        chrome = find_chrome_binary()
        cft_linux = BROWSERS_ROOT / "chrome-for-testing" / "chrome-linux64" / "chrome"
        if cft_linux.exists():
            chrome = str(cft_linux)
        if not chrome:
            return {"ok": False, "detail": "Chrome not found", "installs": []}

        options = Options()
        options.binary_location = chrome
        options.add_argument(f"--user-data-dir={self.root}")
        options.add_argument("--profile-directory=Default")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-sync")
        options.add_argument("--enable-unsafe-extension-debugging")
        options.add_argument("--remote-allow-origins=*")
        options.add_argument("--disable-features=ChromeWhatsNewUI,PasswordManagerOnboarding")
        options.set_capability("webSocketUrl", True)
        for attr, value in (("enable_bidi", True), ("enable_webextensions", True)):
            if hasattr(options, attr):
                try:
                    setattr(options, attr, value)
                except Exception:
                    pass

        driver_path = find_chromedriver()
        service = Service(executable_path=driver_path) if driver_path else None

        installs: List[Dict[str, Any]] = []
        driver = None
        try:
            from concurrent.futures import ThreadPoolExecutor
            from concurrent.futures import TimeoutError as FuturesTimeout

            def _launch():
                if service is not None:
                    return webdriver.Chrome(service=service, options=options)
                return webdriver.Chrome(options=options)

            # Profile locks / Selenium Manager can hang indefinitely otherwise.
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_launch)
                try:
                    driver = future.result(timeout=45)
                except FuturesTimeout as exc:
                    future.cancel()
                    return {
                        "ok": False,
                        "detail": (
                            "BiDi Chrome launch timed out after 45s — close Ops Chrome "
                            "and retry Install / refresh extensions."
                        ),
                        "installs": installs,
                    }

            for path in paths:
                entry: Dict[str, Any] = {"path": str(path), "name": path.name}
                try:
                    if hasattr(driver, "webextension"):
                        result = driver.webextension.install(str(path))
                    else:
                        from selenium.webdriver.common.bidi.webextension import WebExtension

                        result = WebExtension(driver).install(path=str(path))
                    entry["ok"] = True
                    entry["result"] = result if isinstance(result, (str, dict)) else repr(result)
                except Exception as exc:  # noqa: BLE001
                    entry["ok"] = False
                    entry["detail"] = str(exc)
                installs.append(entry)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "detail": f"BiDi Chrome launch failed: {exc}",
                "installs": installs,
            }
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

        ok = all(item.get("ok") for item in installs) if installs else False
        return {
            "ok": ok,
            "detail": (
                f"Registered {sum(1 for i in installs if i.get('ok'))}/{len(installs)} "
                "extensions into ops profile via BiDi"
            ),
            "installs": installs,
        }

    def install_extensions(self, *, force: bool = False) -> Dict[str, Any]:
        """
        Download recommended CRXs and register MV3 ones into the ops Chrome profile.

        Returns a status dict with per-extension ok/error and load paths.
        """
        downloaded = self.download_extensions(force=force)
        results = list(downloaded.get("extensions") or [])

        need_register = force or not self.profile_has_registered_extensions()
        register_paths: List[Path] = []
        if need_register:
            for ext in RECOMMENDED_EXTENSIONS:
                if not ext.get("auto_install", True):
                    continue
                path = extension_install_dir(ext["id"], root=self.extensions_root)
                if not (path / "manifest.json").exists():
                    continue
                mv = extension_manifest_version(ext["id"], root=self.extensions_root)
                if mv == 2:
                    for row in results:
                        if row.get("id") == ext["id"]:
                            row["detail"] = "MV2 — skipped BiDi register; use Chrome Web Store"
                    continue
                register_paths.append(path)

        bidi: Dict[str, Any] = {"ok": True, "detail": "already registered", "installs": []}
        if register_paths:
            # Chrome must not already have this profile open.
            bidi = self._register_extensions_via_bidi(register_paths)
            by_path = {str(Path(i.get("path"))): i for i in bidi.get("installs") or []}
            for row in results:
                if not row.get("auto_install"):
                    continue
                info = by_path.get(row.get("path"))
                if not info:
                    continue
                if info.get("ok"):
                    row["registered"] = True
                    row["detail"] = "registered in ops profile"
                else:
                    row["registered"] = False
                    row["detail"] = info.get("detail") or "BiDi register failed"

        installed_ok = [
            r for r in results if r.get("auto_install") and r.get("ok") and r.get("registered", True)
        ]
        failed = [
            r
            for r in results
            if r.get("auto_install")
            and (not r.get("ok") or r.get("registered") is False)
        ]
        summary = (
            f"Ops extensions ready: {len(installed_ok)} auto / "
            f"{sum(1 for r in results if not r.get('auto_install'))} store-only"
            + (f" ({len(failed)} failed)" if failed else "")
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._write_setup_page(install_results=results)
        except OSError:
            pass

        marker = self.root / ".extensions_registered"
        if bidi.get("ok") or self.profile_has_registered_extensions():
            marker.write_text("1\n", encoding="utf-8")

        return {
            "ok": len(failed) == 0 and bool(downloaded.get("ok")),
            "detail": summary,
            "extensions": results,
            "bidi": bidi,
            "load_paths": [str(p) for p in self.installed_extension_paths()],
            "extensions_root": str(self.extensions_root),
            "registered": self.profile_has_registered_extensions(),
        }

    def _write_setup_page(self, install_results: Optional[Sequence[Dict[str, Any]]] = None) -> None:
        status_by_id: Dict[str, Dict[str, Any]] = {}
        if install_results:
            for row in install_results:
                status_by_id[str(row.get("id"))] = row
        registered = self.profile_has_registered_extensions()
        rows = []
        for ext in RECOMMENDED_EXTENSIONS:
            badge = _CATEGORY_LABELS.get(ext["category"], ext["category"])
            local = is_extension_unpacked(ext["id"], root=self.extensions_root)
            status = status_by_id.get(ext["id"])
            auto = bool(ext.get("auto_install", True))
            if not auto:
                state = "Store install only (MV2 / not auto-registered)"
                state_class = "muted"
            elif status and status.get("registered") is False:
                state = f"Download ok — register failed ({status.get('detail', 'error')})"
                state_class = "warn"
            elif status and not status.get("ok"):
                state = f"Auto-install failed — use store link ({status.get('detail', 'error')})"
                state_class = "warn"
            elif registered and local:
                state = "Installed in Ops Chrome profile"
                state_class = "ok"
            elif local:
                state = "Downloaded — click Install / refresh extensions in Settings to register"
                state_class = "muted"
            else:
                state = "Not installed yet — click Install / refresh extensions in Settings"
                state_class = "muted"
            rows.append(
                f"""
<article class="card">
  <div class="badge">{badge}</div>
  <h2>{ext['name']}</h2>
  <p>{ext['why']}</p>
  <p class="state {state_class}">{state}</p>
  <a class="btn" href="{store_url(ext['id'])}" target="_blank" rel="noreferrer">
    Chrome Web Store
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
      --ok: #2f6b45; --warn: #8a5a12;
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
    .state {{ font-size: 12px; margin: -6px 0 12px; }}
    .state.ok {{ color: var(--ok); }}
    .state.warn {{ color: var(--warn); }}
    .state.muted {{ color: var(--muted); }}
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
      Keep it signed out of your personal Google account. DOWNLOWd downloads
      uBlock Origin Lite, fingerprint defenders, and Bitwarden, then registers
      them into this profile (Chrome 137+ no longer loads unpacked extensions
      from the command line on branded builds).
    </p>
    <div class="grid">
      {''.join(rows)}
    </div>
    <div class="rules">
      <p><strong>Identity rules for this profile</strong></p>
      <ul>
        <li>Do not sync a personal Google account here.</li>
        <li>Chrome password manager and autofill stay off — Bitwarden owns secrets.</li>
        <li>uBlock Origin Lite + Decentraleyes cut tracker noise on signup pages.</li>
        <li>Canvas / WebRTC defenders reduce sticky browser fingerprints.</li>
        <li>Close Chrome before Settings → Install / refresh extensions.</li>
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

    def load_extension_arg(self) -> Optional[str]:
        """Legacy flag for Chromium / older Chrome; branded 137+ ignores it."""
        paths = self.installed_extension_paths()
        if not paths:
            return None
        return "--load-extension=" + ",".join(str(p) for p in paths)

    def launch_args(
        self,
        *urls: str,
        extra_args: Optional[Sequence[str]] = None,
        new_window: bool = True,
        install_extensions: bool = True,
    ) -> List[str]:
        chrome = find_chrome_binary()
        if not chrome:
            raise RuntimeError("Google Chrome / Chromium not found.")
        self.ensure(install_extensions=install_extensions)
        args = [
            chrome,
            f"--user-data-dir={self.root}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            # Keep the old switch re-enabled on Chrome 137–141; ignored later.
            "--disable-features=DisableLoadExtensionCommandLineSwitch,ChromeWhatsNewUI,PasswordManagerOnboarding",
        ]
        load_arg = self.load_extension_arg()
        if load_arg:
            args.append(load_arg)
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
                "extensions_registered": self.profile_has_registered_extensions(),
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
        kwargs: Dict[str, Any] = {
            "user_data_dir": str(self.root),
            "profile_directory": "Default",
            "binary": find_chrome_binary(),
        }
        load_arg = self.load_extension_arg()
        if load_arg:
            kwargs["load_extension_arg"] = load_arg
        return kwargs

    def clear_site_data(self) -> Dict[str, Any]:
        """
        Best-effort wipe of cookies/local storage for a clean next employee.

        Leaves installed extensions and profile prefs intact. Close Chrome first.
        """
        self.ensure(install_extensions=False)
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
