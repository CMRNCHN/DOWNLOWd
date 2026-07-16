"""
Merged credential storage + Microsoft 365 email integration.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Any

import msal
import requests

CREDENTIALS_FILE = Path.home() / ".onboarding_credentials.json"
GRAPH_ENDPOINT = "https://graph.microsoft.com/v1.0/users"


class CredentialStore:
    def __init__(self):
        self._store = self._load()

    def _load(self) -> Dict[str, str]:
        if not CREDENTIALS_FILE.exists():
            return {}
        try:
            return json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logging.warning("Could not decode credentials file. Starting fresh.")
            return {}

    def save(self) -> None:
        CREDENTIALS_FILE.write_text(json.dumps(self._store, indent=2), encoding="utf-8")

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def get_all(self) -> Dict[str, str]:
        return self._store

    def update(self, new_creds: Dict[str, str]):
        self._store.update(new_creds)
        self.save()


class EmailService:
    def __init__(self, credential_store: CredentialStore):
        self.creds = credential_store.get_all()

    def _get_graph_token(self, tenant_id: str, client_id: str, client_secret: str) -> str:
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        app = msal.ConfidentialClientApplication(
            client_id, authority=authority, client_credential=client_secret
        )
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise RuntimeError(f"Failed to obtain Graph API token: {result}")
        return result["access_token"]

    def create_user_account(self, full_name: str, username: str, initial_password: str):
        tenant_id = self.creds.get("tenant_id")
        client_id = self.creds.get("client_id")
        client_secret = self.creds.get("client_secret")
        domain = self.creds.get("domain")

        if not all([tenant_id, client_id, client_secret, domain]):
            logging.warning("[M365] Missing required credentials. Cannot create email account.")
            return

        user_principal_name = f"{username}@{domain}"
        logging.info(f"[M365] Creating Microsoft 365 account for {full_name}: {user_principal_name}")

        try:
            token = self._get_graph_token(tenant_id, client_id, client_secret)
            payload: Dict[str, Any] = {
                "accountEnabled": True,
                "displayName": full_name,
                "mailNickname": username,
                "userPrincipalName": user_principal_name,
                "passwordProfile": {
                    "forceChangePasswordNextSignIn": True,
                    "password": initial_password,
                },
            }
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            response = requests.post(GRAPH_ENDPOINT, headers=headers, json=payload)
            response.raise_for_status()
            logging.info(f"[M365] Successfully created mailbox for {full_name}")
        except Exception as e:
            logging.error(f"[M365] EXCEPTION during mailbox creation: {e}", exc_info=True)