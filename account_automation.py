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
        self._driver = None

    def _get_browser(self):
        if self._driver is not None:
            try:
                _ = self._driver.current_url
                return self._driver
            except Exception:
                self._driver = None

        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--incognito")
        options.add_argument("--window-size=1280,900")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        self._driver = webdriver.Chrome(options=options)
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

    def _fill_first_match(self, driver, selectors: List[Tuple[str, str]], value: str) -> bool:
        if not value:
            return False
        for by, sel in selectors:
            try:
                elements = driver.find_elements(by, sel)
                if not elements:
                    continue
                el = next((item for item in elements if item.is_displayed()), elements[0])
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

        filled: List[str] = []
        try:
            driver = self._get_browser()
            wait = WebDriverWait(driver, 15)
            driver.get(signup_url)
            wait.until(lambda current: current.execute_script("return document.readyState") == "complete")
            try:
                wait.until(self._focus_form_context)
            except WebDriverException:
                driver.switch_to.default_content()
            for field, selectors in field_map.items():
                value = personal_data.get(field, "")
                if self._fill_first_match(driver, selectors, value):
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
            self.close_browser()
            return self._handoff(service, signup_url, personal_data, account_name)

    def create_outlook_account(self, personal_data: Dict[str, str], account_name: str) -> Dict[str, Any]:
        logging.info("Starting Outlook account creation for %s", account_name)
        field_map = {
            "username": [
                (By.NAME, "MemberName"),
                (By.ID, "usernameInput"),
                (By.CSS_SELECTOR, "input[type='email']"),
                (By.CSS_SELECTOR, "input[autocomplete='username']"),
            ],
        }
        return self._prefill_or_handoff(
            "Outlook",
            "https://signup.live.com/",
            personal_data,
            account_name,
            field_map,
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
            "first_name": [
                (By.ID, "firstName"),
                (By.NAME, "firstName"),
                (By.CSS_SELECTOR, "input[autocomplete='given-name']"),
            ],
            "last_name": [
                (By.ID, "lastName"),
                (By.NAME, "lastName"),
                (By.CSS_SELECTOR, "input[autocomplete='family-name']"),
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
            "first_name": [
                (By.ID, "firstName"),
                (By.NAME, "firstName"),
                (By.CSS_SELECTOR, "input[autocomplete='given-name']"),
            ],
            "last_name": [
                (By.ID, "lastName"),
                (By.NAME, "lastName"),
                (By.CSS_SELECTOR, "input[autocomplete='family-name']"),
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
