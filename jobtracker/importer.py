"""Import a spreadsheet exported as CSV (e.g. Google Sheets: File > Download > CSV).

Columns are matched by name, loosely: "Names", "Company" and "Company name" all mean the company.
Columns it doesn't recognise are kept, appended to the application's comments as "Header: value".
Importing the same file twice doesn't duplicate anything.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db

# normalised header -> (record, field). A field of None means "known, but ignore it".
HEADERS: dict[str, tuple[str, str | None]] = {}


def _map(record: str, fld: str | None, *names: str) -> None:
    for name in names:
        HEADERS[name] = (record, fld)


_map("company", "name", "name", "names", "company", "companies", "company name", "organisation", "organization")
_map("company", "website", "website", "site", "company website", "homepage")
_map("company", "is_big_tech", "is big tech", "big tech", "bigtech", "faang")
_map("company", "remote", "remote", "is remote", "work mode", "location type", "remote hybrid onsite")
_map("company", "urgent", "priority", "urgent", "is urgent", "high priority")
_map("company", "refs", "references", "reference", "referral", "referrals", "referred by")
_map("company", "about", "what they do", "about", "description", "company description")
_map("app", "job_url", "job post", "job posting", "job link", "job url", "posting", "post", "job")
_map("app", "position", "position", "role", "title", "job title")
_map("app", "tools", "tools", "technologies", "technology", "tech", "tech stack", "stack", "skills")
_map("app", "date_applied", "date applied", "applied", "applied on", "application date", "date")
_map("app", "status", "status", "stage", "state")
_map("app", "offer", "offer", "salary", "compensation")
_map("app", "joining_date", "joining date", "join date", "start date")
_map("app", "responsibilities", "responsibilities", "duties")
_map("app", "comments", "comments", "comment", "notes", "note")
_map("app", "told", "what i told them", "told them", "what i told")
_map("app", None, "days since applied", "weeks since applied", "days", "weeks")

STATUS_WORDS = {
    "applied": "Applied", "submitted": "Applied", "sent": "Applied", "pending": "Applied",
    "screening": "Screening", "screen": "Screening", "phone screen": "Screening", "hr screen": "Screening",
    "recruiter call": "Screening", "in review": "Screening", "reviewing": "Screening",
    "interview": "Interview", "interviewing": "Interview", "interview scheduled": "Interview",
    "technical interview": "Interview", "onsite": "Interview", "assessment": "Interview",
    "offer": "Offer", "offered": "Offer", "accepted": "Offer",
    "rejected": "Rejected", "declined": "Rejected", "rejection": "Rejected", "not selected": "Rejected",
    "ghosted": "Ghosted", "no response": "Ghosted", "no reply": "Ghosted",
    "withdrawn": "Withdrawn", "withdrew": "Withdrawn", "cancelled": "Withdrawn", "canceled": "Withdrawn",
}
URGENT_WORDS = db.TRUE_WORDS | {"urgent", "high", "p1", "top", "asap", "✓", "✔", "★"}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
NUMERIC_DATE = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})")


@dataclass
class ImportResult:
    rows: int = 0
    companies_created: int = 0
    companies_updated: int = 0
    applications_created: int = 0
    duplicates_skipped: int = 0
    rows_skipped: int = 0
    date_order: str = "mdy"
    unknown_columns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def norm_header(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def parse_bool(text: str, words: set[str] = db.TRUE_WORDS) -> bool:
    return text.strip().lower() in words


def parse_remote(text: str) -> str:
    t = text.strip().lower()
    if not t:
        return ""
    if "hybrid" in t:
        return "hybrid"
    if "remote" in t or t in db.TRUE_WORDS:
        return "remote"
    if t in {"no", "n", "false", "0", "onsite", "on-site", "on site", "office", "in office", "in-office"}:
        return "onsite"
    return ""


def detect_date_order(values: list[str]) -> str:
    """Guess day/month order from the values themselves: a first number over 12 means D/M/Y."""
    dmy = mdy = 0
    for value in values:
        m = NUMERIC_DATE.fullmatch(value.strip())
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            dmy += a > 12
            mdy += b > 12
    return "dmy" if dmy and not mdy else "mdy"


def _year(text: str) -> int:
    y = int(text)
    return y + 2000 if y < 100 else y


def parse_date(text: str, order: str = "mdy") -> str | None:
    """Return YYYY-MM-DD, '' for a blank cell, or None when the text isn't a date we understand."""
    t = text.strip()
    if not t:
        return ""
    try:
        m = re.fullmatch(r"(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})", t)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        m = NUMERIC_DATE.fullmatch(t)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), _year(m.group(3))
            day, month = (a, b) if order == "dmy" else (b, a)
            return date(y, month, day).isoformat()
        words = re.findall(r"[a-z]+|\d+", t.lower())
        month = next((MONTHS[w[:3]] for w in words if w[:3] in MONTHS), None)
        nums = [w for w in words if w.isdigit()]
        if month and len(nums) >= 2:
            day, year = (nums[0], nums[1]) if len(nums[0]) <= 2 else (nums[1], nums[0])
            return date(_year(year), month, int(day)).isoformat()
        if re.fullmatch(r"\d{5}(\.\d+)?", t):  # a spreadsheet serial number
            return (date(1899, 12, 30) + timedelta(days=int(float(t)))).isoformat()
    except ValueError:
        return None
    return None


