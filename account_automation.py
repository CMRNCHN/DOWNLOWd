"""
Assisted partner signup: best-effort Selenium prefill, structured clipboard
payload, and macOS paste helpers for an in-app field palette (Keysmith-ready).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Any, Dict, List, Optional, Tuple

from chrome_ops_profile import ChromeOpsProfile, find_chrome_binary, open_ops_browser

try:
    from selenium import webdriver
    from selenium.common.exceptions import NoSuchElementException, WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    _SELENIUM_AVAILABLE = True
except ImportError:
    _SELENIUM_AVAILABLE = False

    class By:  # type: ignore[no-redef]
        ID = "id"
        NAME = "name"
        CSS_SELECTOR = "css selector"
        XPATH = "xpath"


# Hotkey contract (⌘1…⌘6) — order is stable for Keysmith macros.
ASSIST_FIELD_KEYS: Tuple[str, ...] = (
    "first_name",
    "last_name",
    "email",
    "password",
    "confirm_password",
    "postal",
)

ASSIST_FIELD_LABELS: Dict[str, str] = {
    "first_name": "First",
    "last_name": "Last",
    "email": "Email",
    "password": "Password",
    "confirm_password": "Confirm",
    "postal": "Zip",
    "username": "Username",
}

_BOT_MARKERS = (
    "e6020",
    "access denied",
    "unusual traffic",
    "bot detection",
    "please verify you are a human",
    "attention required",
    "cf-browser-verification",
    "akamai",
    "your browser did something unexpected",
)


def copy_to_clipboard(text: str) -> bool:
    """Best-effort clipboard copy (macOS pbcopy / Linux xclip)."""
    try:
        if sys.platform == "darwin":
            proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
            return proc.returncode == 0
        proc = subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=text.encode("utf-8"),
            check=True,
        )
        return proc.returncode == 0
    except Exception:
        return False


# Back-compat alias.
_copy_to_clipboard = copy_to_clipboard


def normalize_personal_data(personal_data: Dict[str, str]) -> Dict[str, str]:
    """Fill first/last/confirm/postal aliases used by fill maps and the palette."""
    data = {str(k): str(v) if v is not None else "" for k, v in personal_data.items()}
    full = data.get("full_name", "").strip()
    first = data.get("first_name", "").strip()
    last = data.get("last_name", "").strip()
    if not first and full:
        first = full.split(" ", 1)[0]
    if not last and full and " " in full:
        last = full.split(" ", 1)[1]
    postal = (
        data.get("postal")
        or data.get("postal_code")
        or data.get("zip")
        or data.get("zip_code")
        or ""
    ).strip()
    password = data.get("password", "")
    email = data.get("email") or data.get("username") or ""
    username = data.get("username") or email
    return {
        **data,
        "first_name": first,
        "last_name": last,
        "email": email,
        "username": username,
        "password": password,
        "confirm_password": data.get("confirm_password") or password,
        "postal": postal,
        "country": data.get("country") or data.get("country_region") or "USA",
    }


def format_assist_payload(
    personal_data: Dict[str, str],
    account_name: str,
    *,
    service: str = "",
) -> str:
    """
    Structured clipboard payload (Keysmith-ready).

    One `key: value` line per field. Macros and humans can parse the same text.
    """
    data = normalize_personal_data(personal_data)
    lines = [
        f"service: {service}" if service else None,
        f"account_name: {account_name}",
        f"full_name: {data.get('full_name', '')}",
        f"first_name: {data.get('first_name', '')}",
        f"last_name: {data.get('last_name', '')}",
        f"email: {data.get('email', '')}",
        f"username: {data.get('username', '')}",
        f"password: {data.get('password', '')}",
        f"confirm_password: {data.get('confirm_password', '')}",
        f"postal: {data.get('postal', '')}",
        f"country: {data.get('country', '')}",
    ]
    return "\n".join(line for line in lines if line is not None)


def assist_field_value(personal_data: Dict[str, str], field_key: str) -> str:
    data = normalize_personal_data(personal_data)
    if field_key == "email":
        return data.get("email") or data.get("username") or ""
    return data.get(field_key, "")


def paste_field_value(value: str) -> bool:
    """
    Copy `value` to the clipboard and Cmd+V into the frontmost app (macOS).

    Requires Accessibility permission for DOWNLOWd / Terminal / Python.
    """
    if not value:
        return False
    if not _copy_to_clipboard(value):
        return False
    if sys.platform != "darwin":
        return True
    script = (
        'tell application "System Events" to keystroke "v" using command down'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        return proc.returncode == 0
    except Exception:
        return False


def arrange_windows_for_assist(
    *,
    app_title: str = "DOWNLOWd",
    browser_apps: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Best-effort side-by-side layout: DOWNLOWd left, browser right (macOS).

    Embedding a live browser inside Tk is not supported; this keeps both
    visible so the operator can watch signup while using the field companion.
    """
    result: Dict[str, Any] = {"ok": False, "platform": sys.platform, "detail": ""}
    if sys.platform != "darwin":
        result["detail"] = "Side-by-side arrange is implemented for macOS only."
        return result
    browsers = browser_apps or ["Google Chrome", "Safari", "Microsoft Edge", "Arc"]
    browser_list = ", ".join(f'"{name}"' for name in browsers)
    script = f'''
tell application "System Events"
  set screenW to item 1 of (size of (first window of process "Finder" whose role description is "window"))
end tell
try
  tell application "Finder"
    set screenBounds to bounds of window of desktop
    set screenW to item 3 of screenBounds
    set screenH to item 4 of screenBounds
  end tell
on error
  set screenW to 1440
  set screenH to 900
end try
set gap to 12
set leftW to (screenW * 0.38) as integer
set rightX to leftW + gap
set rightW to screenW - rightX - gap
try
  tell application "{app_title}" to activate
end try
tell application "System Events"
  if exists (process "{app_title}") then
    tell process "{app_title}"
      set frontmost to true
      try
        set position of front window to {{gap, 40}}
        set size of front window to {{leftW, screenH - 80}}
      end try
    end tell
  end if
end tell
set browserName to ""
repeat with candidate in {{{browser_list}}}
  tell application "System Events"
    if exists (process (candidate as text)) then
      set browserName to candidate as text
      exit repeat
    end if
  end tell
end repeat
if browserName is "" then
  return "no_browser"
end if
tell application "System Events"
  tell process browserName
    set frontmost to true
    try
      set position of front window to {{rightX, 40}}
      set size of front window to {{rightW, screenH - 80}}
    end try
  end tell
end tell
return browserName
'''
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        out = (proc.stdout or "").strip()
        if proc.returncode != 0:
            result["detail"] = (proc.stderr or out or "osascript failed").strip()
            return result
        if out == "no_browser":
            result["detail"] = "No Chrome/Safari/Edge window open yet."
            return result
        result["ok"] = True
        result["browser"] = out
        result["detail"] = f"Arranged {app_title} left, {out} right."
        return result
    except Exception as exc:
        result["detail"] = str(exc)
        return result


