"""
Account Creation Automation Module
Selenium form prefill with clipboard + browser-handoff fallback.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import webbrowser
from typing import Any, Dict, List, Tuple

try:
    from selenium import webdriver
    from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    _SELENIUM_AVAILABLE = True
except ImportError:
    _SELENIUM_AVAILABLE = False


def _copy_to_clipboard(text: str) -> bool:
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


def _clipboard_payload(personal_data: Dict[str, str], account_name: str) -> str:
    lines = [
        f"account_name: {account_name}",
        f"full_name: {personal_data.get('full_name', '')}",
        f"first_name: {personal_data.get('first_name', '')}",
        f"last_name: {personal_data.get('last_name', '')}",
        f"email: {personal_data.get('email', '')}",
        f"password: {personal_data.get('password', '')}",
    ]
    return "\n".join(lines)


class AccountCreator:
    """Prefills partner signup forms via Selenium; falls back to browser handoff."""

    def __init__(self, headless: bool = False):
        self.headless = headless

    def _get_browser(self):
        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--window-size=1280,900")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        # Keep the window open after the driver disconnects so the user can finish captcha/submit.
        options.add_experimental_option("detach", True)
        return webdriver.Chrome(options=options)

    def _fill_first_match(self, driver, wait: Any, selectors: List[Tuple[str, str]], value: str) -> bool:
        if not value:
            return False
        for by, sel in selectors:
            try:
                el = wait.until(EC.presence_of_element_located((by, sel)))
                el.clear()
                el.send_keys(value)
                return True
            except (TimeoutException, NoSuchElementException, WebDriverException):
                continue
        return False

    def _handoff(self, service: str, signup_url: str, personal_data: Dict[str, str], account_name: str) -> Dict[str, Any]:
        payload = _clipboard_payload(personal_data, account_name)
        copied = _copy_to_clipboard(payload)
        try:
            webbrowser.open(signup_url)
        except Exception as e:
            return {"service": service, "status": "error", "error": str(e), "url": signup_url}
        return {
            "service": service,
            "status": "manual_completion_required",
            "url": signup_url,
            "account_name": account_name,
            "clipboard_prepared": copied,
            "message": (
                "Opened signup page. Field values were copied to the clipboard "
                if copied
                else "Opened signup page. Complete signup manually "
            )
            + "(complete captcha / remaining fields yourself).",
        }

    def _prefill_or_handoff(
        self,
        service: str,
        signup_url: str,
        personal_data: Dict[str, str],
        account_name: str,
        field_map: Dict[str, List[Tuple[str, str]]],
    ) -> Dict[str, Any]:
        if not _SELENIUM_AVAILABLE:
            logging.warning("%s: Selenium unavailable; using browser handoff", service)
            return self._handoff(service, signup_url, personal_data, account_name)

        driver = None
        filled: List[str] = []
        try:
            driver = self._get_browser()
            wait = WebDriverWait(driver, 15)
            driver.get(signup_url)
            for field, selectors in field_map.items():
                value = personal_data.get(field, "")
                if self._fill_first_match(driver, wait, selectors, value):
                    filled.append(field)

            _copy_to_clipboard(_clipboard_payload(personal_data, account_name))
            logging.info("%s: prefilled %s for %s (browser left open)", service, filled, account_name)
            return {
                "service": service,
                "status": "prefilled_awaiting_manual",
                "url": signup_url,
                "account_name": account_name,
                "filled_fields": filled,
                "clipboard_prepared": True,
                "message": (
                    f"Prefill attempted for {', '.join(filled) or 'no fields'}. "
                    "Complete captcha and submit in the open browser window. "
                    "Field values were also copied to the clipboard."
                ),
            }
        except WebDriverException as e:
            logging.error("%s Selenium failed for %s: %s", service, account_name, e)
            return self._handoff(service, signup_url, personal_data, account_name)
        finally:
            # With detach=True, quit() releases the driver but leaves Chrome open for the user.
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

    def create_outlook_account(self, personal_data: Dict[str, str], account_name: str) -> Dict[str, Any]:
        logging.info("Starting Outlook account creation for %s", account_name)
        # Microsoft signup is heavily bot-protected; handoff + clipboard is more reliable.
        return self._handoff(
            "Outlook",
            "https://signup.live.com/",
            personal_data,
            account_name,
        )

    def create_hyatt_account(self, personal_data: Dict[str, str], account_name: str) -> Dict[str, Any]:
        logging.info("Starting Hyatt account creation for %s", account_name)
        first = personal_data.get("first_name") or (personal_data.get("full_name") or "").split(" ", 1)[0]
        last = personal_data.get("last_name") or (
            (personal_data.get("full_name") or "").split(" ", 1)[1]
            if " " in (personal_data.get("full_name") or "")
            else ""
        )
        data = {**personal_data, "first_name": first, "last_name": last}
        field_map = {
            "first_name": [(By.ID, "firstName"), (By.NAME, "firstName")],
            "last_name": [(By.ID, "lastName"), (By.NAME, "lastName")],
            "email": [(By.ID, "email"), (By.NAME, "email"), (By.CSS_SELECTOR, "input[type='email']")],
            "password": [(By.ID, "password"), (By.NAME, "password"), (By.CSS_SELECTOR, "input[type='password']")],
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
        first = personal_data.get("first_name") or (personal_data.get("full_name") or "").split(" ", 1)[0]
        last = personal_data.get("last_name") or (
            (personal_data.get("full_name") or "").split(" ", 1)[1]
            if " " in (personal_data.get("full_name") or "")
            else ""
        )
        data = {**personal_data, "first_name": first, "last_name": last}
        field_map = {
            "first_name": [(By.ID, "firstName"), (By.NAME, "firstName")],
            "last_name": [(By.ID, "lastName"), (By.NAME, "lastName")],
            "email": [(By.ID, "email"), (By.NAME, "email"), (By.CSS_SELECTOR, "input[type='email']")],
            "password": [(By.ID, "password"), (By.NAME, "password"), (By.CSS_SELECTOR, "input[type='password']")],
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
