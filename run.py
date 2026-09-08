#!/usr/bin/env python3
"""
GUI runner for the employee onboarding pipeline.

Prefer:  .venv/bin/python run.py
Or:      ./run.py   (after chmod +x; uses PATH python3)
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _bootstrap_runtime() -> Path:
    """Ensure Homebrew CLI tools are visible when launched as a .app, and log crashes."""
    home = Path.home()
    log_path = home / ".downlowd_launch.log"
    extras = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(home / ".local" / "bin"),
    ]
    path = os.environ.get("PATH", "")
    parts = [p for p in path.split(":") if p]
    for extra in extras:
        if extra not in parts and Path(extra).exists():
            parts.insert(0, extra)
    os.environ["PATH"] = ":".join(parts)

    try:
        with log_path.open("w", encoding="utf-8") as fh:
            fh.write(f"argv={sys.argv!r}\n")
            fh.write(f"executable={sys.executable!r}\n")
            fh.write(f"PATH={os.environ.get('PATH')!r}\n")
            fh.write(f"cwd={os.getcwd()!r}\n")
            fh.flush()
    except OSError:
        pass
    return log_path


def main() -> None:
    """Main entry point for the GUI application."""
    log_path = _bootstrap_runtime()
    try:
        from gui import AppGUI

        app = AppGUI()
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write("AppGUI constructed; entering mainloop\n")
        except OSError:
            pass
        app.run()
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write("mainloop exited normally\n")
        except OSError:
            pass
    except Exception:
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write("FATAL:\n")
                fh.write(traceback.format_exc())
        except OSError:
            pass
        raise


if __name__ == "__main__":
    main()
