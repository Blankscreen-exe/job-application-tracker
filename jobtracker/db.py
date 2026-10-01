"""SQLite storage: companies, their applications, the people you talk to and a log of events,
plus a little about you (your profile and portfolio projects) for writing messages, and the resume
versions you send (the files themselves live in a folder; see resumes.py).

Functions here never commit; callers wrap their work in `with conn:` so a request is all-or-nothing.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    website     TEXT NOT NULL DEFAULT '',
    is_big_tech INTEGER NOT NULL DEFAULT 0,
    remote      TEXT NOT NULL DEFAULT '',
    urgent      INTEGER NOT NULL DEFAULT 0,
    about       TEXT NOT NULL DEFAULT '',
    refs        TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
    id               INTEGER PRIMARY KEY,
    company_id       INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    position         TEXT NOT NULL DEFAULT '',
    job_url          TEXT NOT NULL DEFAULT '',
    tools            TEXT NOT NULL DEFAULT '',
    date_applied     TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT '',
    offer            TEXT NOT NULL DEFAULT '',
    joining_date     TEXT NOT NULL DEFAULT '',
    responsibilities TEXT NOT NULL DEFAULT '',
    comments         TEXT NOT NULL DEFAULT '',
    told             TEXT NOT NULL DEFAULT '[]',
    resume_id        INTEGER REFERENCES resumes(id) ON DELETE SET NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS applications_company ON applications(company_id);
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    company_id     INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    kind           TEXT NOT NULL DEFAULT 'note',
    body           TEXT NOT NULL DEFAULT '',
    event_date     TEXT NOT NULL DEFAULT '',
    event_time     TEXT NOT NULL DEFAULT '',
    remind         INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_company ON events(company_id);
CREATE TABLE IF NOT EXISTS contacts (
    id         INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name       TEXT NOT NULL DEFAULT '',
    role       TEXT NOT NULL DEFAULT '',
    link       TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS contacts_company ON contacts(company_id);
CREATE TABLE IF NOT EXISTS profile (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    name       TEXT NOT NULL DEFAULT '',
    headline   TEXT NOT NULL DEFAULT '',
    location   TEXT NOT NULL DEFAULT '',
    about      TEXT NOT NULL DEFAULT '',
    skills     TEXT NOT NULL DEFAULT '',
    links      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    tools       TEXT NOT NULL DEFAULT '',
    repo_url    TEXT NOT NULL DEFAULT '',
    demo_url    TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL DEFAULT '',
    dates       TEXT NOT NULL DEFAULT '',
    results     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resumes (
    id         INTEGER PRIMARY KEY,
    filename   TEXT NOT NULL UNIQUE COLLATE NOCASE,
    label      TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
SCHEMA_VERSION = 3
# Columns added after the first release: CREATE TABLE IF NOT EXISTS won't add them to an old database.
ADDED_COLUMNS = {
    "applications": {"told": "TEXT NOT NULL DEFAULT '[]'",
                     "resume_id": "INTEGER REFERENCES resumes(id) ON DELETE SET NULL"},
    "events": {"remind": "INTEGER NOT NULL DEFAULT 0"},
}

# Editable fields per table and how each is checked. Anything not listed (id, timestamps) is ignored.
FIELDS: dict[str, dict[str, str]] = {
    "companies": {
        "name": "name", "website": "text", "is_big_tech": "bool", "remote": "remote",
        "urgent": "bool", "about": "text", "refs": "text", "notes": "text",
    },
    "applications": {
        "company_id": "ref", "position": "text", "job_url": "text", "tools": "tools",
        "date_applied": "date", "status": "text", "offer": "text", "joining_date": "date",
        "responsibilities": "text", "comments": "text", "told": "pairs", "resume_id": "optref",
    },
    "events": {
        "company_id": "ref", "application_id": "optref", "kind": "kind", "body": "text",
        "event_date": "date", "event_time": "time", "remind": "bool",
    },
    "contacts": {
        "company_id": "ref", "name": "person", "role": "text", "link": "text", "notes": "text",
    },
    "profile": {
        "name": "text", "headline": "text", "location": "text", "about": "text", "skills": "tools", "links": "text",
    },
    "projects": {
        "name": "project", "description": "text", "tools": "tools", "repo_url": "text", "demo_url": "text",
        "role": "text", "dates": "text", "results": "text",
    },
    # The filename is set when a file is found or uploaded (resumes.py), never edited here.
    "resumes": {"label": "text", "notes": "text", "is_default": "bool"},
}
TABLES = tuple(FIELDS)
# Tables whose rows belong to a company.
COMPANY_TABLES = ("applications", "events", "contacts")
# Kinds of text that can't be left blank, and what to say when they are.
REQUIRED = {"name": "A company needs a name", "person": "A person needs a name", "project": "A project needs a name"}
# Event kinds that get a reminder unless you say otherwise.
REMIND_BY_DEFAULT = ("interview",)
REMOTE_VALUES = ("", "remote", "hybrid", "onsite")
EVENT_KINDS = ("note", "interview", "onboarding", "call", "email", "status", "other")
TRUE_WORDS = {"1", "true", "yes", "y", "x", "on"}

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
TIME_RE = re.compile(r"([01]?\d|2[0-3]):[0-5]\d")


class ValidationError(ValueError):
    """Bad input from the user; the message is shown to them as-is."""


class NotFound(LookupError):
    pass


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path: Path | str, *, init: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if init:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Bring an older database up to date. Safe to run every time."""
    with conn:
        for table, columns in ADDED_COLUMNS.items():
            have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, decl in columns.items():
                if column not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.execute("INSERT OR IGNORE INTO profile (id, created_at, updated_at) VALUES (1, ?, ?)", (now(), now()))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _coerce(kind: str, key: str, value):
    if kind in ("ref", "optref"):
        if value in (None, "") and kind == "optref":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{key} must be a number") from None
    if kind == "bool":
        if isinstance(value, str):
            return int(value.strip().lower() in TRUE_WORDS)
        return int(bool(value))
    if kind == "pairs":
        return json.dumps(parse_pairs(value), ensure_ascii=False)
    text = "" if value is None else str(value).strip()
    if kind in REQUIRED:
        if not text:
            raise ValidationError(REQUIRED[kind])
        return text
    if kind == "remote":
        text = text.lower()
        if text not in REMOTE_VALUES:
            raise ValidationError(f"remote must be one of: {', '.join(v or 'blank' for v in REMOTE_VALUES)}")
        return text
    if kind == "kind":
        text = text.lower() or "note"
        if text not in EVENT_KINDS:
            raise ValidationError(f"kind must be one of: {', '.join(EVENT_KINDS)}")
        return text
    if kind == "date":
        if text:
            try:
                if not DATE_RE.fullmatch(text):
                    raise ValueError
                date.fromisoformat(text)
            except ValueError:
                raise ValidationError(f"{key} must be a date like 2026-09-28") from None
        return text
    if kind == "time":
        if text and not TIME_RE.fullmatch(text):
            raise ValidationError(f"{key} must be a time like 14:30")
        return text.zfill(5) if text else ""
    if kind == "tools":
        return join_tools(text)
    return text


