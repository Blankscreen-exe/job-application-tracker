"""Exports: CSV files that open straight in Excel or Google Sheets, and a full JSON backup."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import date

from . import db

REMOTE_LABELS = {"": "", "remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}

# Same layout as the original spreadsheet, so a re-import (or a paste into the sheet) lines up.
APPLICATION_COLUMNS = [
    "Names", "Website", "Is Big Tech?", "Remote?", "Urgent", "Job post", "References", "Position",
    "Tools", "Date Applied", "Days Since Applied", "Weeks Since Applied", "Status", "Offer",
    "Joining Date", "Responsibilities", "Comments",
]
COMPANY_COLUMNS = [
    "Name", "Website", "Is Big Tech?", "Remote?", "Urgent", "What they do", "References", "Notes",
    "People", "Applications", "Latest position", "Latest status", "Last applied",
]


def _yes(value: bool) -> str:
    return "Yes" if value else "No"


def _csv(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def _latest(apps: list[dict]) -> dict | None:
    return max(apps, key=lambda a: (a["date_applied"], a["id"]), default=None)


def _grouped(conn: sqlite3.Connection, table: str) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for row in db.all_rows(conn, table):
        out.setdefault(row["company_id"], []).append(row)
    return out


def companies_csv(conn: sqlite3.Connection) -> str:
    apps = _grouped(conn, "applications")
    people = _grouped(conn, "contacts")
    rows = []
    for c in sorted(db.all_rows(conn, "companies"), key=lambda c: c["name"].lower()):
        mine = apps.get(c["id"], [])
        latest = _latest(mine)
        who = "; ".join(" - ".join(filter(None, [p["name"], p["role"]])) for p in people.get(c["id"], []))
        rows.append([
            c["name"], c["website"], _yes(c["is_big_tech"]), REMOTE_LABELS.get(c["remote"], c["remote"]),
            _yes(c["urgent"]), c["about"], c["refs"], c["notes"], who, len(mine),
            latest["position"] if latest else "", latest["status"] if latest else "",
            max((a["date_applied"] for a in mine), default=""),
        ])
    return _csv(COMPANY_COLUMNS, rows)


def applications_csv(conn: sqlite3.Connection, today: date | None = None) -> str:
    """One row per application; companies you haven't applied to yet get a row of their own."""
    today = today or date.today()
    apps = _grouped(conn, "applications")
    rows = []
    for c in sorted(db.all_rows(conn, "companies"), key=lambda c: c["name"].lower()):
        base = [c["name"], c["website"], _yes(c["is_big_tech"]),
                REMOTE_LABELS.get(c["remote"], c["remote"]), _yes(c["urgent"])]
        mine = sorted(apps.get(c["id"], []), key=lambda a: (a["date_applied"], a["id"]))
        if not mine:
            rows.append([*base, "", c["refs"], "", "", "", "", "", "", "", "", "", c["notes"]])
        for a in mine:
            days = weeks = ""
            if a["date_applied"]:
                n = (today - date.fromisoformat(a["date_applied"])).days
                days, weeks = n, n // 7
            rows.append([
                *base, a["job_url"], c["refs"], a["position"], a["tools"], a["date_applied"], days, weeks,
                a["status"], a["offer"], a["joining_date"], a["responsibilities"], a["comments"],
            ])
    return _csv(APPLICATION_COLUMNS, rows)


def backup_json(conn: sqlite3.Connection) -> str:
    return json.dumps({"exported": db.now(), **db.all_data(conn)}, indent=2, ensure_ascii=False)


EXPORTS = {
    "companies.csv": ("text/csv", companies_csv),
    "applications.csv": ("text/csv", applications_csv),
    "backup.json": ("application/json", backup_json),
}


def render(conn: sqlite3.Connection, name: str) -> tuple[str, bytes]:
    """(content type, bytes) for an export. CSVs carry a BOM so Excel reads accents correctly."""
    ctype, fn = EXPORTS[name]
    text = fn(conn)
    return ctype, text.encode("utf-8-sig" if ctype == "text/csv" else "utf-8")
