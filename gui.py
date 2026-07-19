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


def _auth_shell(dialog: ctk.CTkToplevel, *, height: int) -> ctk.CTkFrame:
    """Shared unlock chrome: zinc desktop + white card with brand mark."""
    dialog.resizable(False, False)
    dialog.configure(fg_color=C["bg"])
    dialog.update_idletasks()
    width = 420
    screen_w = max(dialog.winfo_screenwidth(), width)
    screen_h = max(dialog.winfo_screenheight(), height)
    x = max((screen_w - width) // 2, 40)
    y = max((screen_h - height) // 3, 40)
    dialog.geometry(f"{width}x{height}+{x}+{y}")
    shell = ctk.CTkFrame(dialog, fg_color=C["bg"], corner_radius=0)
    shell.pack(fill=tk.BOTH, expand=True)
    card = ctk.CTkFrame(
        shell,
        fg_color=C["card"],
        corner_radius=22,
        border_width=1,
        border_color=C["border"],
    )
    card.pack(fill=tk.BOTH, expand=True, padx=22, pady=22)
    return card


def _present_auth_dialog(dialog: ctk.CTkToplevel, parent: tk.Tk) -> None:
    """Force the unlock dialog on-screen (CTkToplevel stays hidden if parent is withdrawn)."""

    def show() -> None:
        try:
            if not dialog.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            dialog.transient(parent)
        except tk.TclError:
            pass
        dialog.update_idletasks()
        dialog.deiconify()
        dialog.lift()
        dialog.focus_force()
        try:
            dialog.attributes("-topmost", True)
        except tk.TclError:
            pass
        try:
            dialog.grab_set()
        except tk.TclError:
            pass

    show()
    dialog.after(50, show)
    dialog.after(220, show)
    dialog.after(400, lambda: dialog.attributes("-topmost", False) if dialog.winfo_exists() else None)


def _auth_brand(parent: ctk.CTkFrame, title: str, subtitle: str) -> None:
    header = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
    header.pack(fill=tk.X, padx=28, pady=(28, 18))
    mark = ctk.CTkFrame(
        header,
        width=44,
        height=44,
        corner_radius=14,
        fg_color=C["text"],
    )
    mark.pack()
    mark.pack_propagate(False)
    BrandGlyph(mark, size=30).pack(expand=True)
    ctk.CTkLabel(
        header,
        text="DOWNLOWd",
        font=("Avenir Next", 22, "bold"),
        text_color=C["text"],
    ).pack(pady=(14, 2))
    ctk.CTkLabel(
        header,
        text=title,
        font=("Avenir Next", 15, "bold"),
        text_color=C["text"],
    ).pack()
    ctk.CTkLabel(
        header,
        text=subtitle,
        font=("SF Pro Text", 12),
        text_color=C["muted"],
        wraplength=300,
        justify="center",
    ).pack(pady=(6, 0))


def _auth_field_label(parent: ctk.CTkFrame, text: str) -> None:
    ctk.CTkLabel(
        parent,
        text=text.upper(),
        font=("Avenir Next", 9, "bold"),
        text_color=C["muted"],
        anchor="w",
    ).pack(fill=tk.X, pady=(0, 6))


def _auth_entry(
    parent: ctk.CTkFrame,
    *,
    textvariable: tk.StringVar,
    show: str = "",
    placeholder: str = "",
) -> ctk.CTkEntry:
    entry = ctk.CTkEntry(
        parent,
        textvariable=textvariable,
        height=44,
        corner_radius=12,
        border_width=1,
        border_color=C["border"],
        fg_color=C["surface"],
        text_color=C["text"],
        placeholder_text=placeholder,
        placeholder_text_color=C["muted"],
        font=("SF Pro Text", 13),
        show=show,
    )
    entry.pack(fill=tk.X, pady=(0, 14))
    return entry


def _auth_primary_button(parent: ctk.CTkFrame, text: str, command: Callable[[], None]) -> ctk.CTkButton:
    button = ctk.CTkButton(
        parent,
        text=text,
        command=command,
        height=46,
        corner_radius=14,
        border_width=0,
        fg_color=C["text"],
        hover_color="#323238",
        text_color="#ffffff",
        font=("SF Pro Text", 13, "bold"),
        cursor="hand2",
    )
    button.pack(fill=tk.X, pady=(6, 0))
    return button


class AppPasswordDialog(ctk.CTkToplevel):
    """Create or verify the local application password."""

    def __init__(self, parent: tk.Tk, session_manager: SessionManager):
        super().__init__(parent)
        self.session_manager = session_manager
        self.audit = get_audit_logger()
        self.success = False
        self.setup_mode = not session_manager.has_password()

        self.title("Set DOWNLOWd Password" if self.setup_mode else "Unlock DOWNLOWd")
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._build_ui()
        _present_auth_dialog(self, parent)

    def _on_cancel(self):
        self.audit.log_authentication(False, method="app_password_cancelled")
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def _build_ui(self):
        card = _auth_shell(self, height=520 if self.setup_mode else 460)
        title = "Create app password" if self.setup_mode else "Unlock workspace"
        subtitle = (
            "Protect local onboarding controls with a separate password."
            if self.setup_mode
            else "Enter your DOWNLOWd app password to continue."
        )
        _auth_brand(card, title, subtitle)

        form = ctk.CTkFrame(card, fg_color="transparent", corner_radius=0)
        form.pack(fill=tk.BOTH, expand=True, padx=28, pady=(0, 28))

        _auth_field_label(form, "App Password")
        password_var = tk.StringVar()
        password_entry = _auth_entry(form, textvariable=password_var, show="•")

        confirm_var = tk.StringVar()
        if self.setup_mode:
            _auth_field_label(form, "Confirm Password")
            _auth_entry(form, textvariable=confirm_var, show="•")

        status_var = tk.StringVar()
        ctk.CTkLabel(
            form,
            textvariable=status_var,
            font=("SF Pro Text", 11),
            text_color=C["danger"],
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 8))

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

        _auth_primary_button(
            form,
            "Create Password" if self.setup_mode else "Unlock",
            submit,
        )
        password_entry.bind("<Return>", lambda _event: submit())
        password_entry.focus()


class BitwardenLoginDialog(ctk.CTkToplevel):
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
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._build_ui()
        _present_auth_dialog(self, parent)

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
        card = _auth_shell(self, height=540)
        _auth_brand(
            card,
            "Sign in with Bitwarden",
            "Your vault unlocks the workspace.",
        )

        form = ctk.CTkFrame(card, fg_color="transparent", corner_radius=0)
        form.pack(fill=tk.BOTH, expand=True, padx=28, pady=(0, 28))

        _auth_field_label(form, "Email")
        email_var = tk.StringVar(value=self.credential_store.get("bw_email", ""))
        _auth_entry(form, textvariable=email_var, placeholder="you@company.com")

        _auth_field_label(form, "Master Password")
        password_var = tk.StringVar()
        password_entry = _auth_entry(form, textvariable=password_var, show="•")
        password_entry.focus()

        status_var = tk.StringVar(value="")
        ctk.CTkLabel(
            form,
            textvariable=status_var,
            font=("SF Pro Text", 11),
            text_color=C["muted"],
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 4))

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

        _auth_primary_button(form, "Unlock with Bitwarden", do_login)
        password_entry.bind("<Return>", lambda _event: do_login())
        ctk.CTkLabel(
            form,
            text="DOWNLOWd is a Bitwarden wrapper — secrets stay in your vault.",
            font=("SF Pro Text", 11),
            text_color=C["muted"],
            wraplength=300,
            justify="center",
        ).pack(pady=(16, 0))


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
        # Keep root mapped but invisible during auth so CTk dialogs can appear on macOS.
        # withdraw() hides child unlock windows; off-screen geometry shows a blank window.
        self.root.title("DOWNLOWd")
        self.root.geometry("1x1+0+0")
        self.root.minsize(1, 1)
        try:
            self.root.attributes("-alpha", 0.0)
        except tk.TclError:
            self.root.withdraw()
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
        self.root.minsize(420, 480)
        self.root.geometry("480x560+120+60")
        for child in self.root.winfo_children():
            child.destroy()
        self.dashboard = Dashboard(self.root, self)
        try:
            self.root.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
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
    """Single-window workspace: people list + ledger + action modals."""

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

        self.shared_passphrase = tk.StringVar(
            value=self.store.get("shared_passphrase", "")
        )
        self.collection_name = tk.StringVar(
            value=self.store.get("collection_name", "Personal Vault")
        )
        self.auto_import = tk.BooleanVar(
            value=self.store.get("auto_import", "true") == "true"
        )
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
        self.status = tk.StringVar(value="Watching Downloads for HQ files…")
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.step_labels: Dict[str, tk.Label] = {}
        self._pipeline_running = False
        self.selected_employee: Optional[str] = None
        self.selected_profile_id: Optional[str] = None
        self.selected_record_role = "identity"
        self.profile_bundle: Dict[str, Dict[str, Any]] = {}
        self._revealed_profile_values: Set[Tuple[str, str]] = set()
        self._employee_modal: Optional[ctk.CTkToplevel] = None
        self._ledger_employee_map: Dict[str, str] = {}

        self._build()
        self._configure_logging()
        self.after(100, self._poll_log_queue)
        self._refresh_queued_files()
        threading.Thread(target=self._monitor_downloads, daemon=True).start()

    def _build(self):
        shell = ctk.CTkFrame(self, fg_color=C["bg"], corner_radius=0)
        shell.pack(fill=tk.BOTH, expand=True)

        header = ctk.CTkFrame(shell, fg_color="transparent", height=56)
        header.pack(fill=tk.X, padx=16, pady=(14, 8))
        header.pack_propagate(False)

        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.pack(side=tk.LEFT)
        mark = ctk.CTkFrame(brand, width=34, height=34, corner_radius=11, fg_color=C["text"])
        mark.pack(side=tk.LEFT)
        mark.pack_propagate(False)
        BrandGlyph(mark, size=24).pack(expand=True)
        ctk.CTkLabel(
            brand,
            text="DOWNLOWd",
            font=("Avenir Next", 16, "bold"),
            text_color=C["text"],
        ).pack(side=tk.LEFT, padx=(10, 0))

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.pack(side=tk.RIGHT)
        ctk.CTkButton(
            actions,
            text="Sync",
            command=self._sync_profiles,
            width=64,
            height=32,
            corner_radius=10,
            fg_color=C["card_hi"],
            hover_color="#dedee2",
            text_color=C["text"],
            font=("Avenir Next", 11, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ctk.CTkButton(
            actions,
            text="⚙",
            command=self._open_settings_modal,
            width=32,
            height=32,
            corner_radius=10,
            fg_color=C["card_hi"],
            hover_color="#dedee2",
            text_color=C["text"],
            font=("Avenir Next", 14),
        ).pack(side=tk.LEFT)

        body = ctk.CTkFrame(shell, fg_color="transparent")
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 8))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # --- Employees (contact list) ---
        people = ctk.CTkFrame(
            body,
            fg_color=C["card"],
            corner_radius=18,
            border_width=1,
            border_color=C["border"],
        )
        people.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        head = ctk.CTkFrame(people, fg_color="transparent")
        head.pack(fill=tk.X, padx=14, pady=(12, 6))
        ctk.CTkLabel(
            head,
            text="People",
            font=("Avenir Next", 13, "bold"),
            text_color=C["text"],
        ).pack(side=tk.LEFT)
        self.employee_count = tk.StringVar(value="0")
        ctk.CTkLabel(
            head,
            textvariable=self.employee_count,
            font=("Avenir Next", 11),
            text_color=C["muted"],
        ).pack(side=tk.RIGHT)

        self.employee_scroll = ctk.CTkScrollableFrame(
            people,
            fg_color="transparent",
            corner_radius=0,
        )
        self.employee_scroll.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 10))
        self.employee_grid = self.employee_scroll

        # --- Ledger widget ---
        ledger = ctk.CTkFrame(
            body,
            fg_color=C["card"],
            corner_radius=18,
            border_width=1,
            border_color=C["border"],
        )
        ledger.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        lhead = ctk.CTkFrame(ledger, fg_color="transparent")
        lhead.pack(fill=tk.X, padx=14, pady=(12, 6))
        ctk.CTkLabel(
            lhead,
            text="Ledger",
            font=("Avenir Next", 13, "bold"),
            text_color=C["text"],
        ).pack(side=tk.LEFT)
        ctk.CTkButton(
            lhead,
            text="+",
            command=self._add_transaction_dialog,
            width=28,
            height=28,
            corner_radius=9,
            fg_color=C["text"],
            hover_color="#323238",
            text_color="#ffffff",
            font=("Avenir Next", 14, "bold"),
        ).pack(side=tk.RIGHT)

        self.budget_overview = ctk.CTkFrame(ledger, fg_color="transparent")
        self.budget_overview.pack(fill=tk.X, padx=10, pady=(0, 6))

        self.ledger_filter = tk.StringVar(value="All")
        self._ledger_filter_ids: Dict[str, Optional[str]] = {"All": None}
        self.ledger_chips = ctk.CTkFrame(ledger, fg_color="transparent")
        self.ledger_chips.pack(fill=tk.X, padx=10, pady=(0, 6))

        list_frame = ctk.CTkFrame(ledger, fg_color=C["surface"], corner_radius=12)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        cols = ("date", "merchant", "amount")
        self.trans_tree = ttk.Treeview(list_frame, columns=cols, show="headings", height=8)
        for c, t, w in (("date", "Date", 72), ("merchant", "Merchant", 110), ("amount", "$", 54)):
            self.trans_tree.heading(c, text=t)
            self.trans_tree.column(c, width=w, anchor="w" if c != "amount" else "e")
        self.trans_tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.trans_tree.bind("<Delete>", lambda _e: self._delete_selected_transaction())

        # Hidden vars used by legacy helpers / settings persistence
        self.profile_title = tk.StringVar(value="")
        self.profile_subtitle = tk.StringVar(value="")
        self.profile_search = tk.StringVar(value="")
        self.employee_amount = tk.StringVar()
        self.employee_merchant = tk.StringVar()
        self.employee_combo_var = tk.StringVar(value="")
        self._profile_list_ids: List[str] = []
        self.record_buttons: Dict[str, ctk.CTkButton] = {}
        self.nav_buttons: Dict[str, ctk.CTkButton] = {}

        footer = ctk.CTkFrame(shell, fg_color="transparent", height=28)
        footer.pack(fill=tk.X, padx=16, pady=(0, 10))
        footer.pack_propagate(False)
        ctk.CTkLabel(
            footer,
            textvariable=self.status,
            font=("Avenir Next", 10),
            text_color=C["muted"],
            anchor="w",
        ).pack(fill=tk.X)

        self._refresh_employee_list()
        self._refresh_transaction_list()

    def _show_view(self, view_name: str):
        # Tabs removed — keep as no-op for any leftover callers.
        if view_name == "profiles" and self.selected_profile_id:
            self._open_employee_modal(self.selected_profile_id)

    def _run_context_action(self):
        self._sync_profiles()

    def _open_settings_modal(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Settings")
        dialog.geometry("380x520")
        dialog.resizable(False, False)
        dialog.configure(fg_color=C["bg"])
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        card = ctk.CTkFrame(
            dialog,
            fg_color=C["card"],
            corner_radius=18,
            border_width=1,
            border_color=C["border"],
        )
        card.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)
        ctk.CTkLabel(
            card,
            text="Settings",
            font=("Avenir Next", 16, "bold"),
            text_color=C["text"],
        ).pack(anchor="w", padx=18, pady=(16, 10))

        form = ctk.CTkScrollableFrame(card, fg_color="transparent")
        form.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 8))

        def labeled_entry(label: str, var: tk.Variable, show: str = "") -> None:
            ctk.CTkLabel(
                form,
                text=label.upper(),
                font=("Avenir Next", 9, "bold"),
                text_color=C["muted"],
                anchor="w",
            ).pack(fill=tk.X, pady=(8, 4))
            ctk.CTkEntry(
                form,
                textvariable=var,
                height=36,
                corner_radius=10,
                border_width=1,
                border_color=C["border"],
                fg_color=C["surface"],
                show=show,
                font=("SF Pro Text", 12),
            ).pack(fill=tk.X)

        labeled_entry("Shared passphrase", self.shared_passphrase, show="•")
        labeled_entry("Vault collection", self.collection_name)

        for label, var in (
            ("Auto-import HQ files", self.auto_import),
            ("Sync on startup", self.sync_on_startup),
            ("Create Outlook", self.provision_outlook),
            ("Create Hyatt", self.provision_hyatt),
            ("Create Marriott", self.provision_marriott),
        ):
            row = ctk.CTkFrame(form, fg_color="transparent")
            row.pack(fill=tk.X, pady=4)
            ctk.CTkLabel(row, text=label, font=("Avenir Next", 12), text_color=C["text"]).pack(
                side=tk.LEFT
            )
            ctk.CTkSwitch(
                row,
                text="",
                variable=var,
                width=42,
                fg_color=C["border"],
                progress_color=C["text"],
            ).pack(side=tk.RIGHT)

        ctk.CTkLabel(
            form,
            text="LOCAL DELETE",
            font=("Avenir Next", 9, "bold"),
            text_color=C["muted"],
            anchor="w",
        ).pack(fill=tk.X, pady=(10, 4))
        ctk.CTkOptionMenu(
            form,
            variable=self.local_delete_mode,
            values=list(LOCAL_DELETE_MODES),
            height=34,
            corner_radius=10,
            fg_color=C["surface"],
            button_color=C["card_hi"],
            button_hover_color="#dedee2",
            text_color=C["text"],
            dropdown_fg_color=C["card"],
            font=("Avenir Next", 11),
        ).pack(fill=tk.X)
        ctk.CTkLabel(
            form,
            text="VAULT CLEANUP",
            font=("Avenir Next", 9, "bold"),
            text_color=C["muted"],
            anchor="w",
        ).pack(fill=tk.X, pady=(10, 4))
        ctk.CTkOptionMenu(
            form,
            variable=self.bw_shred_mode,
            values=list(BW_SHRED_MODES),
            height=34,
            corner_radius=10,
            fg_color=C["surface"],
            button_color=C["card_hi"],
            button_hover_color="#dedee2",
            text_color=C["text"],
            dropdown_fg_color=C["card"],
            font=("Avenir Next", 11),
        ).pack(fill=tk.X)

        def save():
            self._save_settings()
            dialog.destroy()

        ctk.CTkButton(
            card,
            text="Save",
            command=save,
            height=40,
            corner_radius=12,
            fg_color=C["text"],
            hover_color="#323238",
            text_color="#ffffff",
            font=("Avenir Next", 12, "bold"),
        ).pack(fill=tk.X, padx=18, pady=(4, 16))

    def _open_employee_modal(self, employee_id: str):
        profile = self.profile_store.get(employee_id)
        if not profile:
            return
        self.selected_profile_id = employee_id
        self.selected_employee = profile.get("display_name")
        self._clear_profile_secrets()
        self._expand_window(True)

        dialog = ctk.CTkToplevel(self)
        dialog.title(profile.get("display_name", "Employee"))
        dialog.geometry("520x640")
        dialog.configure(fg_color=C["bg"])
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        self._employee_modal = dialog

        def on_close():
            self._clear_profile_secrets()
            self._expand_window(False)
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()
            self._employee_modal = None

        dialog.protocol("WM_DELETE_WINDOW", on_close)

        card = ctk.CTkFrame(
            dialog,
            fg_color=C["card"],
            corner_radius=18,
            border_width=1,
            border_color=C["border"],
        )
        card.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill=tk.X, padx=16, pady=(14, 8))
        ctk.CTkLabel(
            top,
            text=profile.get("display_name", "Employee"),
            font=("Avenir Next", 18, "bold"),
            text_color=C["text"],
        ).pack(side=tk.LEFT)
        ctk.CTkButton(
            top,
            text="✕",
            command=on_close,
            width=28,
            height=28,
            corner_radius=8,
            fg_color=C["card_hi"],
            hover_color="#dedee2",
            text_color=C["text"],
        ).pack(side=tk.RIGHT)

        self.profile_subtitle.set("Loading…")
        ctk.CTkLabel(
            card,
            textvariable=self.profile_subtitle,
            font=("Avenir Next", 11),
            text_color=C["muted"],
            anchor="w",
        ).pack(fill=tk.X, padx=16)

        rail = ctk.CTkFrame(card, fg_color=C["card_hi"], corner_radius=12)
        rail.pack(fill=tk.X, padx=14, pady=(10, 8))
        self.record_buttons = {}
        labels = {
            "identity": "Identity",
            "email_login": "Email",
            "work_card": "Card",
            "hyatt_login": "Hyatt",
            "marriott_login": "Marriott",
        }
        for role in RECORD_ROLES:
            btn = ctk.CTkButton(
                rail,
                text=labels.get(role, role),
                command=lambda r=role: self._show_profile_record(r),
                width=86,
                height=30,
                corner_radius=9,
                fg_color="transparent",
                hover_color="#e7e7ea",
                text_color=C["muted"],
                font=("Avenir Next", 10, "bold"),
            )
            btn.pack(side=tk.LEFT, padx=3, pady=4)
            self.record_buttons[role] = btn

        self.profile_viewer = ctk.CTkScrollableFrame(
            card,
            fg_color=C["surface"],
            corner_radius=12,
        )
        self.profile_viewer.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 8))

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill=tk.X, padx=14, pady=(0, 14))
        self.profile_edit_button = ctk.CTkButton(
            actions,
            text="Edit",
            command=self._edit_selected_identity,
            width=72,
            height=34,
            corner_radius=10,
            fg_color=C["text"],
            hover_color="#323238",
            text_color="#ffffff",
            font=("Avenir Next", 11, "bold"),
        )
        self.profile_edit_button.pack(side=tk.LEFT)
        ctk.CTkButton(
            actions,
            text="Resume",
            command=self._resume_profile_accounts,
            width=78,
            height=34,
            corner_radius=10,
            fg_color=C["card_hi"],
            hover_color="#dedee2",
            text_color=C["text"],
            font=("Avenir Next", 11, "bold"),
        ).pack(side=tk.LEFT, padx=6)
        ctk.CTkButton(
            actions,
            text="Spend",
            command=self._configure_selected_budget,
            width=72,
            height=34,
            corner_radius=10,
            fg_color=C["card_hi"],
            hover_color="#dedee2",
            text_color=C["text"],
            font=("Avenir Next", 11, "bold"),
        ).pack(side=tk.LEFT)
        self.profile_restore_button = ctk.CTkButton(
            actions,
            text="Restore",
            command=self._restore_selected_profile,
            width=72,
            height=34,
            corner_radius=10,
            fg_color=C["card_hi"],
            hover_color="#dedee2",
            text_color=C["text"],
            font=("Avenir Next", 11, "bold"),
        )
        self.profile_restore_button.pack(side=tk.RIGHT, padx=(6, 0))
        self.profile_delete_button = ctk.CTkButton(
            actions,
            text="Delete",
            command=self._delete_selected_profile,
            width=72,
            height=34,
            corner_radius=10,
            fg_color="transparent",
            hover_color="#fee2e2",
            text_color=C["danger"],
            font=("Avenir Next", 11, "bold"),
        )
        self.profile_delete_button.pack(side=tk.RIGHT)

        self.selected_record_role = "identity"
        self._render_profile_viewer("Loading…")

        def load():
            try:
                bundle = self.profile_sync.get_bundle(employee_id)
                self.after(0, lambda: self._apply_profile_bundle(employee_id, bundle))
            except Exception as exc:
                self.after(0, lambda error=exc: self._profile_load_failed(employee_id, error))

        threading.Thread(target=load, daemon=True).start()

    def _expand_window(self, large: bool) -> None:
        root = self.app.root
        try:
            if large:
                root.geometry("760x640+80+40")
                root.minsize(640, 560)
            else:
                root.geometry("480x560+120+60")
                root.minsize(420, 480)
        except tk.TclError:
            pass

    def _refresh_profiles_list(self):
        self._refresh_active_employees()
        if self.selected_profile_id and getattr(self, "_employee_modal", None):
            try:
                if self._employee_modal.winfo_exists() and not self.profile_bundle:
                    # reload after sync
                    eid = self.selected_profile_id
                    def load():
                        try:
                            bundle = self.profile_sync.get_bundle(eid)
                            self.after(0, lambda: self._apply_profile_bundle(eid, bundle))
                        except Exception as exc:
                            self.after(0, lambda error=exc: self._profile_load_failed(eid, error))
                    threading.Thread(target=load, daemon=True).start()
            except tk.TclError:
                pass

    def _on_profile_selected(self, _event=None):
        if self.selected_profile_id:
            self._open_employee_modal(self.selected_profile_id)

    def _apply_profile_bundle(self, employee_id: str, bundle: Dict[str, Dict[str, Any]]):
        if self.selected_profile_id != employee_id:
            bundle.clear()
            return
        self.profile_bundle = bundle
        filled = 0
        identity = bundle.get("identity") or {}
        if identity and not identity.get("_load_error"):
            rows = self._identity_view_rows(identity)
            filled = sum(1 for _l, v, _s in rows if v and v != "—")
        self.profile_subtitle.set(
            f"{len(bundle)}/5 vault records"
            + (f"  ·  {filled} identity fields" if filled else "")
            + f"  ·  {datetime.now().strftime('%H:%M')}"
        )
        self._show_profile_record(self.selected_record_role)
        profile = self.profile_store.get(employee_id) or {}
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
        if not hasattr(self, "profile_edit_button"):
            return
        deletion = profile.get("deletion") or {}
        pending = deletion.get("status") in {"pending", "partial", "purge_failed"}
        try:
            self.profile_restore_button.configure(state=tk.NORMAL if pending else tk.DISABLED)
            self.profile_delete_button.configure(state=tk.DISABLED if pending else tk.NORMAL)
            self.profile_edit_button.configure(
                state=tk.NORMAL if "identity" in self.profile_bundle and not pending else tk.DISABLED
            )
        except tk.TclError:
            pass

    def _show_profile_record(self, role: str):
        self.selected_record_role = role
        for key, button in self.record_buttons.items():
            exists = key in self.profile_bundle
            try:
                button.configure(
                    fg_color=C["text"] if key == role else "transparent",
                    text_color="#ffffff" if key == role else (C["text"] if exists else C["muted"]),
                )
            except tk.TclError:
                pass
        self._render_profile_viewer()

    @staticmethod
    def _identity_view_rows(item: Dict[str, Any]) -> List[Tuple[str, str, bool]]:
        identity = item.get("identity") or {}
        # Legacy Secure Note imports may have dropped native identity fields.
        item_name = str(item.get("name") or "")
        display_name = item_name.rsplit(" — ", 1)[0] if " — " in item_name else item_name
        if not identity.get("firstName") and display_name:
            parts = display_name.split()
            if len(parts) >= 2 and not identity.get("firstName"):
                identity = {
                    **identity,
                    "firstName": identity.get("firstName") or parts[0],
                    "lastName": identity.get("lastName") or " ".join(parts[1:]),
                }
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
            ("Company", "company"),
            ("Username", "username"),
        )
        rows: List[Tuple[str, str, bool]] = []
        for label, key in keys:
            if key == "_displayName":
                value = display_name
            else:
                value = identity.get(key)
            if value in (None, ""):
                continue
            rows.append((label, str(value), key == "ssn"))
        hidden_fields = {EMPLOYEE_ID_FIELD, RECORD_ROLE_FIELD}
        for field in item.get("fields") or []:
            field_name = str(field.get("name") or "").strip()
            if not field_name or field_name in hidden_fields:
                continue
            value = str(field.get("value") or "").strip()
            if not value:
                continue
            sensitive_name = field_name.casefold()
            is_sensitive = any(
                token in sensitive_name
                for token in ("birth", "dob", "social", "ssn", "passport", "license", "password")
            )
            rows.append((field_name, value, is_sensitive))
        notes = str(item.get("notes") or "").strip()
        if notes:
            rows.append(("Notes", notes, False))
        return rows

    def _render_profile_viewer(self, message: Optional[str] = None):
        if not hasattr(self, "profile_viewer"):
            return
        try:
            if not self.profile_viewer.winfo_exists():
                return
        except tk.TclError:
            return
        for child in self.profile_viewer.winfo_children():
            child.destroy()
        if message:
            ctk.CTkLabel(
                self.profile_viewer,
                text=message,
                text_color=C["muted"],
                font=("Avenir Next", 11),
                wraplength=440,
                justify="left",
            ).pack(anchor="w", padx=14, pady=14)
            return
        role = self.selected_record_role
        item = self.profile_bundle.get(role)
        if item is None:
            ctk.CTkLabel(
                self.profile_viewer,
                text="Not created yet",
                text_color=C["muted"],
                font=("Avenir Next", 12, "bold"),
            ).pack(anchor="w", padx=14, pady=(14, 4))
            ctk.CTkLabel(
                self.profile_viewer,
                text="Use Resume to provision this account, or wait for HQ auto-import.",
                text_color=C["muted"],
                font=("Avenir Next", 11),
                wraplength=420,
                justify="left",
            ).pack(anchor="w", padx=14)
            return
        if item.get("_load_error"):
            ctk.CTkLabel(
                self.profile_viewer,
                text="Record unavailable",
                text_color=C["text"],
                font=("Avenir Next", 12, "bold"),
            ).pack(anchor="w", padx=14, pady=(14, 6))
            ctk.CTkLabel(
                self.profile_viewer,
                text="Could not load this Bitwarden item. Sync and try again.",
                text_color=C["muted"],
                font=("Avenir Next", 11),
                wraplength=420,
                justify="left",
            ).pack(anchor="w", padx=14)
            return

        ctk.CTkLabel(
            self.profile_viewer,
            text=str(item.get("name") or role),
            text_color=C["text"],
            font=("Avenir Next", 12, "bold"),
        ).pack(anchor="w", padx=14, pady=(12, 8))

        if role == "identity":
            rows = self._identity_view_rows(item)
        elif role == "work_card":
            card = item.get("card") or {}
            rows = [
                (label, str(value), sensitive)
                for label, value, sensitive in (
                    ("Cardholder", card.get("cardholderName"), False),
                    ("Brand", card.get("brand"), False),
                    ("Number", card.get("number"), True),
                    ("CVV", card.get("code"), True),
                    ("Expires", f"{card.get('expMonth') or '—'}/{card.get('expYear') or '—'}", False),
                )
                if value not in (None, "", "—/—")
            ]
        else:
            login = item.get("login") or {}
            uris = login.get("uris") or []
            uri = uris[0].get("uri") if uris else None
            rows = [
                (label, str(value), sensitive)
                for label, value, sensitive in (
                    ("Username", login.get("username"), False),
                    ("Password", login.get("password"), True),
                    ("Website", uri, False),
                )
                if value not in (None, "")
            ]

        if not rows:
            ctk.CTkLabel(
                self.profile_viewer,
                text="No fields stored on this item yet.",
                text_color=C["muted"],
                font=("Avenir Next", 11),
            ).pack(anchor="w", padx=14, pady=8)
            return

        for index, (label, value, sensitive) in enumerate(rows):
            row = ctk.CTkFrame(
                self.profile_viewer,
                fg_color="#f1f1f3" if index % 2 == 0 else "#f7f7f8",
                corner_radius=10,
            )
            row.pack(fill=tk.X, padx=10, pady=3)
            ctk.CTkLabel(
                row,
                text=label,
                anchor="w",
                width=100,
                text_color=C["muted"],
                font=("Avenir Next", 10, "bold"),
            ).pack(side=tk.LEFT, padx=(10, 6), pady=8)
            reveal_key = (role, label)
            shown = value
            if sensitive and reveal_key not in self._revealed_profile_values:
                shown = "••••••••"
            ctk.CTkLabel(
                row,
                text=shown,
                anchor="w",
                text_color=C["text"],
                font=("SF Mono", 11),
            ).pack(side=tk.LEFT, fill=tk.X, expand=True, pady=8)
            if sensitive:
                ctk.CTkButton(
                    row,
                    text="Hide" if reveal_key in self._revealed_profile_values else "Show",
                    command=lambda key=reveal_key: self._toggle_profile_reveal(key),
                    width=48,
                    height=24,
                    corner_radius=7,
                    fg_color=C["card"],
                    hover_color="#e4e4e7",
                    text_color=C["text"],
                    font=("Avenir Next", 9, "bold"),
                ).pack(side=tk.RIGHT, padx=8, pady=6)

    def _toggle_profile_reveal(self, key: Tuple[str, str]):
        if key in self._revealed_profile_values:
            self._revealed_profile_values.remove(key)
        else:
            self._revealed_profile_values.add(key)
        self._render_profile_viewer()

    def _sync_profiles(self):
        self.status.set("Syncing…")
        self._clear_profile_secrets()

        def sync():
            try:
                self.profile_sync.sync_profiles()
                self.after(0, self._profile_sync_complete)
            except Exception as exc:
                self.after(0, lambda error=exc: self._profile_sync_failed(error))

        threading.Thread(target=sync, daemon=True).start()

    def _profile_sync_complete(self):
        self.status.set("Synced")
        self._refresh_profiles_list()
        self._refresh_employee_list()

    def _profile_sync_failed(self, error: Exception):
        self.status.set("Sync failed")
        messagebox.showerror("Sync", str(error), parent=self)

    def _resume_profile_accounts(self):
        profile = self.profile_store.get(self.selected_profile_id or "")
        if not profile:
            return
        self.selected_employee = profile.get("display_name")
        self.resume_selected_employee()

    def _edit_selected_identity(self):
        profile = self.profile_store.get(self.selected_profile_id or "")
        item = self.profile_bundle.get("identity")
        if not profile or not item:
            return
        parent = getattr(self, "_employee_modal", None) or self
        dialog = ctk.CTkToplevel(parent)
        dialog.title("Edit identity")
        dialog.geometry("360x420")
        dialog.configure(fg_color=C["bg"])
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        form = ctk.CTkScrollableFrame(dialog, fg_color=C["card"], corner_radius=14)
        form.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
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
        for label, key in fields:
            ctk.CTkLabel(
                form,
                text=label.upper(),
                font=("Avenir Next", 9, "bold"),
                text_color=C["muted"],
                anchor="w",
            ).pack(fill=tk.X, pady=(8, 3))
            variables[key] = tk.StringVar(value=str(identity.get(key) or ""))
            ctk.CTkEntry(
                form,
                textvariable=variables[key],
                height=34,
                corner_radius=10,
                border_width=1,
                border_color=C["border"],
                fg_color=C["surface"],
            ).pack(fill=tk.X)

        def save():
            updates = {key: variable.get().strip() for key, variable in variables.items()}
            if not updates["firstName"] or not updates["lastName"]:
                messagebox.showerror("Identity", "First and last name are required.", parent=dialog)
                return
            dialog.destroy()
            self._save_identity_updates(profile["employee_id"], updates, item.get("revisionDate"))

        ctk.CTkButton(
            form,
            text="Save",
            command=save,
            height=38,
            corner_radius=11,
            fg_color=C["text"],
            hover_color="#323238",
            text_color="#ffffff",
            font=("Avenir Next", 12, "bold"),
        ).pack(fill=tk.X, pady=(16, 8))

    def _save_identity_updates(
        self,
        employee_id: str,
        updates: Dict[str, str],
        expected_revision: Optional[str],
    ):
        self.status.set("Saving identity…")

        def worker():
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

        threading.Thread(target=worker, daemon=True).start()

    def _identity_saved(self, employee_id: str, item: Dict[str, Any]):
        if self.selected_profile_id == employee_id:
            self.profile_bundle["identity"] = item
            self._show_profile_record("identity")
        self.status.set("Identity saved")
        self.audit.log_security_event("profile_identity_edit", f"employee_id={employee_id} result=success")

    def _identity_save_failed(self, employee_id: str, error: Exception):
        self.status.set("Identity save failed")
        self.audit.log_security_event(
            "profile_identity_edit",
            f"employee_id={employee_id} result=failed",
        )
        messagebox.showerror("Identity", str(error), parent=self)

    @staticmethod
    def _redacted_item_ids(item_ids: List[str]) -> str:
        return f"{len(item_ids)} item(s)"

    def _delete_selected_profile(self):
        employee_id = self.selected_profile_id
        profile = self.profile_store.get(employee_id or "")
        if not employee_id or not profile:
            return
        if not messagebox.askyesno(
            "Delete profile",
            f"Trash Bitwarden items for {profile.get('display_name')}?\n"
            "Restore remains available for two days.",
            parent=self,
        ):
            return

        def worker():
            try:
                result = self.profile_sync.trash_bundle(employee_id)
                self.after(0, lambda: self._profile_trash_complete(employee_id, result))
            except Exception as exc:
                self.after(
                    0,
                    lambda error=exc: messagebox.showerror("Delete", str(error), parent=self),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _profile_trash_complete(self, employee_id: str, result: Dict[str, List[str]]):
        self.audit.log_security_event(
            "profile_trash",
            f"employee_id={employee_id} trashed={self._redacted_item_ids(result.get('trashed', []))} "
            f"failed={self._redacted_item_ids(result.get('failed', []))}",
        )
        self._clear_profile_secrets()
        self._refresh_employee_list()
        self.status.set("Profile moved to trash")
        modal = getattr(self, "_employee_modal", None)
        if modal is not None:
            try:
                modal.destroy()
            except tk.TclError:
                pass
            self._employee_modal = None
            self._expand_window(False)

    def _restore_selected_profile(self):
        employee_id = self.selected_profile_id
        if not employee_id:
            return

        def worker():
            try:
                result = self.profile_sync.restore_bundle(employee_id)
                self.after(0, lambda: self._profile_restore_complete(employee_id, result))
            except Exception as exc:
                self.after(
                    0,
                    lambda error=exc: messagebox.showerror("Restore", str(error), parent=self),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _profile_restore_complete(self, employee_id: str, result: Dict[str, List[str]]):
        self.audit.log_security_event(
            "profile_restore",
            f"employee_id={employee_id} restored={self._redacted_item_ids(result.get('restored', []))} "
            f"failed={self._redacted_item_ids(result.get('failed', []))}",
        )
        self.status.set("Profile restored")
        self._refresh_employee_list()
        if self.selected_profile_id == employee_id:
            self._open_employee_modal(employee_id)

    def _set_step(self, key: str, detail: str = "") -> None:
        labels = {
            "intake": "Intake",
            "convert": "Convert",
            "import": "Import",
            "accounts": "Accounts",
            "lockdown": "Cleanup",
            "done": "Done",
        }
        self.workflow_step.set(key)
        text = labels.get(key, key)
        if detail:
            text = f"{text}: {detail}"
        self.status.set(text)

    def _save_settings(self):
        passphrase = self.shared_passphrase.get().strip()
        payload = {
            "collection_name": self.collection_name.get().strip() or "Personal Vault",
            "auto_import": "true" if self.auto_import.get() else "false",
            "sync_on_startup": "true" if self.sync_on_startup.get() else "false",
            "provision_outlook": "true" if self.provision_outlook.get() else "false",
            "provision_hyatt": "true" if self.provision_hyatt.get() else "false",
            "provision_marriott": "true" if self.provision_marriott.get() else "false",
            "local_delete_mode": self.local_delete_mode.get(),
            "bw_shred_mode": self.bw_shred_mode.get(),
        }
        if len(passphrase) >= 8:
            payload["shared_passphrase"] = passphrase
        self.store.update(payload)
        self.status.set("Settings saved")

    def _build_ledger_chips(self, names: List[str]) -> None:
        if not hasattr(self, "ledger_chips"):
            return
        for child in self.ledger_chips.winfo_children():
            child.destroy()
        self._ledger_filter_ids = {"All": None}
        for name in ["All", *names]:
            self._ledger_filter_ids[name] = None if name == "All" else self._ledger_employee_map.get(name)

            def select(n=name):
                self.ledger_filter.set(n)
                self._refresh_transaction_list()
                self._build_ledger_chips(names)

            ctk.CTkButton(
                self.ledger_chips,
                text=name if name == "All" else (name.split()[0] if name.split() else name),
                command=select,
                width=52,
                height=26,
                corner_radius=8,
                fg_color=C["text"] if self.ledger_filter.get() == name else C["card_hi"],
                hover_color="#323238" if self.ledger_filter.get() == name else "#dedee2",
                text_color="#ffffff" if self.ledger_filter.get() == name else C["text"],
                font=("Avenir Next", 9, "bold"),
            ).pack(side=tk.LEFT, padx=2)

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
            self.run_pipeline(quiet=True)

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
                            self.after(0, lambda: self.run_pipeline(quiet=True))
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
                "Already running",
                "Wait for the current run to finish.",
                parent=self,
            )
            return
        if not self.selected_employee:
            messagebox.showinfo(
                "Select an employee",
                "Open an employee before resuming account creation.",
                parent=self,
            )
            return
        passphrase = self._resolve_passphrase()
        if not passphrase:
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
                    lambda: self.status.set(f"Accounts updated for {employee_name}"),
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

    def _resolve_passphrase(self) -> Optional[str]:
        passphrase = self.shared_passphrase.get().strip()
        if len(passphrase) < 8:
            passphrase = self.store.get("shared_passphrase", "").strip()
            if passphrase:
                self.shared_passphrase.set(passphrase)
        if len(passphrase) >= 8:
            return passphrase
        entered = simpledialog.askstring(
            "Shared passphrase",
            "Enter the shared employee passphrase (8+ characters).\n"
            "It will be saved for automatic HQ imports.",
            show="*",
            parent=self,
        )
        if not entered or len(entered.strip()) < 8:
            self.status.set("Passphrase required for provisioning")
            return None
        passphrase = entered.strip()
        self.shared_passphrase.set(passphrase)
        self.store.update({"shared_passphrase": passphrase})
        return passphrase

    # --- Pipeline -----------------------------------------------------
    def run_pipeline(self, *, quiet: bool = False):
        if self._pipeline_running:
            if not quiet:
                messagebox.showinfo(
                    "Already running",
                    "Wait for the current run to finish.",
                    parent=self,
                )
            return
        queued = self._queued_employee_files()
        if not queued:
            if not quiet:
                messagebox.showinfo(
                    "Nothing queued",
                    "Drop HQ-*.txt / HQ-*.rtf into Downloads.",
                    parent=self,
                )
            return
        passphrase = self._resolve_passphrase()
        if not passphrase:
            return
        collection = self.collection_name.get().strip() or "Personal Vault"
        self.store.update({"collection_name": collection})
        previous_employee_ids = {
            profile["employee_id"]
            for profile in self.profile_store.list_profiles(include_purged=True)
        }

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
        self.status.set(f"Importing {len(queued)} HQ file(s)…")
        threading.Thread(target=worker, daemon=True).start()

    def _pipeline_finished(self):
        self._pipeline_running = False

    def _on_onboarding_complete(self, previous_employee_ids: Set[str]):
        self._refresh_queued_files()
        self._refresh_employee_list()
        self._prompt_new_employee_budgets(previous_employee_ids)
        self.status.set("Onboarding complete")
        self._sync_profiles()

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
        logging.info(msg)
        short = msg if len(msg) < 72 else msg[:69] + "…"
        try:
            self.status.set(short)
        except tk.TclError:
            pass

    # --- Transactions helpers -----------------------------------------
    def _refresh_employee_list(self):
        profiles = self.profile_store.list_profiles()
        self._ledger_employee_map = {
            profile["display_name"]: profile["employee_id"]
            for profile in profiles
        }
        names = sorted(self._ledger_employee_map)
        if self.ledger_filter.get() not in {"All", *names}:
            self.ledger_filter.set("All")
        self._build_ledger_chips(names)
        self._refresh_active_employees()

    def _refresh_active_employees(self):
        if not hasattr(self, "employee_grid"):
            return
        profiles = self.profile_store.list_profiles()
        for child in self.employee_grid.winfo_children():
            child.destroy()
        self.employee_count.set(str(len(profiles)))
        if not profiles:
            ctk.CTkLabel(
                self.employee_grid,
                text="Waiting for HQ-*.txt in Downloads",
                font=("Avenir Next", 11),
                text_color=C["muted"],
                wraplength=180,
                justify="left",
            ).pack(anchor="w", padx=8, pady=24)
            return
        for profile in profiles:
            name = profile.get("display_name", "Unknown")
            employee_id = profile["employee_id"]
            refs = profile.get("vault_refs") or {}
            completed = len(refs)
            deletion = profile.get("deletion") or {}
            status = str(deletion.get("status") or profile.get("status") or "active").title()
            initials = "".join(part[0] for part in name.split() if part)[:2].upper()
            selected = employee_id == self.selected_profile_id
            row = ctk.CTkFrame(
                self.employee_grid,
                fg_color=C["card_hi"] if selected else "transparent",
                corner_radius=12,
                cursor="hand2",
            )
            row.pack(fill=tk.X, pady=2)
            avatar = ctk.CTkFrame(
                row,
                width=34,
                height=34,
                corner_radius=17,
                fg_color=C["text"],
            )
            avatar.pack(side=tk.LEFT, padx=(8, 8), pady=8)
            avatar.pack_propagate(False)
            ctk.CTkLabel(
                avatar,
                text=initials or "—",
                text_color="#ffffff",
                font=("Avenir Next", 10, "bold"),
            ).pack(expand=True)
            copy = ctk.CTkFrame(row, fg_color="transparent")
            copy.pack(side=tk.LEFT, fill=tk.X, expand=True)
            ctk.CTkLabel(
                copy,
                text=name,
                font=("Avenir Next", 12, "bold"),
                text_color=C["text"],
                anchor="w",
            ).pack(fill=tk.X)
            meta = profile.get("email") or f"{completed}/5 records · {status}"
            ctk.CTkLabel(
                copy,
                text=meta,
                font=("Avenir Next", 9),
                text_color=C["muted"],
                anchor="w",
            ).pack(fill=tk.X)
            CompletionRing(row, completed * 20, size=34).pack(side=tk.RIGHT, padx=8)

            def open_profile(_event=None, eid=employee_id):
                self._open_employee_modal(eid)

            for widget in (row, avatar, copy):
                widget.bind("<Button-1>", open_profile)
            for child in list(copy.winfo_children()) + list(avatar.winfo_children()):
                child.bind("<Button-1>", open_profile)

    def _select_employee(self, employee_name: str):
        self.selected_employee = employee_name
        profile = next(
            (
                p
                for p in self.profile_store.list_profiles()
                if p.get("display_name") == employee_name
            ),
            None,
        )
        if profile:
            self._open_employee_modal(profile["employee_id"])

    def _refresh_transaction_list(self):
        if not hasattr(self, "trans_tree"):
            return
        for item in self.trans_tree.get_children():
            self.trans_tree.delete(item)
        selected_name = self.ledger_filter.get()
        employee_id = None
        if selected_name not in {"All", "All employees", ""}:
            employee_id = getattr(self, "_ledger_employee_map", {}).get(selected_name)
        if employee_id:
            transactions = self.transaction_db.get_transactions_by_employee_id(employee_id)
        else:
            transactions = self.transaction_db.get_all_transactions()
        for trans in transactions[:40]:
            self.trans_tree.insert(
                "",
                "end",
                iid=str(trans["id"]),
                values=(
                    trans["date"],
                    trans["merchant"],
                    f"{trans['amount']:.0f}",
                ),
            )
        filter_name = selected_name if selected_name not in {"All", ""} else "All employees"
        self._refresh_budget_overview(filter_name)

    def _add_transaction_dialog(self):
        names = sorted(getattr(self, "_ledger_employee_map", {}))
        if not names:
            messagebox.showinfo("Ledger", "No employees yet.", parent=self)
            return
        dialog = ctk.CTkToplevel(self)
        dialog.title("Add spend")
        dialog.geometry("320x300")
        dialog.configure(fg_color=C["bg"])
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()
        form = ctk.CTkFrame(dialog, fg_color=C["card"], corner_radius=14)
        form.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        merchant = tk.StringVar()
        amount = tk.StringVar()
        employee = tk.StringVar(value=names[0])
        for label, var in (("Merchant", merchant), ("Amount", amount)):
            ctk.CTkLabel(
                form,
                text=label.upper(),
                font=("Avenir Next", 9, "bold"),
                text_color=C["muted"],
                anchor="w",
            ).pack(fill=tk.X, padx=14, pady=(10, 3))
            ctk.CTkEntry(
                form,
                textvariable=var,
                height=34,
                corner_radius=10,
                border_width=1,
                border_color=C["border"],
                fg_color=C["surface"],
            ).pack(fill=tk.X, padx=14)
        ctk.CTkLabel(
            form,
            text="EMPLOYEE",
            font=("Avenir Next", 9, "bold"),
            text_color=C["muted"],
            anchor="w",
        ).pack(fill=tk.X, padx=14, pady=(10, 3))
        ctk.CTkOptionMenu(
            form,
            variable=employee,
            values=names,
            height=34,
            corner_radius=10,
            fg_color=C["surface"],
            button_color=C["card_hi"],
            text_color=C["text"],
        ).pack(fill=tk.X, padx=14)

        def save():
            self.employee_merchant.set(merchant.get())
            self.employee_amount.set(amount.get())
            self.employee_combo_var.set(employee.get())
            dialog.destroy()
            self._add_transaction()

        ctk.CTkButton(
            form,
            text="Add",
            command=save,
            height=36,
            corner_radius=11,
            fg_color=C["text"],
            hover_color="#323238",
            text_color="#ffffff",
            font=("Avenir Next", 12, "bold"),
        ).pack(fill=tk.X, padx=14, pady=16)

    def _refresh_budget_overview(self, selected_name: str = "All employees"):
        if not hasattr(self, "budget_overview"):
            return
        for child in self.budget_overview.winfo_children():
            child.destroy()
        budget_by_id = {
            budget["employee_id"]: budget
            for budget in self.transaction_db.get_employee_budgets()
        }
        profiles = self.profile_store.list_profiles()
        if selected_name not in {"All employees", "All", ""}:
            profiles = [
                profile
                for profile in profiles
                if profile.get("display_name") == selected_name
            ]
        visible = profiles[:5]
        if not visible:
            ctk.CTkLabel(
                self.budget_overview,
                text="Spend limits appear after import",
                text_color=C["muted"],
                font=("Avenir Next", 10),
            ).pack(anchor="w", padx=4, pady=4)
            return
        for profile in visible:
            budget = budget_by_id.get(profile["employee_id"])
            row = ctk.CTkFrame(self.budget_overview, fg_color="transparent")
            row.pack(fill=tk.X, pady=2)
            name = profile.get("display_name", "Employee").split()[0]
            ctk.CTkLabel(
                row,
                text=name,
                width=56,
                anchor="w",
                font=("Avenir Next", 10, "bold"),
                text_color=C["text"],
            ).pack(side=tk.LEFT)
            if budget is None:
                ctk.CTkLabel(
                    row,
                    text="no limit",
                    font=("Avenir Next", 9),
                    text_color=C["muted"],
                ).pack(side=tk.LEFT)
                continue
            spent = budget["total_spent"]
            limit = budget["spend_limit"]
            ratio = spent / limit if limit else 0
            bar = ctk.CTkProgressBar(
                row,
                height=6,
                corner_radius=3,
                fg_color="#dedee3",
                progress_color=C["danger"] if ratio >= 1 else C["text"],
            )
            bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
            bar.set(min(max(ratio, 0), 1))
            ctk.CTkLabel(
                row,
                text=f"${spent:.0f}/${limit:.0f}",
                font=("Avenir Next", 9),
                text_color=C["muted"],
            ).pack(side=tk.RIGHT)

    def _configure_selected_budget(self):
        employee_id = self.selected_profile_id
        if not employee_id:
            selected_name = self.ledger_filter.get()
            employee_id = getattr(self, "_ledger_employee_map", {}).get(selected_name)
        if not employee_id:
            messagebox.showinfo(
                "Choose an employee",
                "Open an employee before setting a spend limit.",
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
        amount_str = self.employee_amount.get().strip()
        merchant = self.employee_merchant.get().strip()
        employee = self.employee_combo_var.get().strip()
        date = datetime.now().strftime("%Y-%m-%d")
        if not all([amount_str, merchant, employee]):
            messagebox.showerror("Error", "Merchant, amount, and employee are required.", parent=self)
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
            self.employee_amount.set("")
            self.employee_merchant.set("")
            self._refresh_transaction_list()
            self.status.set("Spend added")
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
