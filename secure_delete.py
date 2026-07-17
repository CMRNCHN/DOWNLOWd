"""
Best-effort local file disposal helpers.

On APFS + FileVault, multi-pass overwrite does not guarantee physical erasure
(SSD wear-leveling). Real protection is full-disk encryption. These modes are
still useful against casual recovery of freed filesystem blocks.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

# Dropdown values shown in Settings
LOCAL_DELETE_MODES = {
    "standard": "Standard delete (unlink only)",
    "overwrite": "Overwrite then delete (3-pass random)",
    "best_effort": "Best-effort secure erase (overwrite + fsync + unlink)",
}

BW_SHRED_MODES = {
    "off": "Keep Bitwarden items",
    "onboarding_items": "Shred onboarding-named items only",
    "all_collection": "Shred all items in the collection",
}

DEFAULT_LOCAL_DELETE_MODE = "overwrite"
DEFAULT_BW_SHRED_MODE = "off"


def secure_delete_file(file_path: Path, mode: str = DEFAULT_LOCAL_DELETE_MODE) -> None:
    """Delete a file according to the selected disposal mode."""
    path = Path(file_path)
    if not path.exists():
        return

    mode = mode if mode in LOCAL_DELETE_MODES else DEFAULT_LOCAL_DELETE_MODE

    if mode == "standard":
        path.unlink(missing_ok=True)
        logging.debug("Standard-deleted %s", path)
        return

    # overwrite + best_effort both overwrite; best_effort is explicit about fsync
    file_size = path.stat().st_size or 1
    passes = 3
    with open(path, "wb") as f:
        for _ in range(passes):
            f.write(os.urandom(file_size))
            f.flush()
            os.fsync(f.fileno())
    path.unlink()
    logging.debug("Securely deleted (%s) %s", mode, path)