def parse_confirmation(result: Any) -> str:
    """Normalize callback returns to done | skip | retry."""
    if result is True:
        return "done"
    if result is False or result is None:
        return "skip"
    text = str(result).strip().lower()
    if text in {"done", "yes", "true", "created", "confirm"}:
        return "done"
    if text in {"retry", "again"}:
        return "retry"
    return "skip"


def _chromedriver_on_path() -> bool:
    """True when a usable chromedriver binary is already available."""
    if os.environ.get("DOWNLOWD_CHROMEDRIVER"):
        return os.path.exists(os.environ["DOWNLOWD_CHROMEDRIVER"])
    from shutil import which

    return which("chromedriver") is not None


class AccountCreator:
    """Prefills partner signup forms via Selenium; falls back to browser handoff."""

    def __init__(self, headless: bool = False, *, prefer_system_browser: Optional[bool] = None):
        self.headless = headless
        # Prefer system browser when chromedriver isn't installed — avoids Selenium Manager hangs.
        if prefer_system_browser is None:
            prefer_system_browser = not _chromedriver_on_path()
        self.prefer_system_browser = prefer_system_browser
        self._driver = None
        self._selenium_disabled = False
        self.last_payload: str = ""
        self.last_personal_data: Dict[str, str] = {}

    def _build_chrome_options(self) -> "Options":
        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--window-size=1280,900")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        # Dedicated ops profile (extensions + no personal Google sync).
        # Skip --incognito so Bitwarden / privacy extensions from this profile load.
        ops = ChromeOpsProfile()
        ops.ensure()
        options.add_argument(f"--user-data-dir={ops.root}")
        options.add_argument("--profile-directory=Default")
        options.add_argument("--no-first-run")
        options.add_argument("--disable-sync")
        load_arg = ops.load_extension_arg()
        if load_arg:
            options.add_argument(load_arg)
            options.add_argument(
                "--disable-extensions-except=" + load_arg.split("=", 1)[1]
            )
        chrome_bin = find_chrome_binary()
        if chrome_bin:
            options.binary_location = chrome_bin
        return options

    def _get_browser(self):
        if self._driver is not None:
            try:
                _ = self._driver.current_url
                return self._driver
            except Exception:
                self._driver = None

        options = self._build_chrome_options()
        service = None
        driver_path = os.environ.get("DOWNLOWD_CHROMEDRIVER") or ""
        if driver_path and os.path.exists(driver_path):
            from selenium.webdriver.chrome.service import Service

            service = Service(executable_path=driver_path)

        def launch():
            if service is not None:
                return webdriver.Chrome(service=service, options=options)
            return webdriver.Chrome(options=options)

        # Selenium Manager can hang when chromedriver is missing/offline.
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(launch)
            try:
                self._driver = future.result(timeout=20)
            except FuturesTimeout as exc:
                future.cancel()
                self._selenium_disabled = True
                raise WebDriverException(
                    "Chrome driver startup timed out after 20s"
                ) from exc
        return self._driver

    @staticmethod
    def _focus_form_context(driver) -> bool:
        """Select the top-level document or first iframe containing visible inputs."""
        driver.switch_to.default_content()
        if any(element.is_displayed() for element in driver.find_elements(By.CSS_SELECTOR, "input")):
            return True
        for frame in driver.find_elements(By.CSS_SELECTOR, "iframe"):
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(frame)
                if any(
                    element.is_displayed()
                    for element in driver.find_elements(By.CSS_SELECTOR, "input")
                ):
                    return True
            except WebDriverException:
                continue
        driver.switch_to.default_content()
        return False

    @staticmethod
    def _page_looks_blocked(driver) -> bool:
        try:
            source = (driver.page_source or "").lower()
        except WebDriverException:
            return True
        if any(marker in source for marker in _BOT_MARKERS):
            return True
        title = ""
        try:
            title = (driver.title or "").lower()
        except WebDriverException:
            return True
        if "access denied" in title or "denied" == title.strip():
            return True
        return False

    def _fill_first_match(self, driver, selectors: List[Tuple[str, str]], value: str) -> bool:
        if not value:
            return False
        for by, sel in selectors:
            try:
                elements = driver.find_elements(by, sel)
                if not elements:
                    continue
                el = next((item for item in elements if item.is_displayed()), elements[0])
                tag = (el.tag_name or "").lower()
                if tag == "select":
                    # Prefer visible option text / value match for country/region selects.
                    options = el.find_elements(By.CSS_SELECTOR, "option")
                    target = value.strip().casefold()
                    for option in options:
                        opt_value = (option.get_attribute("value") or "").strip()
                        opt_text = (option.text or "").strip()
                        if target in {
                            opt_value.casefold(),
                            opt_text.casefold(),
                        } or target in opt_text.casefold():
                            option.click()
                            return True
                    el.send_keys(value)
                    return True
                el.clear()
                el.send_keys(value)
                return True
            except (NoSuchElementException, WebDriverException):
                continue
        return False

    def close_browser(self) -> None:
        if self._driver is None:
            return
        try:
            self._driver.quit()
        except Exception:
            pass
        finally:
            self._driver = None

    def reset_browser_session(self) -> None:
        """Clear the completed signup's browser state before the next employee."""
        if self._driver is None:
            return
        try:
            self._driver.delete_all_cookies()
            self._driver.execute_script(
                "window.localStorage.clear(); window.sessionStorage.clear();"
            )
            self._driver.get("about:blank")
        except WebDriverException:
            self.close_browser()

    def _prepare_payload(
        self,
        service: str,
        personal_data: Dict[str, str],
        account_name: str,
    ) -> Tuple[Dict[str, str], str, bool]:
        data = normalize_personal_data(personal_data)
        payload = format_assist_payload(data, account_name, service=service)
        self.last_personal_data = data
        self.last_payload = payload
        return data, payload, _copy_to_clipboard(payload)

    def _handoff(
        self,
        service: str,
        signup_url: str,
        personal_data: Dict[str, str],
        account_name: str,
        *,
        status: str = "manual_only",
        message: Optional[str] = None,
    ) -> Dict[str, Any]:
        data, payload, copied = self._prepare_payload(service, personal_data, account_name)
        launched = open_ops_browser(signup_url, setup_if_needed=True)
        if not launched.get("ok"):
            try:
                webbrowser.open(signup_url)
                launched = {
                    "ok": True,
                    "detail": f"Ops Chrome unavailable ({launched.get('detail')}); used default browser.",
                }
            except Exception as e:
                return {
                    "service": service,
                    "status": "error",
                    "error": str(e),
                    "url": signup_url,
                    "account_name": account_name,
                    "personal_data": data,
                    "payload": payload,
                    "filled_fields": [],
                    "clipboard_prepared": copied,
                }
        return {
            "service": service,
            "status": status,
            "url": signup_url,
            "account_name": account_name,
            "personal_data": data,
            "payload": payload,
            "filled_fields": [],
            "clipboard_prepared": copied,
            "assist_fields": list(ASSIST_FIELD_KEYS),
            "ops_chrome": launched,
            "message": message
            or (
                "Opened signup in the DOWNLOWd Ops Chrome profile. "
                "Use Bitwarden Auto-fill on the TEMP item, or Paste from the companion. "
                "Complete captcha and submit yourself."
            ),
        }

    def _prefill_or_handoff(
        self,
        service: str,
        signup_url: str,
        personal_data: Dict[str, str],
        account_name: str,
        field_map: Dict[str, List[Tuple[str, str]]],
    ) -> Dict[str, Any]:
        data, payload, copied = self._prepare_payload(service, personal_data, account_name)

        if (
            not _SELENIUM_AVAILABLE
            or self.prefer_system_browser
            or self._selenium_disabled
        ):
            reason = (
                "Selenium unavailable"
                if not _SELENIUM_AVAILABLE
                else "no chromedriver / Selenium disabled"
            )
            logging.info("%s: %s; using Ops Chrome handoff", service, reason)
            return self._handoff(
                service,
                signup_url,
                data,
                account_name,
                status="manual_only",
                message=(
                    f"{reason}. Ops Chrome opened; use Bitwarden Auto-fill or the assist companion "
                    "(⌘1–⌘6) to paste fields."
                ),
            )

        filled: List[str] = []
        try:
            driver = self._get_browser()
            wait = WebDriverWait(driver, 15)
            driver.get(signup_url)
            wait.until(
                lambda current: current.execute_script("return document.readyState") == "complete"
            )
            if self._page_looks_blocked(driver):
                logging.warning("%s: bot wall detected for %s; system browser handoff", service, account_name)
                self.close_browser()
                return self._handoff(
                    service,
                    signup_url,
                    data,
                    account_name,
                    status="bot_blocked",
                    message=(
                        "Signup page blocked automated Chrome. Opened Ops Chrome; "
                        "use the assist panel (⌘1–⌘6) to paste fields."
                    ),
                )
            try:
                wait.until(self._focus_form_context)
            except WebDriverException:
                driver.switch_to.default_content()

            if self._page_looks_blocked(driver) or not self._focus_form_context(driver):
                logging.warning("%s: no usable form for %s; system browser handoff", service, account_name)
                self.close_browser()
                return self._handoff(
                    service,
                    signup_url,
                    data,
                    account_name,
                    status="bot_blocked",
                    message=(
                        "No fillable form found (likely bot protection). "
                        "Ops Chrome opened; use Bitwarden Auto-fill or Paste."
                    ),
                )

            for field, selectors in field_map.items():
                value = data.get(field, "")
                if self._fill_first_match(driver, selectors, value):
                    filled.append(field)

            if not filled:
                logging.warning("%s: prefill matched nothing for %s; system browser handoff", service, account_name)
                self.close_browser()
                return self._handoff(
                    service,
                    signup_url,
                    data,
                    account_name,
                    status="manual_only",
                    message=(
                        "Could not autofill any fields. Ops Chrome opened; "
                        "use the assist panel (⌘1–⌘6)."
                    ),
                )

            _copy_to_clipboard(payload)
            logging.info("%s: prefilled %s for %s (browser left open)", service, filled, account_name)
            return {
                "service": service,
                "status": "prefilled",
                "url": signup_url,
                "account_name": account_name,
                "personal_data": data,
                "payload": payload,
                "filled_fields": filled,
                "clipboard_prepared": True,
                "assist_fields": list(ASSIST_FIELD_KEYS),
                "message": (
                    f"Prefill filled: {', '.join(filled)}. "
                    "Finish captcha/submit in the open Chrome window, "
                    "or use the assist panel for remaining fields."
                ),
            }
        except WebDriverException as e:
            logging.error("%s Selenium failed for %s: %s", service, account_name, e)
            self.close_browser()
            return self._handoff(
                service,
                signup_url,
                data,
                account_name,
                status="manual_only",
                message=f"Chrome automation failed ({e}). Ops Chrome opened; use assist panel.",
            )

    def create_outlook_account(self, personal_data: Dict[str, str], account_name: str) -> Dict[str, Any]:
        logging.info("Starting Outlook account creation for %s", account_name)
        data = normalize_personal_data(personal_data)
        # Outlook step 1 wants the local part or full email in MemberName.
        if data.get("username") and "@" not in data["username"]:
            data = {**data, "username": data["username"]}
        field_map = {
            "username": [
                (By.NAME, "MemberName"),
                (By.ID, "usernameInput"),
                (By.ID, "MemberName"),
                (By.CSS_SELECTOR, "input[type='email']"),
                (By.CSS_SELECTOR, "input[autocomplete='username']"),
                (By.CSS_SELECTOR, "input[name='loginfmt']"),
            ],
            "email": [
                (By.NAME, "MemberName"),
                (By.ID, "usernameInput"),
                (By.CSS_SELECTOR, "input[type='email']"),
            ],
        }
        return self._prefill_or_handoff(
            "Outlook",
            "https://signup.live.com/",
            data,
            account_name,
            field_map,
        )

    def create_hyatt_account(self, personal_data: Dict[str, str], account_name: str) -> Dict[str, Any]:
        logging.info("Starting Hyatt account creation for %s", account_name)
        data = normalize_personal_data(personal_data)
        field_map = {
            "first_name": [
                (By.ID, "firstName"),
                (By.NAME, "firstName"),
                (By.CSS_SELECTOR, "input[autocomplete='given-name']"),
                (By.CSS_SELECTOR, "input[name*='first']"),
            ],
            "last_name": [
                (By.ID, "lastName"),
                (By.NAME, "lastName"),
                (By.CSS_SELECTOR, "input[autocomplete='family-name']"),
                (By.CSS_SELECTOR, "input[name*='last']"),
            ],
            "email": [
                (By.ID, "email"),
                (By.NAME, "email"),
                (By.CSS_SELECTOR, "input[type='email']"),
                (By.CSS_SELECTOR, "input[autocomplete='email']"),
            ],
            "password": [
                (By.ID, "password"),
                (By.NAME, "password"),
                (By.CSS_SELECTOR, "input[type='password']"),
                (By.CSS_SELECTOR, "input[autocomplete='new-password']"),
            ],
            "confirm_password": [
                (By.ID, "confirmPassword"),
                (By.NAME, "confirmPassword"),
                (By.CSS_SELECTOR, "input[autocomplete='new-password']"),
                (By.XPATH, "(//input[@type='password'])[2]"),
            ],
        }
        return self._prefill_or_handoff(
            "Hyatt",
            "https://www.hyatt.com/en-US/member/enroll",
            data,
            account_name,
            field_map,
        )

    def create_marriott_account(self, personal_data: Dict[str, str], account_name: str) -> Dict[str, Any]:
        logging.info("Starting Marriott account creation for %s", account_name)
        data = normalize_personal_data(personal_data)
        field_map = {
            "first_name": [
                (By.ID, "firstName"),
                (By.NAME, "firstName"),
                (By.CSS_SELECTOR, "input[autocomplete='given-name']"),
                (By.CSS_SELECTOR, "input[id*='firstName']"),
                (By.CSS_SELECTOR, "input[name*='firstName']"),
            ],
            "last_name": [
                (By.ID, "lastName"),
                (By.NAME, "lastName"),
                (By.CSS_SELECTOR, "input[autocomplete='family-name']"),
                (By.CSS_SELECTOR, "input[id*='lastName']"),
                (By.CSS_SELECTOR, "input[name*='lastName']"),
            ],
            "email": [
                (By.ID, "email"),
                (By.NAME, "email"),
                (By.CSS_SELECTOR, "input[type='email']"),
                (By.CSS_SELECTOR, "input[autocomplete='email']"),
                (By.CSS_SELECTOR, "input[id*='email']"),
            ],
            "password": [
                (By.ID, "password"),
                (By.NAME, "password"),
                (By.CSS_SELECTOR, "input[autocomplete='new-password']"),
                (By.XPATH, "(//input[@type='password'])[1]"),
            ],
            "confirm_password": [
                (By.ID, "confirmPassword"),
                (By.NAME, "confirmPassword"),
                (By.ID, "passwordConfirm"),
                (By.NAME, "passwordConfirm"),
                (By.CSS_SELECTOR, "input[id*='confirm'][type='password']"),
                (By.XPATH, "(//input[@type='password'])[2]"),
            ],
            "postal": [
                (By.ID, "postalCode"),
                (By.NAME, "postalCode"),
                (By.ID, "zipCode"),
                (By.NAME, "zipCode"),
                (By.CSS_SELECTOR, "input[autocomplete='postal-code']"),
                (By.CSS_SELECTOR, "input[id*='postal']"),
                (By.CSS_SELECTOR, "input[id*='zip']"),
                (By.CSS_SELECTOR, "input[name*='postal']"),
                (By.CSS_SELECTOR, "input[name*='zip']"),
            ],
            "country": [
                (By.ID, "country"),
                (By.NAME, "country"),
                (By.ID, "countryCode"),
                (By.NAME, "countryCode"),
                (By.CSS_SELECTOR, "select[autocomplete='country']"),
                (By.CSS_SELECTOR, "select[id*='country']"),
            ],
        }
        return self._prefill_or_handoff(
            "Marriott",
            "https://www.marriott.com/loyalty/createAccount/createAccountPage1.mi",
            data,
            account_name,
            field_map,
        )

    def create_all_accounts(self, personal_data: Dict[str, str], account_name: str) -> Dict[str, Any]:
        return {
            "account_name": account_name,
            "services": {
                "outlook": self.create_outlook_account(personal_data, account_name),
                "hyatt": self.create_hyatt_account(personal_data, account_name),
                "marriott": self.create_marriott_account(personal_data, account_name),
            },
        }


