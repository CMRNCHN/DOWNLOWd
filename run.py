#!/usr/bin/env python3
"""
GUI runner for the employee onboarding pipeline.

Prefer:  .venv/bin/python run.py
Or:      ./run.py   (after chmod +x; uses PATH python3)
"""

from gui import AppGUI


def main():
    """Main entry point for the GUI application."""
    app = AppGUI()
    app.run()


if __name__ == "__main__":
    main()
