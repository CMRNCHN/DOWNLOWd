#!/usr/bin/env python3
"""
A single unified GUI module combining main window, settings, and onboarding flow.
"""

import logging
import queue
import sys
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from typing import Dict, Set

from onboarding import Onboarding, OnboardingConfig, BitwardenConfig
from integrations import CredentialStore, EmailService

DOWNLOADS = Path.home() / "Downloads"


class AppGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("DOWNLOWd")
        self.root.geometry("800x600")

        # Setup dependencies
        self.credential_store = CredentialStore()
        self.email_service = EmailService(self.credential_store)
        self.onboarding_logic = Onboarding(self.credential_store, self.email_service)

        self.build_main_screen()

    def build_main_screen(self):
        # This is now the main application window, so we build the full UI here.
        # The logic is taken from your old `onboarding_gui.py`.
        self.onboarding_window = OnboardingGUI(self.root, self.onboarding_logic, self)

    def start_onboarding(self):
        # This button is now part of the main OnboardingGUI
        self.onboarding_window.run_import_clicked()

    def open_settings(self):
        SettingsGUI(self.root, self.credential_store)

    def run(self):
        self.root.mainloop()


class SettingsGUI(tk.Toplevel):
    def __init__(self, parent: tk.Tk, credential_store: CredentialStore):
        super().__init__(parent)
        self.title("M365 Settings")
        self.credential_store = credential_store
        self.geometry("480x220")
        self.resizable(False, False)
        self.transient(parent)

        all_creds = self.credential_store.get_all()
        self.m365_vars: Dict[str, tk.StringVar] = {
            "tenant_id": tk.StringVar(value=all_creds.get("tenant_id", "")),
            "client_id": tk.StringVar(value=all_creds.get("client_id", "")),
            "client_secret": tk.StringVar(value=all_creds.get("client_secret", "")),
            "domain": tk.StringVar(value=all_creds.get("domain", "")),
        }
        self._create_widgets()

    def _create_widgets(self):
        frame = ttk.Frame(self, padding="10")
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)
        fields = {"Tenant ID:": "tenant_id", "Client ID:": "client_id", "Client Secret:": "client_secret", "Domain:": "domain"}
        for i, (label_text, key) in enumerate(fields.items()):
            ttk.Label(frame, text=label_text).grid(row=i, column=0, sticky=tk.W, pady=5, padx=5)
            entry = ttk.Entry(frame, textvariable=self.m365_vars[key], width=45)
            if "secret" in key: entry.config(show="*")
            entry.grid(row=i, column=1, sticky=tk.EW, pady=5)
        button_frame = ttk.Frame(frame)
        button_frame.grid(row=len(fields), column=0, columnspan=2, pady=(20, 0))
        ttk.Button(button_frame, text="Save", command=self._save_and_close).pack(side=tk.RIGHT, padx=5)
        ttk.Button(button_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)

    def _save_and_close(self):
        new_creds = {key: var.get() for key, var in self.m365_vars.items()}
        self.credential_store.update(new_creds)
        messagebox.showinfo("Success", "M365 credentials saved successfully.", parent=self)
        self.destroy()


class QueueHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue[str]):
        super().__init__()
        self.log_queue = log_queue
    def emit(self, record: logging.LogRecord):
        self.log_queue.put(self.format(record))