# Custom field *names* mirror HTML name/id attributes on each signup form so the
# Bitwarden browser extension can match them during Auto-fill.
# match: 0 Domain, 1 Host, 2 StartsWith, 3 Exact
SITE_AUTOFILL_SPECS: Dict[str, Dict[str, Any]] = {
    "Outlook": {
        "urls": ["https://signup.live.com/"],
        "match": 2,
        "username_key": "username",
        "password_key": "password",
        "custom_fields": [
            ("MemberName", "username", 0),
            ("usernameInput", "username", 0),
            ("loginfmt", "email", 0),
            ("i0116", "email", 0),
            ("email", "email", 0),
        ],
    },
    "Hyatt": {
        "urls": [
            "https://www.hyatt.com/en-US/member/enroll",
            "https://www.hyatt.com/",
        ],
        "match": 1,
        "username_key": "email",
        "password_key": "password",
        "custom_fields": [
            ("firstName", "first_name", 0),
            ("lastName", "last_name", 0),
            ("email", "email", 0),
            ("password", "password", 1),
            ("confirmPassword", "confirm_password", 1),
        ],
    },
    "Marriott": {
        "urls": [
            "https://www.marriott.com/loyalty/createAccount/createAccountPage1.mi",
            "https://www.marriott.com/",
        ],
        "match": 1,
        "username_key": "email",
        "password_key": "password",
        "custom_fields": [
            ("firstName", "first_name", 0),
            ("lastName", "last_name", 0),
            ("email", "email", 0),
            ("password", "password", 1),
            ("confirmPassword", "confirm_password", 1),
            ("passwordConfirm", "confirm_password", 1),
            ("postalCode", "postal", 0),
            ("zipCode", "postal", 0),
        ],
    },
}