def parse_told(text: str) -> list[dict]:
    """"Resume: v3; Salary: will discuss" -> label/value rows (the export writes them this way)."""
    rows = []
    for part in re.split(r"[;\n]", text):
        label, sep, value = part.partition(":")
        rows.append({"label": label.strip(), "value": value.strip()} if sep else {"label": "", "value": part.strip()})
    return db.parse_pairs(rows)


def normalize_status(text: str, statuses: list[str]) -> str:
    t = text.strip()
    if not t:
        return ""
    for status in statuses:
        if status.lower() == t.lower():
            return status
    word = STATUS_WORDS.get(norm_header(t))
    if word and word in statuses:
        return word
    return t  # keep what the sheet said rather than lose it


def _find_header(rows: list[list[str]]) -> int:
    for i, row in enumerate(rows[:15]):
        if any(HEADERS.get(norm_header(c)) == ("company", "name") for c in row):
            return i
    raise db.ValidationError("Couldn't find a header row with a company column (e.g. 'Names' or 'Company')")


def import_csv(conn: sqlite3.Connection, text: str, *, statuses: list[str],
               date_order: str = "auto", dry_run: bool = False) -> ImportResult:
    """Import CSV text into the database. With dry_run nothing is saved, but the counts are real."""
    result = ImportResult()
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    head = _find_header(rows)
    headers = rows[head]
    columns: list[tuple[str, tuple[str, str | None] | None]] = []
    for h in headers:
        target = HEADERS.get(norm_header(h))
        columns.append((h.strip(), target))
        if target is None and h.strip():
            result.unknown_columns.append(h.strip())

    body = [r for r in rows[head + 1:] if any(c.strip() for c in r)]
    date_cols = [i for i, (_, t) in enumerate(columns) if t and t[1] in ("date_applied", "joining_date")]
    if date_order == "auto":
        date_order = detect_date_order([r[i] for r in body for i in date_cols if i < len(r)])
    result.date_order = date_order

    try:
        for line_no, row in enumerate(body, start=head + 2):
            result.rows += 1
            _import_row(conn, row, columns, line_no, statuses, date_order, result)
    except BaseException:
        conn.rollback()
        raise
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return result