def join_tools(text: str) -> str:
    """Normalise a comma-separated tool list: trimmed, no blanks, no case-insensitive repeats."""
    seen: set[str] = set()
    out: list[str] = []
    for part in re.split(r"[,;\n]", text or ""):
        part = part.strip()
        if part and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return ", ".join(out)


def parse_pairs(value) -> list[dict]:
    """Label/value rows ("Salary": "will discuss in interview"), from a list or its JSON text.

    Rows with neither a label nor a value are dropped.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except json.JSONDecodeError:
            raise ValidationError("told must be a list of label/value rows") from None
    if not isinstance(value, list):
        raise ValidationError("told must be a list of label/value rows")
    out = []
    for item in value:
        if isinstance(item, dict):
            label, text = item.get("label"), item.get("value")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            label, text = item
        else:
            raise ValidationError("told must be a list of label/value rows")
        label, text = str(label or "").strip(), str(text or "").strip()
        if label or text:
            out.append({"label": label, "value": text})
    return out


def clean(table: str, data: dict) -> dict:
    spec = FIELDS[table]
    return {key: _coerce(spec[key], key, value) for key, value in data.items() if key in spec}


def _row(table: str, row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key, kind in FIELDS[table].items():
        if kind == "bool":
            out[key] = bool(out[key])
        elif kind == "pairs":
            out[key] = parse_pairs(out[key])
    return out


def _integrity(exc: sqlite3.IntegrityError) -> ValidationError:
    msg = str(exc)
    if "UNIQUE" in msg:
        return ValidationError("A company with that name already exists")
    if "FOREIGN KEY" in msg:
        return ValidationError("That company, application or resume no longer exists")
    return ValidationError(msg)


def _check_event_link(conn: sqlite3.Connection, values: dict, current: dict | None = None) -> None:
    app_id = values.get("application_id", current and current["application_id"])
    company_id = values.get("company_id", current and current["company_id"])
    if app_id is None:
        return
    row = conn.execute("SELECT company_id FROM applications WHERE id = ?", (app_id,)).fetchone()
    if row is None or row["company_id"] != company_id:
        raise ValidationError("That application belongs to a different company")


def get(conn: sqlite3.Connection, table: str, row_id: int) -> dict | None:
    return _row(table, conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone())


def insert(conn: sqlite3.Connection, table: str, data: dict) -> dict:
    if table == "profile":
        raise ValidationError("There is only one profile; update it instead")
    if table == "resumes":
        raise ValidationError("Upload a resume file instead")
    values = clean(table, data)
    for key, kind in FIELDS[table].items():
        if kind in REQUIRED and key not in values:
            raise ValidationError(REQUIRED[kind])
    if table in COMPANY_TABLES and "company_id" not in values:
        raise ValidationError("company_id is required")
    if table == "events":
        _check_event_link(conn, values)
        values.setdefault("remind", int(values.get("kind", "note") in REMIND_BY_DEFAULT))
    values["created_at"] = values["updated_at"] = now()
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    try:
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))
    except sqlite3.IntegrityError as exc:
        raise _integrity(exc) from None
    return get(conn, table, cur.lastrowid)  # type: ignore[return-value]


def update(conn: sqlite3.Connection, table: str, row_id: int, data: dict) -> dict:
    old = get(conn, table, row_id)
    if old is None:
        raise NotFound(f"{table} {row_id}")
    values = clean(table, data)
    values.pop("company_id", None)  # rows don't move between companies
    if not values:
        return old
    if table == "events":
        _check_event_link(conn, values, old)
    if table == "resumes" and values.get("is_default"):
        conn.execute("UPDATE resumes SET is_default = 0 WHERE id != ?", (row_id,))
    values["updated_at"] = now()
    sets = ", ".join(f"{key} = ?" for key in values)
    try:
        conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*values.values(), row_id])
    except sqlite3.IntegrityError as exc:
        raise _integrity(exc) from None
    if table == "applications" and "status" in values and values["status"] != old["status"]:
        label = old["position"] or "application"
        insert(conn, "events", {
            "company_id": old["company_id"], "application_id": row_id, "kind": "status",
            "body": f"{label}: {old['status'] or 'no status'} → {values['status'] or 'no status'}",
            "event_date": date.today().isoformat(),
        })
    return get(conn, table, row_id)  # type: ignore[return-value]


def delete(conn: sqlite3.Connection, table: str, row_id: int) -> None:
    if table == "profile":
        raise ValidationError("The profile can't be deleted")
    if conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,)).rowcount == 0:
        raise NotFound(f"{table} {row_id}")


def find_company(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM companies WHERE name = ? COLLATE NOCASE", (name.strip(),)).fetchone()
    return _row("companies", row)


def all_rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    return [_row(table, r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]  # type: ignore[misc]


def all_data(conn: sqlite3.Connection) -> dict:
    return {table: all_rows(conn, table) for table in TABLES}


def quick_add(conn: sqlite3.Connection, company: dict, application: dict | None) -> dict:
    """Add an application, creating its company unless one with that name exists already.

    With no position and no job link only the company is added, for places you mean to apply to.
    """
    name = str(company.get("name") or "").strip()
    if not name:
        raise ValidationError("A company needs a name")
    existing = find_company(conn, name)
    company_id = existing["id"] if existing else insert(conn, "companies", company)["id"]
    app = None
    if application and any(str(application.get(k) or "").strip() for k in ("position", "job_url")):
        app = insert(conn, "applications", {**application, "company_id": company_id})
    return {
        "company_id": company_id,
        "application_id": app["id"] if app else None,
        "created_company": existing is None,
    }


def backup(db_file: Path, dest_dir: Path, keep: int) -> Path | None:
    """Copy the database to dest_dir once a day, keeping the newest `keep` copies."""
    if not db_file.exists():
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"jobs-{date.today().isoformat()}.sqlite3"
    if not target.exists():
        src = sqlite3.connect(db_file)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    for old in sorted(dest_dir.glob("jobs-*.sqlite3"))[:-max(keep, 1)]:
        old.unlink(missing_ok=True)
    return target
