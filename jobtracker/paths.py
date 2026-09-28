"""Where the tracker keeps its data: the database, the config file and daily backups."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """The data folder: data/ in the project. Set JOBTRACKER_HOME to put it somewhere else."""
    env = os.environ.get("JOBTRACKER_HOME")
    path = Path(env).expanduser() if env else PROJECT_DIR / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "jobs.sqlite3"


def config_path() -> Path:
    return data_dir() / "config.json"


def backup_dir() -> Path:
    return data_dir() / "backups"