class OnboardingGUI(ttk.Frame):
    def __init__(self, parent: tk.Tk, onboarding_logic: Onboarding, app_controller: AppGUI):
        super().__init__(parent, padding="10")
        self.pack(fill=tk.BOTH, expand=True)
        self.onboarding_logic = onboarding_logic
        self.app_controller = app_controller

        self.auto_import = tk.BooleanVar(value=True)
        self.secure_delete = tk.BooleanVar(value=True)
        self.shred_bw = tk.BooleanVar(value=False)
        self.provision_email = tk.BooleanVar(value=True)
        self.provision_hyatt = tk.BooleanVar(value=True)
        self.provision_marriott = tk.BooleanVar(value=True)
        self.initial_password = tk.StringVar()
        self.log_queue: queue.Queue[str] = queue.Queue()

        self._build_ui()
        self._configure_logging()

        self.after(100, self._poll_log_queue)
        self.monitor_thread = threading.Thread(target=self.monitor_downloads, daemon=True)
        self.monitor_thread.start()

    def _build_ui(self):
        self.status = tk.StringVar(value=f"Monitoring {DOWNLOADS}...")
        ttk.Label(self, textvariable=self.status).pack(pady=5, anchor='w')

        controls_frame = ttk.LabelFrame(self, text="Controls", padding="10")
        controls_frame.pack(fill="x", padx=0, pady=5)
        controls_frame.columnconfigure(1, weight=1)
        ttk.Label(controls_frame, text="Initial Password:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(controls_frame, textvariable=self.initial_password, show="*").grid(row=0, column=1, sticky="ew", padx=5)

        toggles = ttk.LabelFrame(self, text="Options", padding="10")
        toggles.pack(fill="x", padx=0, pady=5)
        toggles.columnconfigure(1, weight=1)
        ttk.Checkbutton(toggles, text="Auto-import new files", variable=self.auto_import).grid(row=0, column=0, sticky="w", padx=5)
        ttk.Checkbutton(toggles, text="Securely delete local files", variable=self.secure_delete).grid(row=1, column=0, sticky="w", padx=5)
        ttk.Checkbutton(toggles, text="Shred Bitwarden items", variable=self.shred_bw).grid(row=2, column=0, sticky="w", padx=5)
        ttk.Checkbutton(toggles, text="Provision email accounts", variable=self.provision_email).grid(row=0, column=1, sticky="w", padx=5)
        ttk.Checkbutton(toggles, text="Provision Hyatt accounts", variable=self.provision_hyatt).grid(row=1, column=1, sticky="w", padx=5)
        ttk.Checkbutton(toggles, text="Provision Marriott accounts", variable=self.provision_marriott).grid(row=2, column=1, sticky="w", padx=5)

        log_frame = ttk.LabelFrame(self, text="Logs", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=0, pady=5)
        self.log = scrolledtext.ScrolledText(log_frame, state="disabled", wrap=tk.WORD, height=10)
        self.log.pack(fill=tk.BOTH, expand=True)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", pady=5)
        ttk.Button(btn_frame, text="M365 Settings", command=self.app_controller.open_settings).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Quit", command=self.app_controller.root.destroy).pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="Run Import Now", command=self.run_import_clicked).pack(side=tk.RIGHT, padx=5)

    def _configure_logging(self):
        log_handler = QueueHandler(self.log_queue)
        log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] - %(message)s", "%H:%M:%S"))
        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)
        root_logger.setLevel(logging.INFO)
        sys.stdout.write = lambda msg: self.log_queue.put(msg.strip()) # type: ignore
        sys.stderr.write = lambda msg: self.log_queue.put(msg.strip()) # type: ignore

    def _poll_log_queue(self):
        while True:
            try:
                record = self.log_queue.get(block=False)
                if record: self.log_msg(str(record))
            except queue.Empty: break
        self.after(100, self._poll_log_queue)

    def log_msg(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert(tk.END, msg + "\n")
        self.log.configure(state="disabled")
        self.log.see(tk.END)

    def monitor_downloads(self):
        seen: Set[Path] = set()
        while True:
            try:
                for f in DOWNLOADS.glob("HQ-*"):
                    if f.name.startswith("HQ-") and f.suffix in {".txt", ".rtf"} and f not in seen:
                        seen.add(f)
                        self.log_msg(f"[Monitor] Detected new file: {f.name}")
                        if self.auto_import.get(): self.run_import_background()
                time.sleep(5)
            except Exception as e:
                self.log_msg(f"[Monitor] ERROR: {e}")
                time.sleep(10)

    def run_import_clicked(self):
        self.run_import_background()

    def run_import_background(self):
        password = self.initial_password.get()
        if not password:
            messagebox.showerror("Error", "Please enter an initial password.")
            return

        self.status.set("Running onboarding pipeline...")
        config = OnboardingConfig(
            bw=BitwardenConfig(),
            secure_delete_local=self.secure_delete.get(),
            shred_bitwarden_items=self.shred_bw.get(),
            provision_email=self.provision_email.get(),
            provision_hyatt=self.provision_hyatt.get(),
            provision_marriott=self.provision_marriott.get(),
        )
        
        # Wrap the pipeline run in a function to handle exceptions and GUI updates
        def pipeline_wrapper():
            try:
                self.onboarding_logic.run(DOWNLOADS, password, config)
            except RuntimeError as e:
                # Handle known errors like Bitwarden being locked
                logging.error(str(e))
                messagebox.showerror("Onboarding Error", str(e))
            except Exception as e:
                # Handle unexpected errors
                logging.error("An unexpected error occurred in the pipeline.", exc_info=True)
                messagebox.showerror("Unexpected Error", f"An unexpected error occurred:\n\n{e}")
            finally:
                # Always reset the status, whether it succeeds or fails
                self.status.set(f"Monitoring {DOWNLOADS}...")

        thread = threading.Thread(target=pipeline_wrapper, daemon=True)
        thread.start()