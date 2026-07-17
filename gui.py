#!/usr/bin/env python3
"""
DOWNLOWd — Bitwarden-first onboarding appliance GUI.

Startup: Bitwarden login only.
Dashboard: intake → Bitwarden → partner accounts → lockdown.
Settings: disposal modes, provisioning toggles, collection config.
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, TextIO, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND_AVAILABLE = True
except Exception:
    DND_FILES = None  # type: ignore
    TkinterDnD = None  # type: ignore
    _DND_AVAILABLE = False

from audit_logger import get_audit_logger
from data_retention import DataRetentionManager
from integrations import BitwardenService, CredentialStore
from onboarding import BitwardenConfig, Onboarding, OnboardingConfig
from secure_delete import (
    BW_SHRED_MODES,
    DEFAULT_BW_SHRED_MODE,
    DEFAULT_LOCAL_DELETE_MODE,
    LOCAL_DELETE_MODES,
)
from transaction_db import TransactionDatabase

DOWNLOADS = Path.home() / "Downloads"

# Dim liquid-glass palette (Apple-ish dark)
C = {
    "bg": "#121214",
    "surface": "#1c1c1e",
    "card": "#2a2a2e",
    "card_hi": "#34343a",
    "border": "#3a3a40",
    "text": "#f5f5f7",
    "muted": "#8e8e93",
    "accent": "#0a84ff",
    "accent_dim": "#0a84ff33",
    "success": "#30d158",
    "warn": "#ffd60a",
    "danger": "#ff453a",
}


def _filevault_status() -> tuple[Optional[bool], str]:
    if sys.platform != "darwin":
        return None, "Disk encryption status is only checked on macOS."
    try:
        proc = subprocess.run(
            ["fdesetup", "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        out = (proc.stdout or proc.stderr or "").strip()
        if "FileVault is On" in out:
            return True, out
        if "FileVault is Off" in out:
            return False, out
        return None, out or "Unable to determine FileVault status."
    except Exception as e:
        return None, f"Unable to check FileVault: {e}"


def apply_theme(root: tk.Tk) -> ttk.Style:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(bg=C["bg"])
    style.configure(".", background=C["bg"], foreground=C["text"], fieldbackground=C["card"])
    style.configure("TFrame", background=C["bg"])
    style.configure("Card.TFrame", background=C["surface"])
    style.configure("Surface.TFrame", background=C["surface"])
    style.configure("TLabel", background=C["bg"], foreground=C["text"], font=("SF Pro Text", 13))
    style.configure("Muted.TLabel", background=C["bg"], foreground=C["muted"], font=("SF Pro Text", 12))
    style.configure("Title.TLabel", background=C["bg"], foreground=C["text"], font=("SF Pro Display", 22, "bold"))
    style.configure("Subtitle.TLabel", background=C["bg"], foreground=C["muted"], font=("SF Pro Text", 13))
    style.configure("CardTitle.TLabel", background=C["surface"], foreground=C["text"], font=("SF Pro Text", 12, "bold"))
    style.configure("CardMuted.TLabel", background=C["surface"], foreground=C["muted"], font=("SF Pro Text", 11))
    style.configure("Icon.TLabel", background=C["surface"], foreground=C["accent"], font=("SF Pro Display", 28))
    style.configure("Drop.TLabel", background=C["card"], foreground=C["text"], font=("SF Pro Text", 13, "bold"))
    style.configure("TButton", background=C["card"], foreground=C["text"], padding=(14, 8), font=("SF Pro Text", 12))
    style.map("TButton", background=[("active", C["card_hi"])])
    style.configure(
        "Accent.TButton",
        background=C["accent"],
        foreground="#ffffff",
        padding=(16, 10),
        font=("SF Pro Text", 13, "bold"),
    )
    style.map("Accent.TButton", background=[("active", "#409cff")])
    style.configure("TEntry", fieldbackground=C["card"], foreground=C["text"], insertcolor=C["text"])
    style.configure("TCheckbutton", background=C["bg"], foreground=C["text"], font=("SF Pro Text", 12))
    style.configure("TLabelframe", background=C["surface"], foreground=C["text"], bordercolor=C["border"])
    style.configure("TLabelframe.Label", background=C["surface"], foreground=C["muted"], font=("SF Pro Text", 11, "bold"))
    style.configure("TNotebook", background=C["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", background=C["surface"], foreground=C["muted"], padding=(16, 8))
    style.map("TNotebook.Tab", background=[("selected", C["card"])], foreground=[("selected", C["text"])])
    style.configure("TCombobox", fieldbackground=C["card"], foreground=C["text"], background=C["card"])
    style.configure("Treeview", background=C["card"], foreground=C["text"], fieldbackground=C["card"], rowheight=26)
    style.configure("Treeview.Heading", background=C["surface"], foreground=C["muted"])
    return style


class BitwardenLoginDialog(tk.Toplevel):
    """Gate the entire app behind Bitwarden CLI login/unlock."""

    def __init__(
        self,
        parent: tk.Tk,
        bw_service: BitwardenService,
        credential_store: CredentialStore,
        on_success: Callable[[], None],
    ):
        super().__init__(parent)
        self.bw_service = bw_service
        self.credential_store = credential_store
        self.on_success = on_success
        self.audit = get_audit_logger()
        self.success = False

        self.title("DOWNLOWd")
        self.geometry("440x420+200+160")
        self.resizable(False, False)
        self.configure(bg=C["bg"])
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._build_ui()
        self.transient(parent)
        self.grab_set()
        self.lift()
        self.attributes("-topmost", True)
        self.after(250, lambda: self.attributes("-topmost", False))
        self.focus_force()

    def _on_cancel(self):
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        try:
            self.master.destroy()
        except tk.TclError:
            pass

    def _build_ui(self):
        frame = tk.Frame(self, bg=C["bg"], padx=28, pady=24)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(frame, text="􀎡", font=("SF Pro Display", 42), fg=C["accent"], bg=C["bg"]).pack(pady=(8, 4))
        tk.Label(
            frame, text="DOWNLOWd", font=("SF Pro Display", 24, "bold"), fg=C["text"], bg=C["bg"]
        ).pack()
        tk.Label(
            frame,
            text="Sign in with Bitwarden to continue",
            font=("SF Pro Text", 13),
            fg=C["muted"],
            bg=C["bg"],
        ).pack(pady=(4, 20))

        form = tk.Frame(frame, bg=C["bg"])
        form.pack(fill=tk.X)
        form.columnconfigure(0, weight=1)

        tk.Label(form, text="Email", fg=C["muted"], bg=C["bg"], font=("SF Pro Text", 11)).grid(
            row=0, column=0, sticky="w"
        )
        email_var = tk.StringVar(value=self.credential_store.get("bw_email", ""))
        email_entry = ttk.Entry(form, textvariable=email_var)
        email_entry.grid(row=1, column=0, sticky="ew", pady=(2, 12))

        tk.Label(form, text="Master Password", fg=C["muted"], bg=C["bg"], font=("SF Pro Text", 11)).grid(
            row=2, column=0, sticky="w"
        )
        password_var = tk.StringVar()
        password_entry = ttk.Entry(form, textvariable=password_var, show="•")
        password_entry.grid(row=3, column=0, sticky="ew", pady=(2, 8))
        password_entry.focus()

        status_var = tk.StringVar(value="")
        status_lbl = tk.Label(form, textvariable=status_var, fg=C["muted"], bg=C["bg"], font=("SF Pro Text", 11))
        status_lbl.grid(row=4, column=0, sticky="w", pady=(4, 8))

        def do_login():
            email = email_var.get().strip()
            password = password_var.get()
            if not email or not password:
                messagebox.showerror("Required", "Enter email and master password.", parent=self)
                return
            status_var.set("Signing in…")
            self.update_idletasks()

            try:
                status = self.bw_service.get_status()
            except Exception:
                status = "unauthenticated"

            result: Dict[str, Any] = {"success": False}
            if status == "unlocked" or status == "locked":
                ok = self.bw_service.unlock(password)
                result = {"success": ok, "error": None if ok else "Incorrect master password."}
            else:
                result = self.bw_service.login(email, password)
                if result.get("two_factor_required"):
                    code = simpledialog.askstring("Two-Factor", "Enter your 2FA code:", parent=self)
                    if not code:
                        status_var.set("")
                        return
                    result = self.bw_service.login(email, password, code)

            if result.get("success"):
                self.credential_store.update({"bw_email": email})
                self.audit.log_authentication(True, method="bitwarden")
                self.success = True
                self.destroy()
                self.on_success()
            else:
                self.audit.log_authentication(False, method="bitwarden")
                status_var.set("")
                messagebox.showerror(
                    "Bitwarden Login Failed",
                    result.get("error") or "Could not sign in.",
                    parent=self,
                )

        ttk.Button(frame, text="Unlock with Bitwarden", style="Accent.TButton", command=do_login).pack(
            fill=tk.X, pady=(12, 8)
        )
        password_entry.bind("<Return>", lambda e: do_login())
        tk.Label(
            frame,
            text="This app is a Bitwarden wrapper. Your vault unlocks the workspace.",
            fg=C["muted"],
            bg=C["bg"],
            font=("SF Pro Text", 11),
            wraplength=360,
            justify=tk.CENTER,
        ).pack(pady=(8, 0))


class QueueStreamWriter:
    def __init__(self, log_queue: queue.Queue[str], original_stream: TextIO):
        self.log_queue = log_queue
        self.original_stream = original_stream

    def write(self, s: str, /) -> int:
        stripped = s.strip()
        if stripped:
            self.log_queue.put(stripped)
        return len(s)

    def flush(self) -> None:
        self.original_stream.flush()


class QueueHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue[str]):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord):
        self.log_queue.put(self.format(record))


class AppGUI:
    def __init__(self):
        self.root: Any = TkinterDnD.Tk() if _DND_AVAILABLE else tk.Tk()
        self.root.title("DOWNLOWd")
        self.root.geometry("920x720+80+60")
        self.session_log_path: Path | None = None
        apply_theme(self.root)

        self.credential_store = CredentialStore()
        self.bw_service = BitwardenService()
        self.transaction_db = TransactionDatabase()
        self.audit = get_audit_logger()
        self.retention_manager = DataRetentionManager(
            self.transaction_db,
            prompt_callback=self._queue_retention_prompt,
        )
        self.onboarding_logic = Onboarding(
            self.bw_service,
            retention_manager=self.retention_manager,
        )
        self._pending_retention_prompts: queue.Queue = queue.Queue()
        self._setup_file_logging()
        self._auth_ok = False

        # Keep root off-screen during Bitwarden gate (avoid blank second window)
        self.root.geometry("1x1-10000-10000")
        self.root.deiconify()
        self.root.update_idletasks()

        if self.bw_service.session_key and self._bw_ready():
            self._auth_ok = True
            self._post_auth()
        else:
            dialog = BitwardenLoginDialog(
                self.root, self.bw_service, self.credential_store, self._on_auth_success
            )
            dialog.wait_window()
            if not self._auth_ok:
                try:
                    if self.root.winfo_exists():
                        self.root.destroy()
                except tk.TclError:
                    pass
                sys.exit(0)

    def _bw_ready(self) -> bool:
        try:
            return self.bw_service.get_status() in {"unlocked", "locked"} and bool(
                self.bw_service.session_key
            )
        except Exception:
            return False

    def _on_auth_success(self):
        self._auth_ok = True
        self._post_auth()

    def _post_auth(self):
        self.retention_manager.start_scheduler(check_interval_hours=24)
        self.build_main_screen()
        self.root.after(500, self._warn_if_filevault_off)

    def _warn_if_filevault_off(self):
        enabled, detail = _filevault_status()
        if enabled is False:
            messagebox.showwarning(
                "FileVault recommended",
                "FileVault is Off.\n\n"
                "Local employee files and the transaction database are not "
                "encrypted at rest without full-disk encryption.\n\n"
                "Enable FileVault before production use.\n\n"
                f"({detail})",
                parent=self.root,
            )
            self.audit.log_security_event("filevault_off", detail)

    def _queue_retention_prompt(self, action: dict):
        self._pending_retention_prompts.put(action)
        self.root.after(0, self._drain_retention_prompts)

    def _drain_retention_prompts(self):
        while True:
            try:
                action = self._pending_retention_prompts.get_nowait()
            except queue.Empty:
                break
            self._show_retention_prompt(action)

    def _show_retention_prompt(self, action: dict):
        employee = action["employee"]
        day = action["day"]
        message = action["message"]
        if day == 5:
            answer = messagebox.askyesno("Retention (Day 5)", f"{message}\n\nYes = still active")
            self.retention_manager.process_audit_response(employee, 5, "yes" if answer else "no")
        elif day == 10:
            answer = messagebox.askyesno("Retention (Day 10)", f"{message}\n\nYes = shred")
            self.retention_manager.process_audit_response(employee, 10, "yes" if answer else "no")

    def build_main_screen(self):
        self.root.title("DOWNLOWd")
        self.root.geometry("920x720+80+60")
        for child in self.root.winfo_children():
            child.destroy()
        self.dashboard = Dashboard(self.root, self)
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(350, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def run(self):
        self.root.mainloop()

    def _setup_file_logging(self):
        log_dir = Path.cwd() / "logs"
        log_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_file = log_dir / f"onboarding_{timestamp}.log"
        self.session_log_path = log_file
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] - %(message)s"))
        logging.getLogger().addHandler(file_handler)
        logging.getLogger().setLevel(logging.INFO)


class Dashboard(ttk.Frame):
    """Main workspace: workflow dashboard + transactions + settings."""

    def __init__(self, parent: tk.Tk, app: AppGUI):
        super().__init__(parent)
        self.pack(fill=tk.BOTH, expand=True)
        self.app = app
        self.store = app.credential_store
        self.bw = app.bw_service
        self.onboarding = app.onboarding_logic
        self.transaction_db = app.transaction_db
        self.audit = get_audit_logger()

        self.shared_passphrase = tk.StringVar()
        self.collection_name = tk.StringVar(
            value=self.store.get("collection_name", "Employee Onboarding")
        )
        self.auto_import = tk.BooleanVar(value=self.store.get("auto_import", "false") == "true")
        self.provision_outlook = tk.BooleanVar(
            value=self.store.get("provision_outlook", "true") == "true"
        )
        self.provision_hyatt = tk.BooleanVar(
            value=self.store.get("provision_hyatt", "true") == "true"
        )
        self.provision_marriott = tk.BooleanVar(
            value=self.store.get("provision_marriott", "true") == "true"
        )
        self.local_delete_mode = tk.StringVar(
            value=self.store.get("local_delete_mode", DEFAULT_LOCAL_DELETE_MODE)
        )
        self.bw_shred_mode = tk.StringVar(
            value=self.store.get("bw_shred_mode", DEFAULT_BW_SHRED_MODE)
        )

        self.workflow_step = tk.StringVar(value="ready")
        self.status = tk.StringVar(value="Ready")
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.step_labels: Dict[str, tk.Label] = {}

        self._build()
        self._configure_logging()
        self.after(100, self._poll_log_queue)
        self._refresh_queued_files()
        threading.Thread(target=self._monitor_downloads, daemon=True).start()

    def _build(self):
        header = tk.Frame(self, bg=C["bg"], padx=20, pady=14)
        header.pack(fill=tk.X)
        tk.Label(header, text="􀎡  DOWNLOWd", font=("SF Pro Display", 20, "bold"), fg=C["text"], bg=C["bg"]).pack(
            side=tk.LEFT
        )
        email = self.store.get("bw_email", "")
        tk.Label(
            header,
            text=f"Bitwarden · {email}" if email else "Bitwarden unlocked",
            font=("SF Pro Text", 12),
            fg=C["muted"],
            bg=C["bg"],
        ).pack(side=tk.RIGHT)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        self.work_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.work_tab, text="  Workflow  ")
        self._build_workflow_tab()

        self.tx_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.tx_tab, text="  Cards  ")
        self._build_transactions_tab()

        self.settings_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.settings_tab, text="  Settings  ")
        self._build_settings_tab()

    # --- Workflow tab -------------------------------------------------
    def _build_workflow_tab(self):
        pad = ttk.Frame(self.work_tab, padding=16)
        pad.pack(fill=tk.BOTH, expand=True)

        # Compact but loud drop zone
        drop_outer = tk.Frame(pad, bg=C["accent"], padx=2, pady=2)
        drop_outer.pack(fill=tk.X, pady=(0, 14))
        drop_inner = tk.Frame(drop_outer, bg=C["card"], padx=16, pady=14)
        drop_inner.pack(fill=tk.X)
        self.drop_label = tk.Label(
            drop_inner,
            text="􀈂   Drop HQ-*.txt / HQ-*.rtf here   ·   click to browse",
            bg=C["card"],
            fg=C["text"],
            font=("SF Pro Text", 14, "bold"),
            cursor="hand2",
            pady=8,
        )
        self.drop_label.pack(fill=tk.X)
        self.drop_label.bind("<Button-1>", self._browse_files)
        if _DND_AVAILABLE:
            try:
                self.drop_label.drop_target_register(DND_FILES)
                self.drop_label.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        # Queue + passphrase row
        mid = ttk.Frame(pad)
        mid.pack(fill=tk.X, pady=(0, 14))
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)

        qf = ttk.LabelFrame(mid, text="Queued identity files", padding=10)
        qf.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.queue_list = tk.Listbox(
            qf,
            height=4,
            bg=C["card"],
            fg=C["text"],
            selectbackground=C["accent"],
            relief="flat",
            highlightthickness=0,
            font=("SF Pro Text", 12),
        )
        self.queue_list.pack(fill=tk.BOTH, expand=True)
        ttk.Button(qf, text="Refresh", command=self._refresh_queued_files).pack(anchor="e", pady=(6, 0))

        pf = ttk.LabelFrame(mid, text="Shared employee passphrase", padding=10)
        pf.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ttk.Label(
            pf,
            text="Same passphrase for every login created this run.\nEmployees change it after first sign-in.",
            style="Muted.TLabel",
        ).pack(anchor="w")
        ttk.Entry(pf, textvariable=self.shared_passphrase, show="•").pack(fill=tk.X, pady=(8, 4))
        ttk.Label(
            pf,
            text="Usernames: firstnamelastnameYEAR",
            style="Muted.TLabel",
        ).pack(anchor="w")

        # Icon workflow strip
        steps = ttk.Frame(pad)
        steps.pack(fill=tk.X, pady=(0, 14))
        for i, (key, icon, title, hint) in enumerate(
            [
                ("intake", "􀈃", "Intake", "Queue files"),
                ("bitwarden", "􀎡", "Bitwarden", "Create vault items"),
                ("accounts", "􀉪", "Accounts", "Outlook · Hyatt · Marriott"),
                ("lockdown", "􀎠", "Lockdown", "Dispose local files"),
            ]
        ):
            card = tk.Frame(steps, bg=C["surface"], padx=12, pady=12, highlightbackground=C["border"], highlightthickness=1)
            card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0 if i == 0 else 6, 0))
            tk.Label(card, text=icon, font=("SF Pro Display", 26), fg=C["accent"], bg=C["surface"]).pack()
            tk.Label(card, text=title, font=("SF Pro Text", 12, "bold"), fg=C["text"], bg=C["surface"]).pack()
            lbl = tk.Label(card, text=hint, font=("SF Pro Text", 10), fg=C["muted"], bg=C["surface"])
            lbl.pack()
            self.step_labels[key] = lbl

        # Run
        run_row = ttk.Frame(pad)
        run_row.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(
            run_row,
            text="􀊄  Run full onboarding",
            style="Accent.TButton",
            command=self.run_pipeline,
        ).pack(side=tk.LEFT)
        ttk.Label(run_row, textvariable=self.status, style="Muted.TLabel").pack(side=tk.LEFT, padx=14)

        log_frame = ttk.LabelFrame(pad, text="Activity", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log = scrolledtext.ScrolledText(
            log_frame,
            state="disabled",
            wrap=tk.WORD,
            height=10,
            bg=C["card"],
            fg=C["text"],
            insertbackground=C["text"],
            relief="flat",
            font=("SF Mono", 11),
        )
        self.log.pack(fill=tk.BOTH, expand=True)

    def _set_step(self, key: str, detail: str = "") -> None:
        titles = {
            "intake": "Queue files",
            "convert": "Converting…",
            "bitwarden": "Importing vault…",
            "accounts": "Opening partner signup…",
            "lockdown": "Disposing locals…",
            "done": "Complete",
            "ready": "Ready",
        }
        mapped = {
            "intake": "intake",
            "convert": "intake",
            "bitwarden": "bitwarden",
            "accounts": "accounts",
            "lockdown": "lockdown",
            "done": "lockdown",
        }.get(key, key)
        for k, lbl in self.step_labels.items():
            if k == mapped:
                lbl.configure(text=detail or titles.get(key, "Active"), fg=C["success"])
            else:
                # reset muted
                defaults = {
                    "intake": "Queue files",
                    "bitwarden": "Create vault items",
                    "accounts": "Outlook · Hyatt · Marriott",
                    "lockdown": "Dispose local files",
                }
                lbl.configure(text=defaults.get(k, ""), fg=C["muted"])
        self.workflow_step.set(key)
        self.status.set(f"{key}: {detail}" if detail else key)

    # --- Settings tab -------------------------------------------------
    def _build_settings_tab(self):
        pad = ttk.Frame(self.settings_tab, padding=20)
        pad.pack(fill=tk.BOTH, expand=True)

        ttk.Label(pad, text="Settings", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            pad,
            text="Configure disposal, provisioning, and workspace defaults.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(0, 16))

        # Bitwarden
        bw = ttk.LabelFrame(pad, text="Bitwarden", padding=12)
        bw.pack(fill=tk.X, pady=6)
        ttk.Label(bw, text="Collection name").grid(row=0, column=0, sticky="w")
        ttk.Entry(bw, textvariable=self.collection_name, width=40).grid(
            row=0, column=1, sticky="ew", padx=8, pady=4
        )
        bw.columnconfigure(1, weight=1)

        shred = ttk.LabelFrame(pad, text="Bitwarden item shredding", padding=12)
        shred.pack(fill=tk.X, pady=6)
        ttk.Label(shred, text="After onboarding").grid(row=0, column=0, sticky="w")
        shred_combo = ttk.Combobox(
            shred,
            state="readonly",
            width=42,
            values=list(BW_SHRED_MODES.values()),
        )
        shred_combo.grid(row=0, column=1, sticky="w", padx=8, pady=4)
        current_bw = BW_SHRED_MODES.get(self.bw_shred_mode.get(), BW_SHRED_MODES[DEFAULT_BW_SHRED_MODE])
        shred_combo.set(current_bw)

        def on_bw_shred(_e=None):
            label = shred_combo.get()
            for key, text in BW_SHRED_MODES.items():
                if text == label:
                    self.bw_shred_mode.set(key)
                    break

        shred_combo.bind("<<ComboboxSelected>>", on_bw_shred)
        ttk.Label(
            shred,
            text="Usually keep items. Shred modes are for wipe/rehearsal runs only.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Local disposal
        loc = ttk.LabelFrame(pad, text="Local file disposal", padding=12)
        loc.pack(fill=tk.X, pady=6)
        ttk.Label(loc, text="HQ + temp JSON files").grid(row=0, column=0, sticky="w")
        loc_combo = ttk.Combobox(
            loc,
            state="readonly",
            width=42,
            values=list(LOCAL_DELETE_MODES.values()),
        )
        loc_combo.grid(row=0, column=1, sticky="w", padx=8, pady=4)
        current_loc = LOCAL_DELETE_MODES.get(
            self.local_delete_mode.get(), LOCAL_DELETE_MODES[DEFAULT_LOCAL_DELETE_MODE]
        )
        loc_combo.set(current_loc)

        def on_loc(_e=None):
            label = loc_combo.get()
            for key, text in LOCAL_DELETE_MODES.items():
                if text == label:
                    self.local_delete_mode.set(key)
                    break

        loc_combo.bind("<<ComboboxSelected>>", on_loc)
        ttk.Label(
            loc,
            text=(
                "Best practice on Mac: keep FileVault On. Encrypt-then-shred of temps adds little "
                "when the disk is already encrypted. Overwrite-then-delete is best-effort against "
                "casual recovery; SSDs/APFS may remapped blocks so multi-pass is not a guarantee."
            ),
            style="Muted.TLabel",
            wraplength=640,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # Automation
        auto = ttk.LabelFrame(pad, text="Automation", padding=12)
        auto.pack(fill=tk.X, pady=6)
        ttk.Checkbutton(
            auto, text="Auto-run pipeline when new HQ files appear in Downloads", variable=self.auto_import
        ).pack(anchor="w")
        ttk.Checkbutton(auto, text="Provision Outlook (clipboard + browser)", variable=self.provision_outlook).pack(
            anchor="w"
        )
        ttk.Checkbutton(
            auto, text="Provision Hyatt (Selenium prefill + browser)", variable=self.provision_hyatt
        ).pack(anchor="w")
        ttk.Checkbutton(
            auto, text="Provision Marriott (Selenium prefill + browser)", variable=self.provision_marriott
        ).pack(anchor="w")

        # Actions
        actions = ttk.Frame(pad)
        actions.pack(fill=tk.X, pady=16)
        ttk.Button(actions, text="Save settings", style="Accent.TButton", command=self._save_settings).pack(
            side=tk.LEFT
        )
        ttk.Button(actions, text="Open Bitwarden.com", command=lambda: webbrowser.open("https://bitwarden.com")).pack(
            side=tk.LEFT, padx=8
        )

    def _save_settings(self):
        self.store.update(
            {
                "collection_name": self.collection_name.get().strip() or "Employee Onboarding",
                "auto_import": "true" if self.auto_import.get() else "false",
                "provision_outlook": "true" if self.provision_outlook.get() else "false",
                "provision_hyatt": "true" if self.provision_hyatt.get() else "false",
                "provision_marriott": "true" if self.provision_marriott.get() else "false",
                "local_delete_mode": self.local_delete_mode.get(),
                "bw_shred_mode": self.bw_shred_mode.get(),
            }
        )
        self.audit.log_config_change("settings", "updated", "saved")
        messagebox.showinfo("Saved", "Settings stored in Keychain.", parent=self)

    # --- Transactions (compact) ---------------------------------------
    def _build_transactions_tab(self):
        pad = ttk.Frame(self.tx_tab, padding=16)
        pad.pack(fill=tk.BOTH, expand=True)

        entry = ttk.LabelFrame(pad, text="Add card spend", padding=10)
        entry.pack(fill=tk.X)
        self.trans_date = tk.StringVar()
        self.trans_amount = tk.StringVar()
        self.trans_merchant = tk.StringVar()
        self.trans_employee = tk.StringVar()
        ttk.Label(entry, text="Date").grid(row=0, column=0, sticky="w")
        ttk.Entry(entry, textvariable=self.trans_date, width=12).grid(row=0, column=1, padx=4)
        ttk.Label(entry, text="Amount").grid(row=0, column=2, sticky="w")
        ttk.Entry(entry, textvariable=self.trans_amount, width=10).grid(row=0, column=3, padx=4)
        ttk.Label(entry, text="Merchant").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(entry, textvariable=self.trans_merchant, width=20).grid(row=1, column=1, padx=4)
        ttk.Label(entry, text="Employee").grid(row=1, column=2, sticky="w")
        self.employee_combo = ttk.Combobox(entry, textvariable=self.trans_employee, width=18)
        self.employee_combo.grid(row=1, column=3, padx=4)
        ttk.Button(entry, text="Add", command=self._add_transaction).grid(row=0, column=4, rowspan=2, padx=8)

        list_frame = ttk.LabelFrame(pad, text="Recent", padding=8)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=10)
        cols = ("date", "merchant", "amount", "employee")
        self.trans_tree = ttk.Treeview(list_frame, columns=cols, show="headings", height=12)
        for c, t, w in (
            ("date", "Date", 100),
            ("merchant", "Merchant", 220),
            ("amount", "Amount", 80),
            ("employee", "Employee", 160),
        ):
            self.trans_tree.heading(c, text=t)
            self.trans_tree.column(c, width=w)
        self.trans_tree.pack(fill=tk.BOTH, expand=True)
        btns = ttk.Frame(pad)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Refresh", command=self._refresh_transaction_list).pack(side=tk.LEFT)
        ttk.Button(btns, text="Export CSV", command=self._export_transactions).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="Delete selected", command=self._delete_selected_transaction).pack(side=tk.RIGHT)
        self._refresh_employee_list()
        self._refresh_transaction_list()

    # --- Queue / files ------------------------------------------------
    def _queued_employee_files(self) -> List[Path]:
        return sorted(
            f for f in DOWNLOADS.glob("HQ-*") if f.is_file() and f.suffix in {".txt", ".rtf"}
        )

    def _refresh_queued_files(self) -> None:
        if not hasattr(self, "queue_list"):
            return
        queued = self._queued_employee_files()
        self.queue_list.delete(0, tk.END)
        if queued:
            for f in queued:
                self.queue_list.insert(tk.END, f.name)
            self.status.set(f"{len(queued)} file(s) queued")
            self._set_step("intake", f"{len(queued)} ready")
        else:
            self.queue_list.insert(tk.END, "No HQ files queued")
            self.status.set("No files queued")

    def _browse_files(self, _event: tk.Event | None = None):
        files = filedialog.askopenfilenames(
            title="Select employee files",
            filetypes=[("HQ exports", "*.txt *.rtf"), ("All", "*.*")],
        )
        if files:
            self._queue_files(files)

    def _on_drop(self, event: Any) -> None:
        files = self.app.root.splitlist(event.data)
        self._queue_files(files)

    def _queue_files(self, files: Tuple[str, ...]):
        n = 0
        for file_path in files:
            path = Path(file_path)
            if not (path.name.startswith("HQ-") and path.suffix in {".txt", ".rtf"}):
                self.log_msg(f"Skipped (not HQ export): {path.name}")
                continue
            dest = DOWNLOADS / path.name
            try:
                shutil.copy2(path, dest)
                n += 1
                self.log_msg(f"Queued {path.name}")
            except Exception as e:
                self.log_msg(f"Queue error {path.name}: {e}")
        self._refresh_queued_files()
        if n and self.auto_import.get():
            self.run_pipeline()

    def _monitor_downloads(self):
        seen: Set[Path] = set(self._queued_employee_files())
        while True:
            try:
                for f in self._queued_employee_files():
                    if f not in seen:
                        seen.add(f)
                        self.log_msg(f"Detected {f.name}")
                        self.after(0, self._refresh_queued_files)
                        if self.auto_import.get():
                            self.after(0, self.run_pipeline)
                time.sleep(5)
            except Exception as e:
                self.log_msg(f"Monitor error: {e!r}")
                time.sleep(10)

    # --- Pipeline -----------------------------------------------------
    def run_pipeline(self):
        queued = self._queued_employee_files()
        if not queued:
            messagebox.showinfo(
                "Nothing queued",
                "Drop or browse HQ-*.txt / HQ-*.rtf files first.",
                parent=self,
            )
            return
        passphrase = self.shared_passphrase.get()
        if not passphrase or len(passphrase) < 8:
            messagebox.showerror(
                "Passphrase required",
                "Enter a shared employee passphrase (8+ characters).\n"
                "Every new login uses this exact passphrase.",
                parent=self,
            )
            return
        collection = self.collection_name.get().strip() or "Employee Onboarding"
        self.store.update({"collection_name": collection})

        config = OnboardingConfig(
            bw=BitwardenConfig(collection_name=collection),
            local_delete_mode=self.local_delete_mode.get(),
            bw_shred_mode=self.bw_shred_mode.get(),
            provision_outlook=self.provision_outlook.get(),
            provision_hyatt=self.provision_hyatt.get(),
            provision_marriott=self.provision_marriott.get(),
        )

        def on_progress(step: str, detail: str = ""):
            self.app.root.after(0, lambda: self._set_step(step, detail))

        def worker():
            try:
                # Session should already exist from startup; refresh if locked
                status = self.bw.get_status()
                if status == "locked" or (status == "unlocked" and not self.bw.session_key):
                    self.app.root.after(
                        0,
                        lambda: messagebox.showerror(
                            "Bitwarden locked",
                            "Vault locked. Quit and reopen DOWNLOWd to sign in again.",
                            parent=self,
                        ),
                    )
                    return
                if status == "unauthenticated":
                    self.app.root.after(
                        0,
                        lambda: messagebox.showerror(
                            "Not signed in",
                            "Bitwarden session missing. Restart DOWNLOWd and sign in.",
                            parent=self,
                        ),
                    )
                    return

                self.onboarding.run(
                    DOWNLOADS,
                    passphrase,
                    config,
                    session_log_path=self.app.session_log_path,
                    progress_callback=on_progress,
                )
                self.app.root.after(0, self._refresh_queued_files)
                self.app.root.after(0, self._refresh_employee_list)
                self.app.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "Complete",
                        "Onboarding finished.\n\n"
                        "Bitwarden items created.\n"
                        "Partner signup windows opened for autofill where possible.\n"
                        "Local HQ/temp files disposed per Settings.",
                        parent=self,
                    ),
                )
            except Exception as e:
                logging.error("Pipeline failed", exc_info=True)
                self.app.root.after(
                    0, lambda: messagebox.showerror("Pipeline failed", str(e), parent=self)
                )
                self.app.root.after(0, lambda: self.status.set(f"Failed: {e}"))

        self.status.set("Running…")
        threading.Thread(target=worker, daemon=True).start()

    # --- Logging ------------------------------------------------------
    def _configure_logging(self):
        handler = QueueHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(handler)

    def _poll_log_queue(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_msg(msg)
        self.after(120, self._poll_log_queue)

    def log_msg(self, msg: str):
        if not hasattr(self, "log"):
            return
        self.log.configure(state="normal")
        self.log.insert(tk.END, msg + "\n")
        self.log.configure(state="disabled")
        self.log.see(tk.END)

    # --- Transactions helpers -----------------------------------------
    def _refresh_employee_list(self):
        names = self.transaction_db.get_employee_names()
        self.employee_combo["values"] = names

    def _refresh_transaction_list(self):
        for item in self.trans_tree.get_children():
            self.trans_tree.delete(item)
        for trans in self.transaction_db.get_all_transactions()[:50]:
            self.trans_tree.insert(
                "",
                "end",
                iid=str(trans["id"]),
                values=(
                    trans["date"],
                    trans["merchant"],
                    f"${trans['amount']:.2f}",
                    trans["employee_name"],
                ),
            )

    def _add_transaction(self):
        date = self.trans_date.get().strip()
        amount_str = self.trans_amount.get().strip()
        merchant = self.trans_merchant.get().strip()
        employee = self.trans_employee.get().strip()
        if not all([date, amount_str, merchant, employee]):
            messagebox.showerror("Error", "All fields required.", parent=self)
            return
        try:
            amount = float(amount_str)
        except ValueError:
            messagebox.showerror("Error", "Amount must be a number.", parent=self)
            return
        card_number = f"****-{employee[-4:]}" if len(employee) >= 4 else "****-****"
        if self.transaction_db.add_transaction(date, amount, merchant, employee, card_number):
            self.audit.log_transaction_added(employee, amount, merchant)
            self._refresh_transaction_list()
            self._refresh_employee_list()
        else:
            messagebox.showerror("Error", "Failed to add transaction.", parent=self)

    def _export_transactions(self):
        import csv

        transactions = self.transaction_db.get_all_transactions()
        if not transactions:
            messagebox.showinfo("Export", "No transactions.", parent=self)
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Date", "Amount", "Merchant", "Employee", "Card"])
            for t in transactions:
                w.writerow([t["date"], t["amount"], t["merchant"], t["employee_name"], t["card_number"]])
        messagebox.showinfo("Export", f"Wrote {len(transactions)} rows.", parent=self)

    def _delete_selected_transaction(self):
        sel = self.trans_tree.selection()
        if not sel:
            messagebox.showerror("Error", "Select a row.", parent=self)
            return
        if not messagebox.askyesno("Confirm", "Delete selected transaction?", parent=self):
            return
        try:
            txn_id = int(sel[0])
        except ValueError:
            messagebox.showerror("Error", "Bad row id.", parent=self)
            return
        if self.transaction_db.delete_transaction(txn_id):
            self.audit.log_deletion("transaction", str(txn_id), method="manual")
            self._refresh_transaction_list()
        else:
            messagebox.showerror("Error", "Delete failed.", parent=self)


if __name__ == "__main__":
    AppGUI().run()