def _import_row(conn, row, columns, line_no, statuses, date_order, result: ImportResult) -> None:
    company: dict = {}
    app: dict = {}
    extras: list[str] = []
    for i, (header, target) in enumerate(columns):
        value = row[i].strip() if i < len(row) else ""
        if not value:
            continue
        if target is None:
            extras.append(f"{header}: {value}")
            continue
        record, fld = target
        if fld is None:
            continue
        if fld in ("date_applied", "joining_date"):
            parsed = parse_date(value, date_order)
            if parsed is None:
                result.warnings.append(f"Row {line_no}: couldn't read the date '{value}', kept it in comments")
                extras.append(f"{header}: {value}")
                continue
            value = parsed
        elif fld == "is_big_tech":
            value = parse_bool(value)
        elif fld == "urgent":
            value = parse_bool(value, URGENT_WORDS)
        elif fld == "remote":
            parsed_remote = parse_remote(value)
            if not parsed_remote:
                extras.append(f"{header}: {value}")
            value = parsed_remote
        elif fld == "status":
            value = normalize_status(value, statuses)
            if value not in statuses:
                result.warnings.append(f"Row {line_no}: kept unknown status '{value}' as-is")
        elif fld == "told":
            value = parse_told(value)
        (company if record == "company" else app)[fld] = value

    name = company.get("name", "")
    if not name:
        result.rows_skipped += 1
        return

    has_app = any(app.get(k) for k in ("position", "job_url", "date_applied", "tools", "status",
                                       "offer", "joining_date", "responsibilities"))
    if extras:
        app["comments"] = "\n".join(filter(None, [app.get("comments", ""), *extras]))
    if not has_app and app.get("comments"):
        company["notes"] = "\n".join(filter(None, [company.get("notes", ""), app.pop("comments")]))

    existing = db.find_company(conn, name)
    if existing is None:
        company_id = db.insert(conn, "companies", company)["id"]
        result.companies_created += 1
    else:
        company_id = existing["id"]
        fill = {k: v for k, v in company.items() if v and not existing.get(k)}
        if fill:
            db.update(conn, "companies", company_id, fill)
            result.companies_updated += 1

    if not has_app:
        return
    if app.get("date_applied") and not app.get("status"):
        app["status"] = "Applied" if "Applied" in statuses else statuses[0]
    app["tools"] = db.join_tools(app.get("tools", ""))
    dup = conn.execute(
        "SELECT 1 FROM applications WHERE company_id = ? AND position = ? AND job_url = ? AND date_applied = ?",
        (company_id, app.get("position", ""), app.get("job_url", ""), app.get("date_applied", "")),
    ).fetchone()
    if dup:
        result.duplicates_skipped += 1
        return
    db.insert(conn, "applications", {**app, "company_id": company_id})
    result.applications_created += 1


# ---- portfolio projects ------------------------------------------------------------------------

PROJECT_HEADERS: dict[str, str | None] = {}
for _fld, _names in {
    "name": ("name", "project", "project name", "title"),
    "description": ("description", "summary", "about", "details", "what it does", "overview"),
    "tools": ("tools", "tech", "tech stack", "stack", "technologies", "technology", "skills", "built with"),
    "repo_url": ("repo", "repository", "github", "gitlab", "source", "code", "repo url", "source code"),
    "demo_url": ("demo", "live", "live demo", "url", "link", "website", "site", "demo url", "live url"),
    "role": ("role", "my role", "contribution", "responsibilities", "team"),
    "dates": ("dates", "date", "when", "period", "timeline", "year", "duration"),
    "results": ("results", "result", "highlights", "impact", "outcome", "outcomes", "metrics", "achievements"),
}.items():
    for _name in _names:
        PROJECT_HEADERS[_name] = _fld


@dataclass
class ProjectImportResult:
    rows: int = 0
    created: int = 0
    updated: int = 0
    rows_skipped: int = 0
    unknown_columns: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def import_projects(conn: sqlite3.Connection, text: str, *, dry_run: bool = False) -> ProjectImportResult:
    """Import portfolio projects from CSV. A project with the same name as one you have is updated
    with the file's non-empty values, so importing the same file twice changes nothing.
    Unrecognised columns are added to the description as "Header: value"."""
    result = ProjectImportResult()
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    head = next((i for i, r in enumerate(rows[:15]) if any(PROJECT_HEADERS.get(norm_header(c)) == "name" for c in r)), None)
    if head is None:
        raise db.ValidationError("Couldn't find a header row with a project name column (e.g. 'Name' or 'Project')")
    columns = [(h.strip(), PROJECT_HEADERS.get(norm_header(h))) for h in rows[head]]
    result.unknown_columns = [h for h, f in columns if f is None and h]
    try:
        for row in rows[head + 1:]:
            if not any(c.strip() for c in row):
                continue
            result.rows += 1
            project: dict = {}
            extras: list[str] = []
            for i, (header, fld) in enumerate(columns):
                value = row[i].strip() if i < len(row) else ""
                if not value:
                    continue
                if fld is None:
                    extras.append(f"{header}: {value}")
                elif fld in project:
                    project[fld] += "\n" + value
                else:
                    project[fld] = value
            if not project.get("name"):
                result.rows_skipped += 1
                continue
            if extras:
                project["description"] = "\n".join(filter(None, [project.get("description", ""), *extras]))
            existing = conn.execute("SELECT id FROM projects WHERE name = ? COLLATE NOCASE", (project["name"],)).fetchone()
            if existing:
                db.update(conn, "projects", existing["id"], project)
                result.updated += 1
            else:
                db.insert(conn, "projects", project)
                result.created += 1
    except BaseException:
        conn.rollback()
        raise
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return result
