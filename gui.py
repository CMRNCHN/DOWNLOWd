#!/usr/bin/env python3
"""
DOWNLOWd — secure employee onboarding appliance GUI.

Startup: Keychain-backed app password, then Bitwarden login.
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
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, TextIO, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

import customtkinter as ctk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND_AVAILABLE = True
except Exception:
    DND_FILES = None  # type: ignore
    TkinterDnD = None  # type: ignore
    _DND_AVAILABLE = False

from audit_logger import get_audit_logger
from data_retention import DataRetentionManager
from employee_profiles import (
    EMPLOYEE_ID_FIELD,
    RECORD_ROLE_FIELD,
    EmployeeProfileStore,
    ProfileSyncService,
    RECORD_ROLES,
)
from integrations import BitwardenService, CredentialStore, SessionManager
from onboarding import BitwardenConfig, Onboarding, OnboardingConfig
from secure_delete import (
    BW_SHRED_MODES,
    DEFAULT_BW_SHRED_MODE,
    DEFAULT_LOCAL_DELETE_MODE,
    LOCAL_DELETE_MODES,
)
from transaction_db import TransactionDatabase

DOWNLOADS = Path.home() / "Downloads"

# Quiet zinc workspace palette: paper-white surfaces on a cool gray desktop.
C = {
    "bg": "#f4f4f5",
    "surface": "#fafafa",
    "card": "#ffffff",
    "card_hi": "#f4f4f5",
    "border": "#e4e4e7",
    "text": "#18181b",
    "muted": "#71717a",
    "accent": "#18181b",
    "accent_dim": "#e4e4e7",
    "success": "#16a34a",
    "warn": "#ca8a04",
    "danger": "#dc2626",
}

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")


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
    style.configure(".", background=C["card"], foreground=C["text"], fieldbackground=C["card"])
    style.configure("TFrame", background=C["card"])
    style.configure("Card.TFrame", background=C["surface"])
    style.configure("Surface.TFrame", background=C["surface"])
    style.configure("TLabel", background=C["card"], foreground=C["text"], font=("SF Pro Text", 13))
    style.configure("Muted.TLabel", background=C["card"], foreground=C["muted"], font=("SF Pro Text", 12))
    style.configure("Title.TLabel", background=C["card"], foreground=C["text"], font=("SF Pro Display", 22, "bold"))
    style.configure("Subtitle.TLabel", background=C["card"], foreground=C["muted"], font=("SF Pro Text", 13))
    style.configure("CardTitle.TLabel", background=C["surface"], foreground=C["text"], font=("SF Pro Text", 12, "bold"))
    style.configure("CardMuted.TLabel", background=C["surface"], foreground=C["muted"], font=("SF Pro Text", 11))
    style.configure("Icon.TLabel", background=C["surface"], foreground=C["accent"], font=("SF Pro Display", 28))
    style.configure("Drop.TLabel", background=C["card"], foreground=C["text"], font=("SF Pro Text", 13, "bold"))
    style.configure("TButton", background=C["surface"], foreground=C["text"], padding=(14, 8), font=("SF Pro Text", 12))
    style.map("TButton", background=[("active", C["card_hi"])])
    style.configure(
        "Accent.TButton",
        background=C["text"],
        foreground="#ffffff",
        padding=(16, 10),
        font=("SF Pro Text", 13, "bold"),
    )
    style.map("Accent.TButton", background=[("active", "#3f3f46")])
    style.configure("TEntry", fieldbackground=C["card"], foreground=C["text"], insertcolor=C["text"])
    style.configure("TCheckbutton", background=C["card"], foreground=C["text"], font=("SF Pro Text", 12))
    style.configure("TLabelframe", background=C["card"], foreground=C["text"], bordercolor=C["border"])
    style.configure("TLabelframe.Label", background=C["card"], foreground=C["muted"], font=("SF Pro Text", 11, "bold"))
    style.configure("TNotebook", background=C["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", background=C["surface"], foreground=C["muted"], padding=(16, 8))
    style.map("TNotebook.Tab", background=[("selected", C["card"])], foreground=[("selected", C["text"])])
    style.configure("TCombobox", fieldbackground=C["card"], foreground=C["text"], background=C["card"])
    style.configure(
        "Treeview",
        background=C["card"],
        foreground=C["text"],
        fieldbackground=C["card"],
        rowheight=42,
        borderwidth=0,
        font=("SF Pro Text", 11),
    )
    style.configure(
        "Treeview.Heading",
        background=C["text"],
        foreground="#ffffff",
        borderwidth=0,
        padding=(10, 11),
        font=("SF Pro Text", 10, "bold"),
    )
    style.map(
        "Treeview.Heading",
        background=[("active", C["text"])],
        foreground=[("active", "#ffffff")],
    )
    return style


class CompletionRing(tk.Canvas):
    """Small antialiased-looking progress ring for employee cards."""

    def __init__(self, master: Any, percent: int, size: int = 48):
        super().__init__(
            master,
            width=size,
            height=size,
            bg=C["card"],
            highlightthickness=0,
            borderwidth=0,
        )
        inset = 5
        self.create_oval(
            inset,
            inset,
            size - inset,
            size - inset,
            outline="#dedee3",
            width=4,
        )
        if percent:
            self.create_arc(
                inset,
                inset,
                size - inset,
                size - inset,
                start=90,
                extent=-(360 * min(percent, 100) / 100),
                style=tk.ARC,
                outline=C["text"],
                width=4,
            )
        self.create_text(
            size / 2,
            size / 2,
            text=str(percent),
            fill=C["text"],
            font=("SF Pro Text", 9, "bold"),
        )


class BrandGlyph(tk.Canvas):
    """Compact layered-vault mark drawn as crisp vector lines."""

    def __init__(self, master: Any, size: int = 30):
        super().__init__(
            master,
            width=size,
            height=size,
            bg=C["text"],
            highlightthickness=0,
            borderwidth=0,
        )
        center = size / 2
        for offset in (-5, 0, 5):
            y = center + offset
            self.create_polygon(
                center,
                y - 5,
                center + 10,
                y,
                center,
                y + 5,
                center - 10,
                y,
                outline="#ffffff",
                fill="",
                width=1.5,
                joinstyle=tk.ROUND,
            )


class AppPasswordDialog(tk.Toplevel):
    """Create or verify the local application password."""

    def __init__(self, parent: tk.Tk, session_manager: SessionManager):
        super().__init__(parent)
        self.session_manager = session_manager
        self.audit = get_audit_logger()
        self.success = False
        self.setup_mode = not session_manager.has_password()

        self.title("Set DOWNLOWd Password" if self.setup_mode else "Unlock DOWNLOWd")
        self.geometry("440x410+200+160")
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
        self.audit.log_authentication(False, method="app_password_cancelled")
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def _build_ui(self):
        frame = tk.Frame(self, bg=C["bg"], padx=28, pady=24)
        frame.pack(fill=tk.BOTH, expand=True)
        title = "Create app password" if self.setup_mode else "Unlock DOWNLOWd"
        subtitle = (
            "Protect local onboarding controls with a separate password."
            if self.setup_mode
            else "Enter your DOWNLOWd app password to continue."
        )
        tk.Label(
            frame,
            text=title,
            font=("SF Pro Display", 24, "bold"),
            fg=C["text"],
            bg=C["bg"],
        ).pack(pady=(12, 4))
        tk.Label(
            frame,
            text=subtitle,
            font=("SF Pro Text", 12),
            fg=C["muted"],
            bg=C["bg"],
            wraplength=360,
        ).pack(pady=(0, 20))

        form = tk.Frame(frame, bg=C["bg"])
        form.pack(fill=tk.X)
        form.columnconfigure(0, weight=1)
        tk.Label(
            form,
            text="App Password",
            fg=C["muted"],
            bg=C["bg"],
            font=("SF Pro Text", 11),
        ).grid(row=0, column=0, sticky="w")
        password_var = tk.StringVar()
        password_entry = ttk.Entry(form, textvariable=password_var, show="•")
        password_entry.grid(row=1, column=0, sticky="ew", pady=(2, 12))

        confirm_var = tk.StringVar()
        if self.setup_mode:
            tk.Label(
                form,
                text="Confirm Password",
                fg=C["muted"],
                bg=C["bg"],
                font=("SF Pro Text", 11),
            ).grid(row=2, column=0, sticky="w")
            ttk.Entry(form, textvariable=confirm_var, show="•").grid(
                row=3,
                column=0,
                sticky="ew",
                pady=(2, 12),
            )

        status_var = tk.StringVar()
        tk.Label(
            form,
            textvariable=status_var,
            fg=C["danger"],
            bg=C["bg"],
            font=("SF Pro Text", 11),
        ).grid(row=4, column=0, sticky="w", pady=(2, 8))

        def submit():
            password = password_var.get()
            if self.setup_mode:
                if len(password) < 8:
                    status_var.set("Use at least 8 characters.")
                    return
                if password != confirm_var.get():
                    status_var.set("Passwords do not match.")
                    return
                accepted = self.session_manager.set_password(password)
                if accepted:
                    accepted = self.session_manager.create_session(password)
                method = "app_password_setup"
            else:
                accepted = self.session_manager.create_session(password)
                method = "app_password"

            self.audit.log_authentication(accepted, method=method)
            if not accepted:
                status_var.set("Incorrect password or Keychain storage failed.")
                password_var.set("")
                return
            self.success = True
            try:
                self.grab_release()
            except tk.TclError:
                pass
            self.destroy()

        ttk.Button(
            frame,
            text="Create Password" if self.setup_mode else "Unlock",
            style="Accent.TButton",
            command=submit,
        ).pack(fill=tk.X, pady=(12, 8))
        password_entry.bind("<Return>", lambda _event: submit())
        password_entry.focus()


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
        self.bw_service.clear_session()
        self.audit.log_authentication(False, method="bitwarden_cancelled")
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
                        self.bw_service.clear_session()
                        self.audit.log_authentication(False, method="bitwarden_2fa_cancelled")
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
        self.root.geometry("960x720+80+40")
        self.root.minsize(760, 600)
        self.session_log_path: Path | None = None
        apply_theme(self.root)

        self.credential_store = CredentialStore()
        self.session_manager = SessionManager(self.credential_store)
        self.bw_service = BitwardenService()
        self.transaction_db = TransactionDatabase()
        self.profile_store = EmployeeProfileStore()
        self.profile_sync = ProfileSyncService(self.bw_service, self.profile_store)
        self.audit = get_audit_logger()
        self.retention_manager = DataRetentionManager(
            self.transaction_db,
            prompt_callback=self._queue_retention_prompt,
            profile_sync=self.profile_sync,
        )
        self.profile_store.migrate_retention(self.retention_manager.retention_data)
        for profile in self.profile_store.list_profiles(include_purged=True):
            self.transaction_db.link_employee(
                profile.get("display_name", ""),
                profile["employee_id"],
            )
        self.onboarding_logic = Onboarding(
            self.bw_service,
            retention_manager=self.retention_manager,
            profile_store=self.profile_store,
            profile_sync=self.profile_sync,
        )
        self._pending_retention_prompts: queue.Queue = queue.Queue()
        self._setup_file_logging()
        self._auth_ok = False
        self.root.protocol("WM_DELETE_WINDOW", self._shutdown)

        # Keep root off-screen during authentication gates (avoid blank second window)
        self.root.geometry("1x1-10000-10000")
        self.root.deiconify()
        self.root.update_idletasks()

        if not self.session_manager.is_authenticated():
            app_dialog = AppPasswordDialog(self.root, self.session_manager)
            app_dialog.wait_window()
            if not app_dialog.success:
                self._abort_startup()
                return

        if self.bw_service.session_key and self._bw_ready():
            self._auth_ok = True
            self._post_auth()
        else:
            dialog = BitwardenLoginDialog(
                self.root, self.bw_service, self.credential_store, self._on_auth_success
            )
            dialog.wait_window()
            if not self._auth_ok:
                self._abort_startup()

    def _abort_startup(self):
        try:
            if self.root.winfo_exists():
                self.root.destroy()
        except tk.TclError:
            pass

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
        self.root.after(250, self._drain_retention_prompts)
        self.root.after(500, self._warn_if_filevault_off)
        if self.credential_store.get("sync_on_startup", "true") == "true":
            self.root.after(750, self.dashboard._sync_profiles)
        self.root.after(60_000, self._enforce_app_session)

    def _enforce_app_session(self):
        if not self.session_manager.is_authenticated():
            self.audit.log_authentication(False, method="app_session_expired")
            if hasattr(self, "dashboard"):
                self.dashboard._clear_profile_secrets()
            self.bw_service.clear_session()
            self.retention_manager.stop_scheduler()
            messagebox.showwarning(
                "Session expired",
                "Your one-hour DOWNLOWd session expired. Reopen the app to authenticate again.",
                parent=self.root,
            )
            try:
                self.root.destroy()
            except tk.TclError:
                pass
            return
        self.root.after(60_000, self._enforce_app_session)

    def _shutdown(self):
        self.retention_manager.stop_scheduler()
        self.bw_service.clear_session()
        self.audit.log_security_event("session_closed", "Application window closed")
        try:
            self.root.destroy()
        except tk.TclError:
            pass

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

    def _drain_retention_prompts(self):
        while True:
            try:
                action = self._pending_retention_prompts.get_nowait()
            except queue.Empty:
                break
            self._show_retention_prompt(action)
        try:
            if self.root.winfo_exists():
                self.root.after(250, self._drain_retention_prompts)
        except tk.TclError:
            pass

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
        self.root.geometry("960x720+80+40")
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
        self.profile_store = app.profile_store
        self.profile_sync = app.profile_sync
        self.audit = get_audit_logger()

        self.shared_passphrase = tk.StringVar()
        self.collection_name = tk.StringVar(
            value=self.store.get("collection_name", "Personal Vault")
        )
        self.auto_import = tk.BooleanVar(value=self.store.get("auto_import", "false") == "true")
        self.sync_on_startup = tk.BooleanVar(
            value=self.store.get("sync_on_startup", "true") == "true"
        )
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
        self._pipeline_running = False
        self.selected_employee: Optional[str] = None
        self.selected_profile_id: Optional[str] = None
        self.selected_record_role = "identity"
        self.profile_bundle: Dict[str, Dict[str, Any]] = {}
        self._revealed_profile_values: Set[Tuple[str, str]] = set()

        self._build()
        self._configure_logging()
        self.after(100, self._poll_log_queue)
        self._refresh_queued_files()
        threading.Thread(target=self._monitor_downloads, daemon=True).start()

    def _build(self):
        shell = ctk.CTkFrame(self, fg_color=C["bg"], corner_radius=0)
        shell.pack(fill=tk.BOTH, expand=True)
        workspace = ctk.CTkFrame(
            shell,
            fg_color=C["card"],
            corner_radius=0,
        )
        workspace.pack(fill=tk.BOTH, expand=True)

        topbar = ctk.CTkFrame(
            workspace,
            fg_color="transparent",
            corner_radius=0,
            height=78,
        )
        topbar.pack(fill=tk.X, padx=26, pady=(11, 9))
        topbar.pack_propagate(False)
        topbar.grid_columnconfigure(0, weight=1)
        topbar.grid_columnconfigure(2, weight=1)
        brand = ctk.CTkFrame(topbar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="w")
        mark = ctk.CTkFrame(
            brand,
            width=40,
            height=40,
            corner_radius=12,
            fg_color=C["text"],
        )
        mark.pack(side=tk.LEFT, pady=8)
        mark.pack_propagate(False)
        BrandGlyph(mark, size=28).pack(expand=True)
        ctk.CTkLabel(
            brand,
            text="DOWNLOWd",
            font=("Avenir Next", 20, "bold"),
            text_color=C["text"],
        ).pack(side=tk.LEFT, padx=(14, 9))
        ctk.CTkLabel(
            brand,
            text="●  live",
            font=("SF Pro Text", 11),
            text_color=C["muted"],
        ).pack(side=tk.LEFT)

        self.context_action = ctk.CTkButton(
            topbar,
            text="＋  Add",
            command=self._run_context_action,
            width=108,
            height=46,
            corner_radius=15,
            border_width=0,
            fg_color=C["text"],
            hover_color="#323238",
            text_color="#ffffff",
            font=("SF Pro Text", 13, "bold"),
            cursor="hand2",
        )
        self.context_action.grid(row=0, column=2, sticky="e", pady=5)

        nav_shell = ctk.CTkFrame(
            topbar,
            fg_color=C["card_hi"],
            corner_radius=15,
            border_width=0,
        )
        nav_shell.grid(row=0, column=1, pady=5)
        self.nav_buttons: Dict[str, ctk.CTkButton] = {}
        for key, icon, label in (
            ("onboarding", "✦", "Onboarding"),
            ("profiles", "◎", "Profiles"),
            ("ledger", "▤", "Ledger"),
            ("settings", "⌁", "Settings"),
        ):
            button = ctk.CTkButton(
                nav_shell,
                text=f"{icon} {label}",
                command=lambda view=key: self._show_view(view),
                width=90 if key != "onboarding" else 118,
                height=40,
                corner_radius=12,
                border_width=0,
                fg_color="transparent",
                hover_color="#e7e7ea",
                text_color=C["muted"],
                font=("SF Pro Text", 11, "bold"),
                cursor="hand2",
            )
            button.pack(side=tk.LEFT, padx=2, pady=3)
            self.nav_buttons[key] = button

        ctk.CTkFrame(
            workspace,
            height=1,
            fg_color=C["border"],
            corner_radius=0,
        ).pack(fill=tk.X)
        content = ctk.CTkFrame(workspace, fg_color=C["card"], corner_radius=0)
        content.pack(fill=tk.BOTH, expand=True)
        self.work_tab = ctk.CTkFrame(content, fg_color=C["card"], corner_radius=0)
        self.profiles_tab = ctk.CTkFrame(content, fg_color=C["card"], corner_radius=0)
        self.tx_tab = ctk.CTkFrame(content, fg_color=C["card"], corner_radius=0)
        self.settings_tab = ctk.CTkFrame(content, fg_color=C["card"], corner_radius=0)
        self.views = {
            "onboarding": self.work_tab,
            "profiles": self.profiles_tab,
            "ledger": self.tx_tab,
            "settings": self.settings_tab,
        }
        for view in self.views.values():
            view.place(x=0, y=0, relwidth=1, relheight=1)

        self._build_workflow_tab()
        self._build_profiles_tab()
        self._build_transactions_tab()
        self._build_settings_tab()
        self._show_view("onboarding")

    def _show_view(self, view_name: str):
        if getattr(self, "current_view", None) == "profiles" and view_name != "profiles":
            self._clear_profile_secrets()
        self.current_view = view_name
        self.views[view_name].tkraise()
        if view_name == "profiles":
            self._refresh_profiles_list()
        for key, button in self.nav_buttons.items():
            button.configure(
                fg_color=C["text"] if key == view_name else "transparent",
                text_color="#ffffff" if key == view_name else C["muted"],
            )
        labels = {
            "onboarding": "＋  Add",
            "profiles": "↻  Sync",
            "ledger": "⇩  Export",
            "settings": "✓  Save",
        }
        self.context_action.configure(text=labels[view_name])

    def _run_context_action(self):
        if self.current_view == "onboarding":
            self._browse_files()
        elif self.current_view == "profiles":
            self._sync_profiles()
        elif self.current_view == "ledger":
            self._export_transactions()
        else:
            self._save_settings()

    # --- Profiles ----------------------------------------------------
    def _build_profiles_tab(self):
        shell = ctk.CTkFrame(self.profiles_tab, fg_color=C["card"], corner_radius=0)
        shell.pack(fill=tk.BOTH, expand=True, padx=24, pady=20)
        shell.grid_columnconfigure(1, weight=1)
        shell.grid_rowconfigure(0, weight=1)

        rail = ctk.CTkFrame(
            shell,
            fg_color=C["surface"],
            width=248,
            corner_radius=20,
            border_color=C["border"],
            border_width=1,
        )
        rail.grid(row=0, column=0, sticky="nsw", padx=(0, 14))
        rail.grid_propagate(False)
        ctk.CTkLabel(
            rail,
            text="EMPLOYEE PROFILES",
            text_color=C["text"],
            font=("SF Pro Text", 12, "bold"),
        ).pack(anchor="w", padx=16, pady=(16, 10))
        self.profile_search = tk.StringVar()
        search = ctk.CTkEntry(
            rail,
            textvariable=self.profile_search,
            placeholder_text="Search employees",
            height=36,
            corner_radius=10,
            border_color=C["border"],
            border_width=1,
            fg_color=C["card"],
            font=("SF Pro Text", 11),
        )
        search.pack(fill=tk.X, padx=14, pady=(0, 10))
        search.bind("<KeyRelease>", lambda _event: self._refresh_profiles_list())
        self.profile_list = tk.Listbox(
            rail,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            activestyle="none",
            bg=C["surface"],
            fg=C["text"],
            selectbackground=C["text"],
            selectforeground="#ffffff",
            font=("SF Pro Text", 10, "bold"),
        )
        self.profile_list.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 14))
        self.profile_list.bind("<<ListboxSelect>>", self._on_profile_selected)
        self._profile_list_ids: List[str] = []

        detail = ctk.CTkFrame(
            shell,
            fg_color=C["card"],
            corner_radius=22,
            border_color=C["border"],
            border_width=1,
        )
        detail.grid(row=0, column=1, sticky="nsew")
        detail.grid_columnconfigure(0, weight=1)
        detail.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(detail, fg_color="transparent", corner_radius=0)
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(20, 4))
        self.profile_title = tk.StringVar(value="Select an employee")
        self.profile_subtitle = tk.StringVar(value="Live Bitwarden records load on selection")
        ctk.CTkLabel(
            header,
            textvariable=self.profile_title,
            text_color=C["text"],
            font=("SF Pro Display", 20, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            header,
            textvariable=self.profile_subtitle,
            text_color=C["muted"],
            font=("SF Pro Text", 11),
        ).pack(anchor="w", pady=(2, 10))

        self.record_rail = ctk.CTkFrame(
            detail,
            fg_color=C["card_hi"],
            corner_radius=14,
        )
        self.record_rail.grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 12))
        self.record_buttons: Dict[str, ctk.CTkButton] = {}
        labels = {
            "identity": "Identity",
            "email_login": "Email Login",
            "hyatt_login": "Hyatt",
            "marriott_login": "Marriott",
            "work_card": "Work Card",
        }
        for role in RECORD_ROLES:
            button = ctk.CTkButton(
                self.record_rail,
                text=labels[role],
                command=lambda selected=role: self._show_profile_record(selected),
                height=34,
                width=88,
                corner_radius=11,
                border_width=0,
                fg_color="transparent",
                hover_color="#e3e3e6",
                text_color=C["muted"],
                font=("SF Pro Text", 10, "bold"),
                cursor="hand2",
            )
            button.pack(side=tk.LEFT, padx=3, pady=3)
            self.record_buttons[role] = button

        self.profile_viewer = ctk.CTkFrame(
            detail,
            fg_color=C["surface"],
            corner_radius=18,
            border_color=C["border"],
            border_width=1,
        )
        self.profile_viewer.grid(
            row=2,
            column=0,
            sticky="nsew",
            padx=22,
            pady=(0, 12),
        )

        footer = ctk.CTkFrame(detail, fg_color="transparent", corner_radius=0)
        footer.grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 18))
        self.profile_edit_button = ctk.CTkButton(
            footer,
            text="Edit identity",
            command=self._edit_selected_identity,
            height=36,
            corner_radius=11,
            fg_color=C["card_hi"],
            hover_color="#dedee2",
            text_color=C["text"],
        )
        self.profile_edit_button.pack(side=tk.LEFT)
        ctk.CTkButton(
            footer,
            text="Resume accounts",
            command=self._resume_profile_accounts,
            height=36,
            corner_radius=11,
            fg_color=C["card_hi"],
            hover_color="#dedee2",
            text_color=C["text"],
        ).pack(side=tk.LEFT, padx=6)
        self.profile_restore_button = ctk.CTkButton(
            footer,
            text="Restore",
            command=self._restore_selected_profile,
            height=36,
            corner_radius=11,
            fg_color=C["text"],
            hover_color="#323238",
        )
        self.profile_restore_button.pack(side=tk.RIGHT)
        self.profile_delete_button = ctk.CTkButton(
            footer,
            text="Delete employee",
            command=self._delete_selected_profile,
            height=36,
            corner_radius=11,
            fg_color="transparent",
            hover_color="#fee2e2",
            text_color=C["danger"],
            border_width=1,
            border_color="#fecaca",
        )
        self.profile_delete_button.pack(side=tk.RIGHT, padx=6)
        self._render_profile_viewer()

    def _refresh_profiles_list(self):
        if not hasattr(self, "profile_list"):
            return
        query = self.profile_search.get().strip().casefold()
        profiles = [
            profile
            for profile in self.profile_store.list_profiles()
            if not query
            or query in profile.get("display_name", "").casefold()
            or query in profile.get("email", "").casefold()
        ]
        selected = self.selected_profile_id
        self.profile_list.delete(0, tk.END)
        self._profile_list_ids = []
        for profile in profiles:
            refs = profile.get("vault_refs") or {}
            count = len(refs)
            suffix = f"  ·  {count}/5"
            deletion = profile.get("deletion") or {}
            if deletion:
                suffix = f"  ·  {deletion.get('status', 'pending')}"
            self.profile_list.insert(tk.END, profile.get("display_name", "Unknown") + suffix)
            self._profile_list_ids.append(profile["employee_id"])
        if selected in self._profile_list_ids:
            index = self._profile_list_ids.index(selected)
            self.profile_list.selection_set(index)
            if not self.profile_bundle:
                self.after_idle(self._on_profile_selected)
        elif self._profile_list_ids:
            self.profile_list.selection_set(0)
            self.profile_list.activate(0)
            self.after_idle(self._on_profile_selected)

    def _on_profile_selected(self, _event=None):
        selection = self.profile_list.curselection()
        if not selection:
            return
        employee_id = self._profile_list_ids[selection[0]]
        if employee_id == self.selected_profile_id and self.profile_bundle:
            return
        self._clear_profile_secrets()
        self.selected_profile_id = employee_id
        profile = self.profile_store.get(employee_id) or {}
        self.profile_title.set(profile.get("display_name", "Employee"))
        self.profile_subtitle.set("Loading live Bitwarden records…")
        self._render_profile_viewer("Loading…")

        def load():
            try:
                bundle = self.profile_sync.get_bundle(employee_id)
                self.after(0, lambda: self._apply_profile_bundle(employee_id, bundle))
            except Exception as exc:
                self.after(0, lambda error=exc: self._profile_load_failed(employee_id, error))

        threading.Thread(target=load, daemon=True).start()

    def _apply_profile_bundle(self, employee_id: str, bundle: Dict[str, Dict[str, Any]]):
        if self.selected_profile_id != employee_id:
            bundle.clear()
            return
        self.profile_bundle = bundle
        profile = self.profile_store.get(employee_id) or {}
        self.profile_subtitle.set(
            f"Synced records {len(bundle)}/5  ·  "
            f"Last update {datetime.now().strftime('%H:%M')}"
        )
        self._show_profile_record(self.selected_record_role)
        self._update_profile_actions(profile)

    def _profile_load_failed(self, employee_id: str, error: Exception):
        if self.selected_profile_id != employee_id:
            return
        self.profile_bundle = {}
        self.profile_subtitle.set("Vault locked or sync unavailable")
        self._render_profile_viewer(str(error))

    def _clear_profile_secrets(self):
        self.profile_bundle.clear()
        self._revealed_profile_values.clear()

    def _update_profile_actions(self, profile: Dict[str, Any]):
        deletion = profile.get("deletion") or {}
        pending = deletion.get("status") in {"pending", "partial", "purge_failed"}
        self.profile_restore_button.configure(state=tk.NORMAL if pending else tk.DISABLED)
        self.profile_delete_button.configure(state=tk.DISABLED if pending else tk.NORMAL)
        self.profile_edit_button.configure(
            state=tk.NORMAL if "identity" in self.profile_bundle and not pending else tk.DISABLED
        )

    def _show_profile_record(self, role: str):
        self.selected_record_role = role
        for key, button in self.record_buttons.items():
            exists = key in self.profile_bundle
            button.configure(
                fg_color=C["text"] if key == role else "transparent",
                text_color="#ffffff"
                if key == role
                else (C["text"] if exists else C["muted"]),
                text=button.cget("text").replace(" · Missing", "")
                + ("" if exists else " · Missing"),
            )
        self._render_profile_viewer()

    @staticmethod
    def _identity_view_rows(item: Dict[str, Any]) -> List[Tuple[str, str, bool]]:
        identity = item.get("identity") or {}
        item_name = str(item.get("name") or "")
        display_name = item_name.rsplit(" — ", 1)[0] if " — " in item_name else item_name
        keys = (
            ("Employee", "_displayName"),
            ("First name", "firstName"),
            ("Middle name", "middleName"),
            ("Last name", "lastName"),
            ("Email", "email"),
            ("Phone", "phone"),
            ("Address", "address1"),
            ("City", "city"),
            ("State", "state"),
            ("Postal code", "postalCode"),
            ("SSN", "ssn"),
        )
        rows = [
            (
                label,
                str(display_name if key == "_displayName" else identity.get(key) or "—"),
                key == "ssn",
            )
            for label, key in keys
        ]
        hidden_fields = {EMPLOYEE_ID_FIELD, RECORD_ROLE_FIELD}
        for field in item.get("fields") or []:
            field_name = str(field.get("name") or "").strip()
            if not field_name or field_name in hidden_fields:
                continue
            value = str(field.get("value") or "—")
            sensitive_name = field_name.casefold()
            is_sensitive = any(
                token in sensitive_name
                for token in (
                    "birth",
                    "dob",
                    "social",
                    "ssn",
                    "passport",
                    "license",
                    "password",
                )
            )
            rows.append((field_name, value, is_sensitive))
        return rows

    def _render_profile_viewer(self, message: Optional[str] = None):
        if not hasattr(self, "profile_viewer"):
            return
        for child in self.profile_viewer.winfo_children():
            child.destroy()
        if message:
            ctk.CTkLabel(
                self.profile_viewer,
                text=message,
                text_color=C["muted"],
                font=("Avenir Next", 11),
                wraplength=520,
                justify="left",
            ).pack(anchor="w", padx=18, pady=18)
            return
        role = self.selected_record_role
        item = self.profile_bundle.get(role)
        if item is None:
            ctk.CTkLabel(
                self.profile_viewer,
                text="NOT CREATED",
                text_color=C["muted"],
                font=("Avenir Next", 11, "bold"),
            ).pack(anchor="w", padx=18, pady=(18, 4))
            ctk.CTkLabel(
                self.profile_viewer,
                text="This record is not bound to a Bitwarden item.",
                text_color=C["muted"],
                font=("Avenir Next", 11),
            ).pack(anchor="w", padx=18)
            return
        if item.get("_load_error"):
            ctk.CTkLabel(
                self.profile_viewer,
                text="RECORD UNAVAILABLE",
                text_color=C["text"],
                font=("Avenir Next", 12, "bold"),
            ).pack(anchor="w", padx=18, pady=(18, 6))
            ctk.CTkLabel(
                self.profile_viewer,
                text=(
                    "This Bitwarden item could not be loaded. Unlock the vault, "
                    "then use Sync to retry."
                ),
                text_color=C["muted"],
                font=("Avenir Next", 11),
                wraplength=480,
                justify="left",
            ).pack(anchor="w", padx=18)
            return

        ctk.CTkLabel(
            self.profile_viewer,
            text=str(item.get("name") or role).upper(),
            text_color=C["text"],
            font=("Avenir Next", 12, "bold"),
        ).pack(anchor="w", padx=18, pady=(16, 10))
        rows: List[Tuple[str, str, bool]] = []
        if role == "identity":
            rows = self._identity_view_rows(item)
        elif role == "work_card":
            card = item.get("card") or {}
            rows = [
                ("Cardholder", str(card.get("cardholderName") or "—"), False),
                ("Brand", str(card.get("brand") or "—"), False),
                ("Number", str(card.get("number") or "—"), True),
                ("CVV", str(card.get("code") or "—"), True),
                (
                    "Expires",
                    f"{card.get('expMonth') or '—'}/{card.get('expYear') or '—'}",
                    False,
                ),
            ]
        else:
            login = item.get("login") or {}
            uris = login.get("uris") or []
            rows = [
                ("Username", str(login.get("username") or "—"), False),
                ("Password", str(login.get("password") or "—"), True),
                ("Website", str(uris[0].get("uri") if uris else "—"), False),
            ]
        content = ctk.CTkFrame(
            self.profile_viewer,
            fg_color="transparent",
            corner_radius=0,
        )
        content.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 14))
        if role == "identity":
            content.grid_columnconfigure(0, weight=1)
            content.grid_columnconfigure(1, weight=1)
        for index, (label, value, sensitive) in enumerate(rows):
            row = ctk.CTkFrame(
                content,
                fg_color="#f1f1f3" if index % 2 == 0 else "#f5f5f6",
                corner_radius=11,
                height=48,
            )
            if role == "identity":
                grid_row, grid_column = divmod(index, 2)
                row.grid(
                    row=grid_row,
                    column=grid_column,
                    sticky="ew",
                    padx=(0 if grid_column == 0 else 4, 4 if grid_column == 0 else 0),
                    pady=4,
                )
            else:
                row.pack(fill=tk.X, pady=4)
            row.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(
                row,
                text=label,
                anchor="w",
                width=88,
                text_color=C["muted"],
                font=("Avenir Next", 9, "bold"),
            ).grid(row=0, column=0, sticky="w", padx=(12, 6), pady=9)
            reveal_key = (role, label)
            shown = value
            if sensitive and reveal_key not in self._revealed_profile_values:
                shown = "••••••••" if value != "—" else "—"
            ctk.CTkLabel(
                row,
                text=shown,
                anchor="w",
                text_color=C["text"],
                font=("SF Mono", 9),
            ).grid(row=0, column=1, sticky="ew", pady=9)
            if sensitive and value != "—":
                ctk.CTkButton(
                    row,
                    text="Hide" if reveal_key in self._revealed_profile_values else "Reveal",
                    command=lambda key=reveal_key: self._toggle_profile_reveal(key),
                    width=54,
                    height=26,
                    corner_radius=8,
                    border_width=0,
                    fg_color=C["card"],
                    hover_color="#e4e4e7",
                    text_color=C["text"],
                    font=("Avenir Next", 8, "bold"),
                ).grid(row=0, column=2, padx=8, pady=6)

    def _toggle_profile_reveal(self, key: Tuple[str, str]):
        if key in self._revealed_profile_values:
            self._revealed_profile_values.remove(key)
        else:
            self._revealed_profile_values.add(key)
        self._render_profile_viewer()

    def _sync_profiles(self):
        self.status.set("Syncing Bitwarden profiles…")
        self._clear_profile_secrets()

        def sync():
            try:
                self.profile_sync.sync_profiles()
                self.after(0, self._profile_sync_complete)
            except Exception as exc:
                self.after(0, lambda error=exc: self._profile_sync_failed(error))

        threading.Thread(target=sync, daemon=True).start()

    def _profile_sync_complete(self):
        self.status.set("Profiles synced")
        self._refresh_profiles_list()
        if self.selected_profile_id:
            self._on_profile_selected()

    def _profile_sync_failed(self, error: Exception):
        self.status.set("Profile sync failed")
        messagebox.showerror("Profile sync", str(error), parent=self)

    def _resume_profile_accounts(self):
        profile = self.profile_store.get(self.selected_profile_id or "")
        if not profile:
            return
        self.selected_employee = profile.get("display_name")
        self._show_view("onboarding")
        self.resume_selected_employee()

    def _edit_selected_identity(self):
        profile = self.profile_store.get(self.selected_profile_id or "")
        item = self.profile_bundle.get("identity")
        if not profile or not item:
            return
        dialog = tk.Toplevel(self)
        dialog.title("Edit identity")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        form = tk.Frame(dialog, padx=16, pady=16)
        form.pack(fill=tk.BOTH, expand=True)
        identity = item.get("identity") or {}
        fields = (
            ("First name", "firstName"),
            ("Middle name", "middleName"),
            ("Last name", "lastName"),
            ("Email", "email"),
            ("Phone", "phone"),
            ("Address", "address1"),
            ("City", "city"),
            ("State", "state"),
            ("Postal code", "postalCode"),
        )
        variables: Dict[str, tk.StringVar] = {}
        for row_index, (label, key) in enumerate(fields):
            tk.Label(form, text=label, anchor="w").grid(
                row=row_index,
                column=0,
                sticky="w",
                padx=(0, 10),
                pady=3,
            )
            variables[key] = tk.StringVar(value=str(identity.get(key) or ""))
            tk.Entry(form, textvariable=variables[key], width=36).grid(
                row=row_index,
                column=1,
                sticky="ew",
                pady=3,
            )

        def save():
            updates = {key: variable.get().strip() for key, variable in variables.items()}
            if not updates["firstName"] or not updates["lastName"]:
                messagebox.showerror(
                    "Identity",
                    "First and last name are required.",
                    parent=dialog,
                )
                return
            dialog.destroy()
            self._save_identity_updates(
                profile["employee_id"],
                updates,
                item.get("revisionDate"),
            )

        tk.Button(form, text="Cancel", command=dialog.destroy).grid(
            row=len(fields),
            column=0,
            pady=(12, 0),
        )
        tk.Button(form, text="Save", command=save, bg=C["text"], fg="#ffffff").grid(
            row=len(fields),
            column=1,
            sticky="e",
            pady=(12, 0),
        )

    def _save_identity_updates(
        self,
        employee_id: str,
        updates: Dict[str, str],
        expected_revision: Optional[str],
    ):
        self.status.set("Saving identity…")

        def save():
            try:
                item = self.profile_sync.edit_identity(
                    employee_id,
                    updates,
                    expected_revision,
                )
                self.after(0, lambda: self._identity_saved(employee_id, item))
            except Exception as exc:
                self.after(
                    0,
                    lambda error=exc: self._identity_save_failed(employee_id, error),
                )

        threading.Thread(target=save, daemon=True).start()

    def _identity_saved(self, employee_id: str, item: Dict[str, Any]):
        if self.selected_profile_id == employee_id:
            self.profile_bundle["identity"] = item
            self._revealed_profile_values.clear()
            self._render_profile_viewer()
        self.status.set("Identity saved")
        self.audit.log_security_event("profile_identity_edit", f"employee_id={employee_id} result=success")

    def _identity_save_failed(self, employee_id: str, error: Exception):
        self.status.set("Identity save failed")
        self.audit.log_security_event(
            "profile_identity_edit",
            f"employee_id={employee_id} result=failed reason={type(error).__name__}",
        )
        messagebox.showerror("Identity", str(error), parent=self)

    @staticmethod
    def _redacted_item_ids(item_ids: List[str]) -> str:
        return ",".join(f"…{item_id[-6:]}" for item_id in item_ids)

    def _delete_selected_profile(self):
        profile = self.profile_store.get(self.selected_profile_id or "")
        if not profile:
            return
        refs = profile.get("vault_refs") or {}
        item_ids = [str(ref["item_id"]) for ref in refs.values()]
        if not item_ids:
            messagebox.showinfo("Delete employee", "No bound vault items to delete.", parent=self)
            return
        exact = "\n".join(item_ids)
        confirmed = messagebox.askyesno(
            "Delete employee",
            "Move these exact Bitwarden items to Trash?\n\n"
            f"{exact}\n\nPermanent deletion is due in two days.",
            parent=self,
        )
        if not confirmed:
            return
        phrase = simpledialog.askstring(
            "Confirm deletion",
            "Type DELETE to continue:",
            parent=self,
        )
        if phrase != "DELETE":
            return
        employee_id = profile["employee_id"]
        self.status.set("Moving profile bundle to Trash…")

        def trash():
            result = self.profile_sync.trash_bundle(employee_id)
            self.after(0, lambda: self._profile_trash_complete(employee_id, result))

        threading.Thread(target=trash, daemon=True).start()

    def _profile_trash_complete(self, employee_id: str, result: Dict[str, List[str]]):
        self._clear_profile_secrets()
        self._refresh_profiles_list()
        status = "partial" if result["failed"] else "success"
        self.status.set("Profile deletion pending" if not result["failed"] else "Profile deletion incomplete")
        profile = self.profile_store.get(employee_id) or {}
        self.audit.log_security_event(
            "profile_trash",
            f"employee_id={employee_id} action=trash result={status} "
            f"items={self._redacted_item_ids(result['trashed'])} "
            f"deadline={(profile.get('deletion') or {}).get('purge_after', 'unknown')}",
        )
        self._update_profile_actions(profile)
        self._render_profile_viewer(
            "Pending permanent deletion. Restore is available until the two-day deadline."
        )

    def _restore_selected_profile(self):
        employee_id = self.selected_profile_id
        if not employee_id:
            return
        self.status.set("Restoring profile bundle…")

        def restore():
            result = self.profile_sync.restore_bundle(employee_id)
            self.after(0, lambda: self._profile_restore_complete(employee_id, result))

        threading.Thread(target=restore, daemon=True).start()

    def _profile_restore_complete(self, employee_id: str, result: Dict[str, List[str]]):
        status = "partial" if result["failed"] else "success"
        self.status.set("Profile restored" if not result["failed"] else "Restore incomplete")
        self.audit.log_security_event(
            "profile_restore",
            f"employee_id={employee_id} action=restore result={status} "
            f"items={self._redacted_item_ids(result['restored'])}",
        )
        self._refresh_profiles_list()
        self._clear_profile_secrets()
        self._on_profile_selected()

    # --- Workflow tab -------------------------------------------------
    def _build_workflow_tab(self):
        pad = ctk.CTkFrame(self.work_tab, fg_color=C["card"], corner_radius=0)
        pad.pack(fill=tk.BOTH, expand=True, padx=24, pady=(20, 18))
        pad.grid_columnconfigure(0, weight=1)
        pad.grid_rowconfigure(1, weight=1)

        self.employee_count = tk.StringVar(value="0 members")
        header = ctk.CTkFrame(pad, fg_color="transparent", corner_radius=0)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        ctk.CTkLabel(
            header,
            text="EMPLOYEES",
            font=("SF Pro Text", 15, "bold"),
            text_color=C["text"],
        ).pack(side=tk.LEFT)
        ctk.CTkFrame(header, fg_color=C["border"], height=1, corner_radius=0).pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True,
            padx=14,
            pady=10,
        )
        ctk.CTkLabel(
            header,
            textvariable=self.employee_count,
            font=("SF Pro Text", 12),
            text_color=C["muted"],
        ).pack(side=tk.RIGHT)

        employee_shell = ctk.CTkFrame(pad, fg_color=C["card"], corner_radius=0)
        employee_shell.grid(row=1, column=0, sticky="nsew")
        self.employee_canvas = tk.Canvas(
            employee_shell,
            bg=C["card"],
            highlightthickness=0,
            borderwidth=0,
        )
        employee_scroll = ttk.Scrollbar(
            employee_shell,
            orient="vertical",
            command=self.employee_canvas.yview,
        )
        self.employee_canvas.configure(yscrollcommand=employee_scroll.set)
        self.employee_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.employee_grid = tk.Frame(self.employee_canvas, bg=C["card"])
        self.employee_grid_window = self.employee_canvas.create_window(
            (0, 0),
            window=self.employee_grid,
            anchor="nw",
        )
        self.employee_grid.bind(
            "<Configure>",
            lambda _event: self.employee_canvas.configure(
                scrollregion=self.employee_canvas.bbox("all")
            ),
        )
        self.employee_canvas.bind(
            "<Configure>",
            lambda event: self.employee_canvas.itemconfigure(
                self.employee_grid_window,
                width=event.width,
            ),
        )
        self.employee_canvas.bind_all(
            "<MouseWheel>",
            self._scroll_employee_cards,
        )

        queue_bar = ctk.CTkFrame(
            pad,
            fg_color=C["surface"],
            border_color=C["border"],
            border_width=1,
            corner_radius=14,
        )
        queue_bar.grid(row=2, column=0, sticky="ew", pady=(12, 8))
        self.drop_label = ctk.CTkLabel(
            queue_bar,
            text="＋  Drop HQ files",
            text_color=C["text"],
            font=("SF Pro Text", 11, "bold"),
            cursor="hand2",
        )
        self.drop_label.pack(side=tk.LEFT, padx=14, pady=9)
        self.drop_label.bind("<Button-1>", self._browse_files)
        if _DND_AVAILABLE:
            try:
                self.drop_label.drop_target_register(DND_FILES)
                self.drop_label.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass
        self.queue_list = tk.Listbox(
            queue_bar,
            height=1,
            width=36,
            bg=C["surface"],
            fg=C["muted"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=("SF Pro Text", 8),
            activestyle="none",
        )
        self.queue_list.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(10, 0))

        self._refresh_active_employees()

        run_row = ctk.CTkFrame(
            pad,
            fg_color="#f8f8f9",
            border_color=C["border"],
            border_width=1,
            corner_radius=14,
        )
        run_row.grid(row=3, column=0, sticky="ew")
        ctk.CTkLabel(
            run_row,
            text="Shared passphrase",
            font=("SF Pro Text", 10, "bold"),
            text_color=C["muted"],
        ).pack(side=tk.LEFT, padx=(14, 8), pady=10)
        ctk.CTkEntry(
            run_row,
            textvariable=self.shared_passphrase,
            show="•",
            height=36,
            corner_radius=10,
            border_color=C["border"],
            border_width=1,
            fg_color=C["card"],
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8), pady=8)
        self.resume_button = ctk.CTkButton(
            run_row,
            text="Resume selected",
            command=self.resume_selected_employee,
            state=tk.DISABLED,
            height=36,
            corner_radius=10,
            fg_color="#e5e5e8",
            hover_color="#d8d8dc",
            text_color=C["text"],
            font=("SF Pro Text", 10, "bold"),
        )
        self.resume_button.pack(side=tk.RIGHT, padx=(0, 8), pady=8)
        ctk.CTkButton(
            run_row,
            text="Run onboarding",
            command=self.run_pipeline,
            height=36,
            corner_radius=10,
            fg_color=C["text"],
            hover_color="#323238",
            text_color="#ffffff",
            font=("SF Pro Text", 10, "bold"),
        ).pack(side=tk.RIGHT, padx=(0, 8), pady=8)

        status_row = tk.Frame(pad, bg=C["card"])
        status_row.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        tk.Label(
            status_row,
            textvariable=self.status,
            font=("SF Pro Text", 8, "bold"),
            fg=C["muted"],
            bg=C["card"],
            anchor="w",
        ).pack(fill=tk.X)
        log_frame = tk.Frame(pad, bg=C["card"])
        self.log = scrolledtext.ScrolledText(
            log_frame,
            state="disabled",
            wrap=tk.WORD,
            height=2,
            bg=C["card"],
            fg=C["muted"],
            insertbackground=C["text"],
            relief="flat",
            borderwidth=0,
            font=("SF Mono", 8),
            padx=0,
            pady=3,
        )

    def _scroll_employee_cards(self, event: tk.Event) -> None:
        if getattr(self, "current_view", None) != "onboarding":
            return
        self.employee_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

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
        pad = ctk.CTkFrame(self.settings_tab, fg_color=C["card"], corner_radius=0)
        pad.pack(fill=tk.BOTH, expand=True, padx=28, pady=18)

        def section(title: str) -> None:
            row = ctk.CTkFrame(pad, fg_color="transparent", corner_radius=0)
            row.pack(fill=tk.X, pady=(0, 7))
            ctk.CTkLabel(
                row,
                text=title,
                text_color=C["text"],
                font=("Avenir Next", 12, "bold"),
            ).pack(side=tk.LEFT)
            ctk.CTkFrame(row, height=1, fg_color=C["border"], corner_radius=0).pack(
                side=tk.LEFT,
                fill=tk.X,
                expand=True,
                padx=(12, 0),
            )

        def card() -> ctk.CTkFrame:
            frame = ctk.CTkFrame(
                pad,
                fg_color=C["surface"],
                corner_radius=18,
                border_color=C["border"],
                border_width=1,
            )
            frame.pack(fill=tk.X, pady=(0, 12))
            return frame

        section("STORAGE")
        storage = card()
        ctk.CTkLabel(
            storage,
            text="VAULT COLLECTION",
            fg_color=C["text"],
            text_color="#ffffff",
            corner_radius=12,
            anchor="w",
            font=("Avenir Next", 10, "bold"),
            height=36,
        ).pack(fill=tk.X, padx=10, pady=(10, 0))
        storage_body = ctk.CTkFrame(
            storage,
            fg_color="transparent",
            corner_radius=0,
        )
        storage_body.pack(fill=tk.X, padx=12, pady=10)
        storage_body.grid_columnconfigure(0, weight=2)
        storage_body.grid_columnconfigure(1, weight=1)
        storage_body.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            storage_body,
            text="Vault destination",
            text_color=C["muted"],
            font=("Avenir Next", 9, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        ctk.CTkEntry(
            storage_body,
            textvariable=self.collection_name,
            height=34,
            corner_radius=10,
            border_color=C["border"],
            border_width=1,
            fg_color=C["card"],
            font=("Avenir Next", 10),
        ).grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(3, 0))
        ctk.CTkLabel(
            storage_body,
            text="Local files",
            text_color=C["muted"],
            font=("Avenir Next", 9, "bold"),
        ).grid(row=0, column=1, sticky="w", padx=(0, 8))
        loc_combo = ctk.CTkComboBox(
            storage_body,
            values=list(LOCAL_DELETE_MODES.values()),
            height=34,
            corner_radius=10,
            border_color=C["border"],
            fg_color=C["card"],
            button_color=C["text"],
            button_hover_color="#333338",
            font=("Avenir Next", 9),
            command=lambda label: self.local_delete_mode.set(
                next(
                    key
                    for key, text in LOCAL_DELETE_MODES.items()
                    if text == label
                )
            ),
        )
        loc_combo.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(3, 0))
        loc_combo.set(
            LOCAL_DELETE_MODES.get(
                self.local_delete_mode.get(),
                LOCAL_DELETE_MODES[DEFAULT_LOCAL_DELETE_MODE],
            )
        )

        ctk.CTkLabel(
            storage_body,
            text="Vault cleanup",
            text_color=C["muted"],
            font=("Avenir Next", 9, "bold"),
        ).grid(row=0, column=2, sticky="w")
        shred_combo = ctk.CTkComboBox(
            storage_body,
            values=list(BW_SHRED_MODES.values()),
            height=34,
            corner_radius=10,
            border_color=C["border"],
            fg_color=C["card"],
            button_color=C["text"],
            button_hover_color="#333338",
            font=("Avenir Next", 9),
            command=lambda label: self.bw_shred_mode.set(
                next(
                    key
                    for key, text in BW_SHRED_MODES.items()
                    if text == label
                )
            ),
        )
        shred_combo.grid(row=1, column=2, sticky="ew", pady=(3, 0))
        shred_combo.set(
            BW_SHRED_MODES.get(
                self.bw_shred_mode.get(),
                BW_SHRED_MODES[DEFAULT_BW_SHRED_MODE],
            )
        )

        section("AUTOMATION")
        automation = card()

        def toggle_row(
            title: str,
            subtitle: str,
            variable: tk.BooleanVar,
        ) -> None:
            row = ctk.CTkFrame(automation, fg_color="transparent", corner_radius=0)
            row.pack(fill=tk.X, padx=14, pady=4)
            copy = ctk.CTkFrame(row, fg_color="transparent", corner_radius=0)
            copy.pack(side=tk.LEFT)
            ctk.CTkLabel(
                copy,
                text=title,
                text_color=C["text"],
                font=("Avenir Next", 10, "bold"),
            ).pack(anchor="w")
            ctk.CTkLabel(
                copy,
                text=subtitle,
                text_color=C["muted"],
                font=("Avenir Next", 8),
            ).pack(anchor="w")
            ctk.CTkSwitch(
                row,
                text="",
                variable=variable,
                width=42,
                switch_width=38,
                switch_height=20,
                corner_radius=10,
                fg_color="#d9d9de",
                progress_color=C["text"],
                button_color="#ffffff",
                button_hover_color="#ffffff",
            ).pack(side=tk.RIGHT)

        toggle_row("Auto-import files", "Watch Downloads for HQ exports", self.auto_import)
        toggle_row("Sync on startup", "Fetch profile references after unlock", self.sync_on_startup)
        toggle_row("Create email", "Outlook checkpoint before partner accounts", self.provision_outlook)
        toggle_row("Create Hyatt", "Prefill and bind a real Login item", self.provision_hyatt)
        toggle_row("Create Marriott", "Prefill and bind a real Login item", self.provision_marriott)

        section("DANGER ZONE")
        danger = card()
        danger_row = ctk.CTkFrame(danger, fg_color="transparent", corner_radius=0)
        danger_row.pack(fill=tk.X, padx=14, pady=10)
        copy = ctk.CTkFrame(danger_row, fg_color="transparent", corner_radius=0)
        copy.pack(side=tk.LEFT)
        ctk.CTkLabel(
            copy,
            text="Delete employee bundle",
            text_color=C["text"],
            font=("Avenir Next", 10, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            copy,
            text="Scoped item IDs · two-day restore window · no name-based bulk delete",
            text_color=C["muted"],
            font=("Avenir Next", 8),
        ).pack(anchor="w")
        ctk.CTkButton(
            danger_row,
            text="Choose profile",
            command=lambda: self._show_view("profiles"),
            height=36,
            corner_radius=10,
            border_width=1,
            border_color=C["text"],
            fg_color=C["card"],
            hover_color=C["card_hi"],
            text_color=C["text"],
            font=("Avenir Next", 9, "bold"),
        ).pack(side=tk.RIGHT)

    def _save_settings(self):
        new_settings = {
            "collection_name": self.collection_name.get().strip() or "Personal Vault",
            "auto_import": "true" if self.auto_import.get() else "false",
            "sync_on_startup": "true" if self.sync_on_startup.get() else "false",
            "provision_outlook": "true" if self.provision_outlook.get() else "false",
            "provision_hyatt": "true" if self.provision_hyatt.get() else "false",
            "provision_marriott": "true" if self.provision_marriott.get() else "false",
            "local_delete_mode": self.local_delete_mode.get(),
            "bw_shred_mode": self.bw_shred_mode.get(),
        }
        old_settings = {key: str(self.store.get(key, "")) for key in new_settings}
        self.store.update(new_settings)
        for key, new_value in new_settings.items():
            old_value = old_settings[key]
            if old_value != new_value:
                self.audit.log_config_change(key, old_value, new_value)
        messagebox.showinfo("Saved", "Settings stored in Keychain.", parent=self)

    # --- Transactions (compact) ---------------------------------------
    def _build_transactions_tab(self):
        pad = ctk.CTkFrame(self.tx_tab, fg_color=C["card"], corner_radius=0)
        pad.pack(fill=tk.BOTH, expand=True, padx=24, pady=20)

        header = ctk.CTkFrame(pad, fg_color="transparent", corner_radius=0)
        header.pack(fill=tk.X, pady=(0, 12))
        ctk.CTkLabel(
            header,
            text="SPEND CONTROL",
            font=("Avenir Next", 17, "bold"),
            text_color=C["text"],
        ).pack(anchor="w")
        ctk.CTkLabel(
            header,
            text="Employee limits and card activity",
            font=("Avenir Next", 10),
            text_color=C["muted"],
        ).pack(side=tk.LEFT, padx=(12, 0), pady=(4, 0))
        self.ledger_filter = tk.StringVar(value="All employees")
        self.ledger_filter_menu = ctk.CTkComboBox(
            header,
            variable=self.ledger_filter,
            values=["All employees"],
            command=lambda _value: self._refresh_transaction_list(),
            width=190,
            height=34,
            corner_radius=10,
            border_color=C["border"],
            fg_color=C["surface"],
            button_color=C["text"],
            button_hover_color="#333338",
            font=("Avenir Next", 10),
        )
        self.ledger_filter_menu.pack(side=tk.RIGHT)
        ctk.CTkButton(
            header,
            text="Set limit",
            command=self._configure_selected_budget,
            width=86,
            height=34,
            corner_radius=10,
            fg_color=C["card_hi"],
            hover_color="#dedee2",
            text_color=C["text"],
            font=("Avenir Next", 9, "bold"),
        ).pack(side=tk.RIGHT, padx=(0, 8))

        self.budget_overview = ctk.CTkFrame(
            pad,
            fg_color="transparent",
            corner_radius=0,
        )
        self.budget_overview.pack(fill=tk.X, pady=(0, 12))

        entry = ctk.CTkFrame(
            pad,
            fg_color=C["surface"],
            corner_radius=14,
            border_color=C["border"],
            border_width=1,
        )
        entry.pack(fill=tk.X, pady=(0, 10))
        self.trans_date = tk.StringVar(value=datetime.now().date().isoformat())
        self.trans_amount = tk.StringVar()
        self.trans_merchant = tk.StringVar()
        self.trans_employee = tk.StringVar()
        ctk.CTkLabel(
            entry,
            text="NEW SPEND",
            font=("Avenir Next", 10, "bold"),
            text_color=C["muted"],
        ).pack(side=tk.LEFT, padx=(14, 10), pady=10)
        for variable, placeholder, width in (
            (self.trans_date, "Date", 115),
            (self.trans_amount, "Amount", 100),
            (self.trans_merchant, "Merchant", 190),
        ):
            ctk.CTkEntry(
                entry,
                textvariable=variable,
                placeholder_text=placeholder,
                width=width,
                height=34,
                corner_radius=9,
                border_color=C["border"],
                fg_color=C["card"],
                font=("Avenir Next", 10),
            ).pack(side=tk.LEFT, padx=(0, 7), pady=8)
        self.employee_combo = ctk.CTkComboBox(
            entry,
            variable=self.trans_employee,
            values=["Select employee"],
            width=180,
            height=34,
            corner_radius=9,
            border_color=C["border"],
            fg_color=C["card"],
            button_color=C["text"],
            button_hover_color="#333338",
            font=("Avenir Next", 10),
        )
        self.employee_combo.pack(side=tk.LEFT, padx=(0, 7), pady=8)
        ctk.CTkButton(
            entry,
            text="Add spend",
            command=self._add_transaction,
            width=96,
            height=34,
            corner_radius=9,
            fg_color=C["text"],
            hover_color="#333338",
            text_color="#ffffff",
            font=("Avenir Next", 9, "bold"),
        ).pack(side=tk.RIGHT, padx=(0, 10), pady=8)

        list_frame = ctk.CTkFrame(
            pad,
            fg_color=C["card"],
            corner_radius=14,
            border_color=C["border"],
            border_width=1,
        )
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 6))
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
        self.trans_tree.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        btns = ctk.CTkFrame(pad, fg_color="transparent", corner_radius=0)
        btns.pack(fill=tk.X)
        ctk.CTkButton(
            btns,
            text="Refresh",
            command=self._refresh_transaction_list,
            width=76,
            height=30,
            corner_radius=9,
            fg_color=C["card_hi"],
            hover_color="#dedee2",
            text_color=C["text"],
        ).pack(side=tk.LEFT)
        ctk.CTkButton(
            btns,
            text="Delete selected",
            command=self._delete_selected_transaction,
            width=110,
            height=30,
            corner_radius=9,
            fg_color="transparent",
            hover_color="#fee2e2",
            text_color=C["danger"],
        ).pack(side=tk.RIGHT)
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

    def _confirm_account_stage(
        self,
        service: str,
        employee: Dict[str, str],
        result: Dict[str, Any],
    ) -> bool:
        completed = threading.Event()
        response = {"confirmed": False}

        def ask_for_confirmation():
            filled = result.get("filled_fields") or []
            filled_text = ", ".join(filled) if filled else "none (complete manually)"
            email = employee.get("email") or employee.get("username") or "unknown"
            note = (
                "\n\nSelecting No for Outlook keeps hotel accounts pending."
                if service == "Outlook"
                else ""
            )
            response["confirmed"] = messagebox.askyesno(
                f"{service} checkpoint",
                f"Finish creating the {service} account in managed Chrome.\n\n"
                f"Employee: {employee.get('full_name', 'Unknown')}\n"
                f"Email: {email}\n"
                f"Autofilled fields: {filled_text}\n\n"
                "Select Yes only after the account has been created."
                f"{note}",
                parent=self,
            )
            completed.set()

        self.app.root.after(0, ask_for_confirmation)
        completed.wait()
        return response["confirmed"]

    def resume_selected_employee(self):
        if self._pipeline_running:
            messagebox.showinfo(
                "Onboarding already running",
                "Wait for the current onboarding run to finish.",
                parent=self,
            )
            return
        if not self.selected_employee:
            messagebox.showinfo(
                "Select an employee",
                "Select an employee profile before resuming account creation.",
                parent=self,
            )
            return
        passphrase = self.shared_passphrase.get()
        if len(passphrase) < 8:
            messagebox.showerror(
                "Passphrase required",
                "Enter the employee passphrase used for their accounts.",
                parent=self,
            )
            return
        config = OnboardingConfig(
            bw=BitwardenConfig(
                collection_name=self.collection_name.get().strip() or "Personal Vault"
            ),
            local_delete_mode=self.local_delete_mode.get(),
            bw_shred_mode=self.bw_shred_mode.get(),
            provision_outlook=self.provision_outlook.get(),
            provision_hyatt=self.provision_hyatt.get(),
            provision_marriott=self.provision_marriott.get(),
        )
        previous_employee_ids = {
            profile["employee_id"]
            for profile in self.profile_store.list_profiles(include_purged=True)
        }
        employee_name = self.selected_employee

        def on_progress(step: str, detail: str = ""):
            self.app.root.after(
                0,
                lambda current_step=step, current_detail=detail: self._set_step(
                    current_step,
                    current_detail,
                ),
            )

        def worker():
            try:
                self.onboarding.resume_accounts(
                    employee_name,
                    passphrase,
                    config,
                    progress_callback=on_progress,
                    account_confirmation_callback=self._confirm_account_stage,
                )
                self.app.root.after(0, self._refresh_active_employees)
                self.app.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "Accounts updated",
                        f"Account progress saved for {employee_name}.",
                        parent=self,
                    ),
                )
            except Exception as error:
                logging.error("Account resume failed", exc_info=True)
                self.app.root.after(
                    0,
                    lambda current_error=error: messagebox.showerror(
                        "Resume failed",
                        str(current_error),
                        parent=self,
                    ),
                )
            finally:
                self.app.root.after(0, self._pipeline_finished)

        self._pipeline_running = True
        self.status.set(f"Resuming {employee_name}…")
        threading.Thread(target=worker, daemon=True).start()

    # --- Pipeline -----------------------------------------------------
    def run_pipeline(self):
        if self._pipeline_running:
            messagebox.showinfo(
                "Onboarding already running",
                "Wait for the current onboarding run to finish.",
                parent=self,
            )
            return
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
        collection = self.collection_name.get().strip() or "Personal Vault"
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
                    account_confirmation_callback=self._confirm_account_stage,
                )
                self.app.root.after(
                    0,
                    lambda: self._on_onboarding_complete(previous_employee_ids),
                )
            except Exception as e:
                logging.error("Pipeline failed", exc_info=True)
                self.app.root.after(
                    0,
                    lambda error=e: messagebox.showerror(
                        "Pipeline failed",
                        str(error),
                        parent=self,
                    ),
                )
                self.app.root.after(
                    0,
                    lambda error=e: self.status.set(f"Failed: {error}"),
                )
            finally:
                self.app.root.after(0, self._pipeline_finished)

        self._pipeline_running = True
        self.status.set("Running…")
        threading.Thread(target=worker, daemon=True).start()

    def _pipeline_finished(self):
        self._pipeline_running = False

    def _on_onboarding_complete(self, previous_employee_ids: Set[str]):
        self._refresh_queued_files()
        self._refresh_employee_list()
        self._prompt_new_employee_budgets(previous_employee_ids)
        messagebox.showinfo(
            "Complete",
            "Onboarding finished and employee spend controls are ready.",
            parent=self,
        )

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
        profiles = self.profile_store.list_profiles()
        self._ledger_employee_map = {
            profile["display_name"]: profile["employee_id"]
            for profile in profiles
        }
        names = sorted(self._ledger_employee_map)
        self.employee_combo.configure(values=names or ["No employees"])
        self.ledger_filter_menu.configure(values=["All employees", *names])
        if self.ledger_filter.get() not in {"All employees", *names}:
            self.ledger_filter.set("All employees")
        self._refresh_active_employees()

    def _refresh_active_employees(self):
        if not hasattr(self, "employee_grid"):
            return
        profiles = self.profile_store.list_profiles()
        profile_by_name = {
            profile.get("display_name", ""): profile
            for profile in profiles
            if profile.get("display_name")
        }
        names = sorted(profile_by_name)
        for child in self.employee_grid.winfo_children():
            child.destroy()
        if self.selected_employee and self.selected_employee not in names:
            self.selected_employee = None
            if hasattr(self, "resume_button"):
                self.resume_button.configure(state=tk.DISABLED)
        self.employee_grid.grid_columnconfigure(0, weight=1)
        self.employee_grid.grid_columnconfigure(1, weight=1)
        self.employee_count.set(f"{len(names)} members")
        if not names:
            tk.Label(
                self.employee_grid,
                text="No employees yet. Add an HQ export to begin.",
                font=("SF Pro Text", 10),
                fg=C["muted"],
                bg=C["card"],
                pady=36,
            ).grid(row=0, column=0, columnspan=2, sticky="ew")
            return
        for index, name in enumerate(names):
            details = profile_by_name[name]
            refs = details.get("vault_refs") or {}
            completed = len(refs)
            deletion = details.get("deletion") or {}
            status = str(deletion.get("status") or details.get("status") or "active").title()
            initials = "".join(part[0] for part in name.split() if part)[:2].upper()
            selected = name == self.selected_employee
            card = ctk.CTkFrame(
                self.employee_grid,
                fg_color=C["card"],
                corner_radius=22,
                border_color=C["text"] if selected else C["border"],
                border_width=2 if selected else 1,
                height=148,
                cursor="hand2",
            )
            row, column = divmod(index, 2)
            card.grid(
                row=row,
                column=column,
                sticky="nsew",
                padx=(0 if column == 0 else 8, 8 if column == 0 else 0),
                pady=8,
            )
            card.grid_propagate(False)
            top = ctk.CTkFrame(card, fg_color="transparent", corner_radius=0)
            top.pack(fill=tk.X, padx=18, pady=(13, 4))
            avatar = ctk.CTkFrame(
                top,
                width=42,
                height=42,
                corner_radius=21,
                fg_color=C["text"],
            )
            avatar.pack(side=tk.LEFT)
            avatar.pack_propagate(False)
            avatar_label = ctk.CTkLabel(
                avatar,
                text=initials or "—",
                text_color="#ffffff",
                font=("Avenir Next", 11, "bold"),
            )
            avatar_label.pack(expand=True)
            ring = CompletionRing(top, completed * 20, size=42)
            ring.pack(side=tk.RIGHT)
            name_label = ctk.CTkLabel(
                card,
                text=name,
                font=("Avenir Next", 14, "bold"),
                text_color=C["text"],
                anchor="w",
            )
            name_label.pack(fill=tk.X, padx=18, pady=(1, 0))
            metadata = (
                details.get("email")
                or details.get("username")
                or "Bitwarden employee"
            )
            meta_label = ctk.CTkLabel(
                card,
                text=metadata,
                font=("Avenir Next", 9),
                text_color=C["muted"],
                anchor="w",
            )
            meta_label.pack(fill=tk.X, padx=18)
            footer = ctk.CTkFrame(card, fg_color="transparent", corner_radius=0)
            footer.pack(fill=tk.X, padx=18, pady=(5, 10))
            count_label = ctk.CTkLabel(
                footer,
                text=f"{completed}/5 vault records",
                font=("Avenir Next", 9),
                text_color=C["muted"],
            )
            count_label.pack(side=tk.LEFT)
            status_pill = ctk.CTkLabel(
                footer,
                text=status,
                font=("Avenir Next", 9, "bold"),
                text_color="#ffffff" if status == "Active" else C["muted"],
                fg_color=C["text"] if status == "Active" else C["card_hi"],
                corner_radius=14,
                width=64,
                height=24,
            )
            status_pill.pack(side=tk.RIGHT)
            for widget in (
                card,
                top,
                avatar,
                avatar_label,
                ring,
                name_label,
                meta_label,
                footer,
                count_label,
                status_pill,
            ):
                widget.bind(
                    "<Button-1>",
                    lambda _event, employee_name=name: self._select_employee(
                        employee_name
                    ),
                )

    def _select_employee(self, employee_name: str):
        self.selected_employee = employee_name
        if hasattr(self, "resume_button"):
            self.resume_button.configure(state=tk.NORMAL)
        self._refresh_active_employees()

    def _refresh_transaction_list(self):
        for item in self.trans_tree.get_children():
            self.trans_tree.delete(item)
        selected_name = self.ledger_filter.get()
        employee_id = getattr(self, "_ledger_employee_map", {}).get(selected_name)
        if employee_id:
            transactions = self.transaction_db.get_transactions_by_employee_id(employee_id)
        else:
            transactions = self.transaction_db.get_all_transactions()
        for trans in transactions[:50]:
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
        self._refresh_budget_overview(selected_name)

    def _refresh_budget_overview(self, selected_name: str = "All employees"):
        for child in self.budget_overview.winfo_children():
            child.destroy()
        budget_by_id = {
            budget["employee_id"]: budget
            for budget in self.transaction_db.get_employee_budgets()
        }
        profiles = self.profile_store.list_profiles()
        if selected_name != "All employees":
            profiles = [
                profile
                for profile in profiles
                if profile.get("display_name") == selected_name
            ]
        visible_profiles = profiles[:4]
        if not visible_profiles:
            ctk.CTkLabel(
                self.budget_overview,
                text="Employee spend limits will appear after onboarding.",
                text_color=C["muted"],
                font=("Avenir Next", 11),
            ).pack(anchor="w", pady=12)
            return

        for index, profile in enumerate(visible_profiles):
            budget = budget_by_id.get(profile["employee_id"])
            card = ctk.CTkFrame(
                self.budget_overview,
                fg_color=C["surface"],
                corner_radius=16,
                border_color=C["border"],
                border_width=1,
            )
            card.pack(
                side=tk.LEFT,
                fill=tk.X,
                expand=True,
                padx=(0 if index == 0 else 5, 0),
            )
            ctk.CTkLabel(
                card,
                text=profile.get("display_name", "Employee"),
                text_color=C["text"],
                font=("Avenir Next", 11, "bold"),
                anchor="w",
            ).pack(fill=tk.X, padx=14, pady=(11, 0))
            if budget is None:
                ctk.CTkLabel(
                    card,
                    text="Limit not set",
                    text_color=C["muted"],
                    font=("Avenir Next", 10),
                ).pack(anchor="w", padx=14, pady=(4, 12))
                continue
            spent = budget["total_spent"]
            limit = budget["spend_limit"]
            ratio = spent / limit if limit else 0
            progress_color = (
                C["danger"]
                if ratio >= 1
                else C["warn"]
                if ratio >= 0.8
                else C["text"]
            )
            ctk.CTkLabel(
                card,
                text=f"${spent:,.0f} of ${limit:,.0f}",
                text_color=C["muted"],
                font=("Avenir Next", 10),
                anchor="w",
            ).pack(fill=tk.X, padx=14, pady=(2, 6))
            progress = ctk.CTkProgressBar(
                card,
                height=8,
                corner_radius=4,
                fg_color="#dedee3",
                progress_color=progress_color,
            )
            progress.pack(fill=tk.X, padx=14)
            progress.set(min(max(ratio, 0), 1))
            remaining = limit - spent
            ctk.CTkLabel(
                card,
                text=(
                    f"${remaining:,.0f} remaining"
                    if remaining >= 0
                    else f"${abs(remaining):,.0f} over limit"
                ),
                text_color=C["danger"] if remaining < 0 else C["muted"],
                font=("Avenir Next", 9, "bold" if remaining < 0 else "normal"),
            ).pack(anchor="w", padx=14, pady=(5, 10))

    def _configure_selected_budget(self):
        selected_name = self.ledger_filter.get()
        employee_id = getattr(self, "_ledger_employee_map", {}).get(selected_name)
        if not employee_id:
            messagebox.showinfo(
                "Choose an employee",
                "Select one employee from the ledger filter before setting a limit.",
                parent=self,
            )
            return
        profile = self.profile_store.get(employee_id)
        if profile:
            self._prompt_employee_budget(profile)

    def _prompt_employee_budget(self, profile: Dict[str, Any]) -> bool:
        name = profile.get("display_name", "Employee")
        spent = simpledialog.askfloat(
            "Current card spend",
            f"How much has {name} already spent?",
            parent=self,
            minvalue=0.0,
            initialvalue=0.0,
        )
        if spent is None:
            return False
        spend_limit = simpledialog.askfloat(
            "Employee spend limit",
            f"What is {name}'s total spend limit?",
            parent=self,
            minvalue=0.01,
        )
        if spend_limit is None:
            return False
        if spent > spend_limit:
            proceed = messagebox.askyesno(
                "Spend exceeds limit",
                f"{name} is already over the entered limit. Save it anyway?",
                parent=self,
            )
            if not proceed:
                return False
        saved = self.transaction_db.set_employee_budget(
            profile["employee_id"],
            name,
            spent,
            spend_limit,
        )
        if not saved:
            messagebox.showerror(
                "Spend limit",
                "The employee spend limit could not be saved.",
                parent=self,
            )
            return False
        self._refresh_transaction_list()
        return True

    def _prompt_new_employee_budgets(self, previous_employee_ids: Set[str]):
        existing_budget_ids = {
            budget["employee_id"]
            for budget in self.transaction_db.get_employee_budgets()
        }
        new_profiles = [
            profile
            for profile in self.profile_store.list_profiles()
            if profile["employee_id"] not in previous_employee_ids
            and profile["employee_id"] not in existing_budget_ids
        ]
        for profile in new_profiles:
            self._prompt_employee_budget(profile)

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
        employee_id = getattr(self, "_ledger_employee_map", {}).get(employee)
        if not employee_id:
            messagebox.showerror("Error", "Select a valid employee.", parent=self)
            return
        if self.transaction_db.add_transaction(
            date,
            amount,
            merchant,
            employee,
            card_number,
            employee_id=employee_id,
        ):
            self.audit.log_transaction_added(employee, amount, merchant)
            self._refresh_transaction_list()
            self._refresh_employee_list()
        else:
            messagebox.showerror("Error", "Failed to add transaction.", parent=self)

    def _export_transactions(self):
        import csv

        selected_name = self.ledger_filter.get()
        employee_id = getattr(self, "_ledger_employee_map", {}).get(selected_name)
        transactions = (
            self.transaction_db.get_transactions_by_employee_id(employee_id)
            if employee_id
            else self.transaction_db.get_all_transactions()
        )
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
