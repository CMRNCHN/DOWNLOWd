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
    """Best-effort clipboard copy (macOS pbcopy / Linux xclip or xsel)."""
    from shutil import which

    try:
        if sys.platform == "darwin":
            proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
            return proc.returncode == 0
        # X11: prefer xclip, fall back to xsel so a stock desktop still works.
        for cmd in (
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ):
            if which(cmd[0]) is None:
                continue
            proc = subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            return proc.returncode == 0
        logging.warning("No clipboard tool found (install xclip or xsel).")
        return False
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
    Copy `value` to the clipboard and synthesize a paste into the frontmost app.

    macOS uses AppleScript (needs Accessibility permission); X11/Linux uses
    xdotool. When no paste tool is available the value is still on the clipboard
    so the operator can paste it manually.
    """
    if not value:
        return False
    if not _copy_to_clipboard(value):
        return False
    if sys.platform == "darwin":
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
    # X11/Linux: send Ctrl+V to the focused window via xdotool when present.
    from shutil import which

    if which("xdotool") is not None:
        try:
            proc = subprocess.run(
                ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            return proc.returncode == 0
        except Exception:
            return False
    # Clipboard is populated; caller can paste manually.
    return True


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


# Default partner signup endpoints. Each can be overridden with an env var
# (e.g. DOWNLOWD_OUTLOOK_URL) to target a staging/self-hosted signup page or to
# run the flow end-to-end in tests without hitting the live, bot-walled sites.
SIGNUP_URLS: Dict[str, Tuple[str, str]] = {
    "Outlook": ("DOWNLOWD_OUTLOOK_URL", "https://signup.live.com/"),
    "Hyatt": ("DOWNLOWD_HYATT_URL", "https://www.hyatt.com/en-US/member/enroll"),
    "Marriott": (
        "DOWNLOWD_MARRIOTT_URL",
        "https://www.marriott.com/loyalty/createAccount/createAccountPage1.mi",
    ),
}


def signup_url(service: str) -> str:
    """Resolve a partner signup URL, honoring a per-service env override."""
    env_key, default = SIGNUP_URLS.get(service, ("", ""))
    return (os.environ.get(env_key) or default) if env_key else default


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
        options.add_argument("--incognito")
        options.add_argument("--window-size=1280,900")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_bin = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if sys.platform == "darwin" and os.path.exists(chrome_bin):
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
        # Bound page loads / scripts so a slow or unresponsive signup page can
        # never hang the provisioning worker indefinitely.
        try:
            self._driver.set_page_load_timeout(30)
            self._driver.set_script_timeout(15)
        except WebDriverException:
            pass
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
        try:
            webbrowser.open(signup_url)
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
            "message": message
            or (
                "Opened signup in the system browser. Use the assist panel "
                "(⌘1–⌘6) to paste fields. Complete captcha and submit yourself."
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
            logging.info("%s: %s; using system browser handoff", service, reason)
            return self._handoff(
                service,
                signup_url,
                data,
                account_name,
                status="manual_only",
                message=(
                    f"{reason}. System browser opened; use the assist panel "
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
                        "Signup page blocked automated Chrome. Opened system browser; "
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
                        "System browser opened; use the assist panel."
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
                        "Could not autofill any fields. System browser opened; "
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
                message=f"Chrome automation failed ({e}). System browser opened; use assist panel.",
            )

    def create_outlook_account(self, personal_data: Dict[str, str], account_name: str) -> Dict[str, Any]:
        logging.info("Starting Outlook account creation for %s", account_name)
        data = normalize_personal_data(personal_data)
        # Outlook step 1 accepts either the local part or the full email in MemberName.
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
            signup_url("Outlook"),
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
            signup_url("Hyatt"),
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
            signup_url("Marriott"),
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