def build_temp_autofill_payload(
    service: str,
    personal_data: Dict[str, str],
    account_name: str,
) -> Dict[str, Any]:
    """Build a temporary Bitwarden login item shaped for site autofill."""
    spec = SITE_AUTOFILL_SPECS.get(service)
    if not spec:
        raise KeyError(f"No autofill spec for {service}")
    data = normalize_personal_data(personal_data)
    username = data.get(spec["username_key"], "") or data.get("email") or data.get("username", "")
    password = data.get(spec["password_key"], "") or data.get("password", "")
    fields = []
    linked = []
    for field_name, data_key, field_type in spec["custom_fields"]:
        value = data.get(data_key, "")
        if not value:
            continue
        fields.append({"name": field_name, "value": value, "type": field_type})
        linked.append(field_name)
    uris = [{"uri": url, "match": spec["match"]} for url in spec["urls"]]
    payload = {
        "type": 1,
        "name": f"DOWNLOWD · TEMP · {service} · {account_name}",
        "notes": (
            "Temporary signup autofill profile created by DOWNLOWd. "
            "Safe to delete after the account exists."
        ),
        "favorite": True,
        "fields": fields,
        "login": {
            "username": username,
            "password": password,
            "totp": None,
            "uris": uris,
        },
    }
    return {"payload": payload, "linked_fields": linked, "urls": list(spec["urls"])}


