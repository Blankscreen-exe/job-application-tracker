#!/usr/bin/env python3
"""Run the tracker from a checkout: `python jobs.py` (or use the launchers in bin/)."""

import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("Job Tracker needs Python 3.10+ (found %d.%d)" % sys.version_info[:2])

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jobtracker.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
