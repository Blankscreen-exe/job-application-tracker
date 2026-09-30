"""User settings, kept in config.json next to the database. Missing keys fall back to DEFAULTS."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

from . import paths

DEFAULTS: dict = {
    # First port to try; the next free one is used if it's taken.
    "port": 8765,
    # Every status an application can have, in pipeline order.
    "statuses": ["Applied", "Screening", "Interview", "Offer", "Rejected", "Ghosted", "Withdrawn"],
    # Statuses that count as "still in play" for the Active filter.
    "active_statuses": ["Applied", "Screening", "Interview", "Offer"],
    # Statuses that get flagged when nothing has happened for `stale_days`.
    "stale_statuses": ["Applied", "Screening", "Interview"],
    "stale_days": 14,
    # How far ahead the "Coming up" list looks.
    "upcoming_days": 14,
    # How far ahead the dashboard's reminders look.
    "reminder_days": 7,
    # Applications still at the first status with nothing for this many days can be marked Ghosted in one go.
    "ghosted_days": 30,
    # Daily database backups to keep.
    "backups_to_keep": 14,
}


def load(path: Path | None = None) -> dict:
    """Read the config, writing the defaults on first run so there's a file to edit."""
    path = path or paths.config_path()
    cfg = copy.deepcopy(DEFAULTS)
    if not path.exists():
        path.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
        return cfg
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: ignoring {path}: {exc}", file=sys.stderr)
        return cfg
    for key, default in DEFAULTS.items():
        value = user.get(key)
        if value is not None and isinstance(value, type(default)):
            cfg[key] = value
    return cfg
