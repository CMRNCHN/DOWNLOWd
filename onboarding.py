#!/usr/bin/env python3
"""
Merged onboarding logic + partner account logic.
"""

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from bw_import_converter import convert_file_to_bitwarden_json
from integrations import CredentialStore, EmailService


@dataclass
class BitwardenConfig:
    collection_name: str = "Employee Onboarding"

@dataclass
class OnboardingConfig:
    bw: BitwardenConfig
    secure_delete_local: bool = True
    shred_bitwarden_items: bool = False
    provision_email: bool = True
    provision_hyatt: bool = True
    provision_marriott: bool = True


class Onboarding:
    def __init__(self, credential_store: CredentialStore, email_service: EmailService):
        self.credentials = credential_store
        self.email_service = email_service

    def run(self, input_dir: Path, initial_password: str, config: OnboardingConfig):
        self._check_bitwarden_unlocked()
        collection_id = self._get_collection_id(config.bw.collection_name)

        files = sorted(
            [p for p in input_dir.iterdir() if p.is_file() and p.name.startswith("HQ-") and p.suffix in {".txt", ".rtf"}]
        )
        if not files:
            logging.warning("No matching employee export files in %s", input_dir)
            return

        logging.info("Found %d file(s) to process.", len(files))
        json_files: List[Path] = []

        for src in files:
            out = src.with_suffix(".bw.json")
            logging.info("Converting %s -> %s", src.name, out.name)
            stats: Dict[str, Any] = convert_file_to_bitwarden_json(src, out, initial_password)
            json_files.append(out)

            for full_name, username, _ in stats.get("usernames", []): # type: ignore
                domain = self.credentials.get("domain", "outlook.com")
                email = f"{username}@{domain}"

                if config.provision_email:
                    self.email_service.create_user_account(full_name, username, initial_password)

                if config.provision_hyatt:
                    self._create_hyatt_account(full_name, email, initial_password)

                if config.provision_marriott:
                    self._create_marriott_account(full_name, email, initial_password)

        for jf in json_files:
            logging.info("Importing %s into Bitwarden...", jf.name)
            self._import_bitwarden_json(jf, collection_id)

        if config.secure_delete_local:
            for jf in json_files:
                self._secure_delete(jf)
                src_txt = jf.with_suffix(".txt")
                src_rtf = jf.with_suffix(".rtf")
                if src_txt.exists(): self._secure_delete(src_txt)
                if src_rtf.exists(): self._secure_delete(src_rtf)

        if config.shred_bitwarden_items:
            logging.warning("Shredding all items in collection '%s'...", config.bw.collection_name)
            items = self._list_onboarding_items(collection_id)
            for item in items:
                name = item.get("name", "Unknown Item")
                if name.endswith("— Work Login") or name.endswith("— Personal Details") or name.endswith("— Work Card"):
                    logging.info("Deleting Bitwarden item: %s", name)
                    self._delete_bitwarden_item(item["id"])
        logging.info("--- Onboarding pipeline complete. ---")

    def _check_bitwarden_unlocked(self) -> None:
        logging.info("Checking Bitwarden CLI status...")
        try:
            proc = subprocess.run(["bw", "status", "--raw"], capture_output=True, text=True, check=True)
            status = json.loads(proc.stdout).get("status")
            if status != "unlocked":
                raise RuntimeError(f"Bitwarden vault is not unlocked (status: {status}). Run 'bw unlock' first.")
            logging.info("Bitwarden vault is unlocked.")
        except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as e:
            logging.error("Failed to check Bitwarden status. Is 'bw' CLI installed and in your PATH? Details: %s", e)
            raise

    def _get_collection_id(self, collection_name: str) -> str:
        logging.info("Fetching Collection ID for '%s'...", collection_name)
        try:
            proc = subprocess.run(["bw", "get", "collection", collection_name], capture_output=True, text=True, check=True)
            cid = json.loads(proc.stdout).get("id")
            if not cid or cid == "null":
                raise RuntimeError(f"Could not find Bitwarden Collection named '{collection_name}'.")
            logging.info("Found Collection ID: %s", cid)
            return cid
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            logging.error("Could not find Bitwarden Collection named '%s'. Details: %s", collection_name, e)
            raise

    def _import_bitwarden_json(self, json_file: Path, collection_id: str) -> None:
        subprocess.run(["bw", "import", "bitwardenjson", str(json_file), "--collectionid", collection_id], check=True)

    def _list_onboarding_items(self, collection_id: str) -> List[Dict[str, Any]]:
        proc = subprocess.run(["bw", "list", "items", "--collectionid", collection_id, "--raw"], capture_output=True, text=True, check=True)
        return json.loads(proc.stdout)

    def _delete_bitwarden_item(self, item_id: str) -> None:
        subprocess.run(["bw", "delete", "item", item_id], check=True)

    def _secure_delete(self, path: Path) -> None:
        if not path.exists(): return
        if shutil.which("srm"):
            subprocess.run(["srm", "-f", str(path)], check=True, capture_output=True)
        elif shutil.which("shred"):
            subprocess.run(["shred", "-n", "3", "-z", "-u", str(path)], check=True, capture_output=True)
        else:
            logging.warning("srm/shred not found. Using standard delete for %s.", path.name)
            path.unlink(missing_ok=True)

    def _get_browser(self, headless: bool = True) -> WebDriver:
        options = Options()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--window-size=1920,1080")
        return webdriver.Chrome(options=options)

    def _create_hyatt_account(self, full_name: str, email: str, password: str, headless: bool = True):
        first, last = full_name.split(" ", 1)
        logging.info(f"[Hyatt] Creating Hyatt account for {full_name} ({email})")
        driver = self._get_browser(headless=headless)
        wait = WebDriverWait(driver, 20)
        try:
            driver.get("https://www.hyatt.com/en-US/member/enroll")
            wait.until(EC.presence_of_element_located((By.ID, "firstName"))).send_keys(first)
            driver.find_element(By.ID, "lastName").send_keys(last)
            driver.find_element(By.ID, "email").send_keys(email)
            driver.find_element(By.ID, "country").send_keys("United States")
            try:
                driver.find_element(By.ID, "state").send_keys("New Jersey")
            except NoSuchElementException:
                pass
            driver.find_element(By.ID, "password").send_keys(password)
            try:
                driver.find_element(By.ID, "confirmPassword").send_keys(password)
            except NoSuchElementException:
                pass
            driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
            time.sleep(3)
            logging.info(f"[Hyatt] Submitted signup form for {full_name}")
        except (TimeoutException, NoSuchElementException) as e:
            logging.error(f"[Hyatt] ERROR creating account for {full_name}: {e}", exc_info=True)
        finally:
            driver.quit()

    def _create_marriott_account(self, full_name: str, email: str, password: str, headless: bool = True):
        first, last = full_name.split(" ", 1)
        logging.info(f"[Marriott] Creating Marriott account for {full_name} ({email})")
        driver = self._get_browser(headless=headless)
        wait = WebDriverWait(driver, 20)
        try:
            driver.get("https://www.marriott.com/en-gb/loyalty/createAccount/createAccountPage1.mi")
            wait.until(EC.presence_of_element_located((By.ID, "firstName"))).send_keys(first)
            driver.find_element(By.ID, "lastName").send_keys(last)
            driver.find_element(By.ID, "country").send_keys("United States")
            driver.find_element(By.ID, "email").send_keys(email)
            driver.find_element(By.ID, "password").send_keys(password)
            driver.find_element(By.ID, "confirmPassword").send_keys(password)
            driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
            time.sleep(3)
            logging.info(f"[Marriott] Submitted signup form for {full_name}")
        except (TimeoutException, NoSuchElementException) as e:
            logging.error(f"[Marriott] ERROR creating account for {full_name}: {e}", exc_info=True)
        finally:
            driver.quit()