class TemporaryAutofillManager:
    """Push short-lived Bitwarden login items the browser extension can autofill."""

    def __init__(self, bitwarden: Any):
        self.bitwarden = bitwarden
        self._temp_item_ids: List[str] = []

    def push(
        self,
        service: str,
        personal_data: Dict[str, str],
        account_name: str,
    ) -> Dict[str, Any]:
        built = build_temp_autofill_payload(service, personal_data, account_name)
        item = self.bitwarden.create_item(built["payload"])
        item_id = str(item.get("id") or "")
        if item_id:
            self._temp_item_ids.append(item_id)
        try:
            self.bitwarden.sync()
        except Exception:
            logging.warning("Bitwarden sync after temp autofill create failed", exc_info=True)
        linked = built["linked_fields"]
        return {
            "autofill_item_id": item_id,
            "autofill_linked_fields": linked,
            "autofill_urls": built["urls"],
            "autofill_ready": bool(item_id),
            "autofill_message": (
                f"Temporary Bitwarden autofill profile ready ({len(linked)} linked fields). "
                "In the browser extension: open the signup page → Auto-fill "
                f"“DOWNLOWD · TEMP · {service}”."
            ),
        }

    def cleanup(self) -> List[str]:
        removed: List[str] = []
        for item_id in list(self._temp_item_ids):
            try:
                self.bitwarden.delete_item_permanently(item_id)
                removed.append(item_id)
            except Exception:
                try:
                    self.bitwarden.trash_item(item_id)
                    removed.append(item_id)
                except Exception:
                    logging.warning("Failed to remove temp autofill item %s", item_id, exc_info=True)
        self._temp_item_ids = [i for i in self._temp_item_ids if i not in removed]
        if removed:
            try:
                self.bitwarden.sync()
            except Exception:
                logging.warning("Bitwarden sync after temp autofill cleanup failed", exc_info=True)
        return removed
