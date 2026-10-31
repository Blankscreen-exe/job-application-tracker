"""Resume tracks: one folder per kind of resume (python/, nodejs/), each holding your current resume under
one standard file name, so whatever you send is called the same thing:

    resumes/python/muhammad_hammad_hassan_resume.pdf
    resumes/python/muhammad_hammad_hassan_resume.docx
    resumes/nodejs/muhammad_hammad_hassan_resume.pdf

A track holds one file per format. Uploading to a track replaces the file of that format; nothing is kept.
The folder is the source of truth: a new folder shows up as a track. The tracker keeps each track's label,
notes and whether it is the default. Files in a track folder under any other name are ignored.
"""

from __future__ import annotations

import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import db

# Formats a resume can be, in the order they are preferred for View and Copy file.
EXTENSIONS = (".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md")
# Folders that are never tracks. removed/ held deleted files in the first layout.
NOT_TRACKS = {"removed"}
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# --- the standard file name -------------------------------------------------------------------

def standard_name(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT resume_filename FROM profile WHERE id = 1").fetchone()[0]


def clean_name(name: str) -> str:
    """The file name without an extension, safe on every OS: "My CV.pdf" -> "My CV"."""
    name = UNSAFE.sub("_", (name or "").strip())
    stem, ext = os.path.splitext(name)
    if ext.lower() in EXTENSIONS:
        name = stem
    elif name.lower() in EXTENSIONS:  # just ".pdf"
        name = ""
    name = name.strip(" .")
    if not name:
        raise db.ValidationError("The resume file name can't be blank")
    return name


def set_standard_name(conn: sqlite3.Connection, folder: Path, name: str) -> str:
    """Change the file name every resume is saved under, renaming the files already there."""
    new, old = clean_name(name), standard_name(conn)
    if old and new != old:
        for track in _track_dirs(folder):
            for ext in EXTENSIONS:
                src = track / f"{old}{ext}"
                if src.is_file():
                    src.replace(track / f"{new}{ext}")
    conn.execute("UPDATE profile SET resume_filename = ?, updated_at = ? WHERE id = 1", (new, db.now()))
    return new


# --- tracks -----------------------------------------------------------------------------------

def track_slug(name: str) -> str:
    """A folder name for a track: "Node.js" -> "nodejs", "Python / data" -> "python-data"."""
    slug = re.sub(r"\s+", "-", (name or "").strip().lower())
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9_-]", "", slug)).strip("-_")
    if not slug:
        raise db.ValidationError("A track needs a name with letters or numbers, like Python")
    if slug in NOT_TRACKS:
        raise db.ValidationError(f"{slug} can't be used as a track name")
    return slug


def _track_dirs(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_dir() and p.name.lower() not in NOT_TRACKS
                  and not p.name.startswith("."))


def _insert(conn: sqlite3.Connection, track: str, label: str = "") -> dict:
    stamp = db.now()
    first = conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0] == 0
    cur = conn.execute("INSERT INTO resumes (track, label, is_default, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                       (track, label, int(first), stamp, stamp))
    return db.get(conn, "resumes", cur.lastrowid)  # type: ignore[return-value]


def _find(conn: sqlite3.Connection, track: str) -> dict | None:
    return db._row("resumes", conn.execute("SELECT * FROM resumes WHERE track = ?", (track,)).fetchone())


def _get(conn: sqlite3.Connection, row_id: int) -> dict:
    row = db.get(conn, "resumes", row_id)
    if row is None:
        raise db.NotFound(f"resume {row_id}")
    return row


def sync(conn: sqlite3.Connection, folder: Path) -> None:
    """Add a track for every folder the tracker hasn't seen yet."""
    known = {r["track"].lower() for r in conn.execute("SELECT track FROM resumes")}
    for path in _track_dirs(folder):
        if path.name.lower() not in known:
            _insert(conn, path.name)


def _files(track_dir: Path, name: str) -> list[Path]:
    if not name or not track_dir.is_dir():
        return []
    return [p for ext in EXTENSIONS if (p := track_dir / f"{name}{ext}").is_file()]


def listing(conn: sqlite3.Connection, folder: Path) -> list[dict]:
    """Every track, whether its folder is still there, and its files (preferred format first)."""
    name, out = standard_name(conn), []
    for row in db.all_rows(conn, "resumes"):
        track_dir = folder / row["track"]
        files = []
        for path in _files(track_dir, name):
            stat = path.stat()
            files.append({"ext": path.suffix.lstrip("."), "name": path.name, "size": stat.st_size,
                          "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")})
        out.append({**row, "missing": not track_dir.is_dir(), "files": files})
    return out


def upload(conn: sqlite3.Connection, folder: Path, track: str, original_name: str, data: bytes) -> dict:
    """Save a file as the track's resume in that format, replacing the one there. Makes the track if it's new."""
    name = standard_name(conn)
    if not name:
        raise db.ValidationError("Set your resume file name first")
    ext = os.path.splitext(original_name or "")[1].lower()
    if ext not in EXTENSIONS:
        raise db.ValidationError(f"Resumes can be {', '.join(e.lstrip('.').upper() for e in EXTENSIONS)}")
    if not data:
        raise db.ValidationError("That file is empty")
    slug = track_slug(track)
    row = _find(conn, slug) or _insert(conn, slug, track.strip())
    track_dir = folder / row["track"]
    track_dir.mkdir(exist_ok=True)
    target = track_dir / f"{name}{ext}"
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    return row


def file_of(conn: sqlite3.Connection, folder: Path, row_id: int, ext: str = "") -> Path:
    """The track's file in that format, or its preferred one."""
    row = _get(conn, row_id)
    files = _files(folder / row["track"], standard_name(conn))
    if ext:
        files = [p for p in files if p.suffix.lower() == "." + ext.lower().lstrip(".")]
    if not files:
        raise db.ValidationError(f"{row['label'] or row['track']} has no {ext.upper() + ' ' if ext else ''}file yet")
    return files[0]


def delete_file(conn: sqlite3.Connection, folder: Path, row_id: int, ext: str) -> None:
    file_of(conn, folder, row_id, ext).unlink()


def remove(conn: sqlite3.Connection, folder: Path, row_id: int) -> None:
    """Delete a track: its resume files and its folder. Other files in the folder block this, so nothing
    you put there yourself is lost."""
    row = _get(conn, row_id)
    track_dir = folder / row["track"]
    if track_dir.is_dir():
        mine = set(_files(track_dir, standard_name(conn)))
        others = [p.name for p in track_dir.iterdir() if p not in mine]
        if others:
            raise db.ValidationError(f"{row['track']}/ also holds {', '.join(sorted(others)[:3])}; move those out first")
        for path in mine:
            path.unlink()
        track_dir.rmdir()
    db.delete(conn, "resumes", row_id)
    if row["is_default"]:
        conn.execute("UPDATE resumes SET is_default = 1 WHERE id = (SELECT MIN(id) FROM resumes)")


# --- the OS: open, reveal, copy ---------------------------------------------------------------

def content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def open_folder(folder: Path) -> None:
    _launch(folder)


def reveal(path: Path) -> None:
    """Open the file's folder with the file selected, where the OS can do that."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", f"/select,{path}"])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        _launch(path.parent)


# Windows: the file itself (paste into Explorer, an email, a chat or an upload box) and its full path as
# text (paste into a file picker's "File name" box). The path comes in through the environment, unquoted.
WINDOWS_COPY = """
Add-Type -AssemblyName System.Windows.Forms
$files = New-Object System.Collections.Specialized.StringCollection
[void]$files.Add($env:RESUME_PATH)
$data = New-Object System.Windows.Forms.DataObject
$data.SetFileDropList($files)
$data.SetText($env:RESUME_PATH)
[System.Windows.Forms.Clipboard]::SetDataObject($data, $true)
"""


def copy_to_clipboard(path: Path) -> None:
    """Put the file on the clipboard so it can be pasted wherever a file can."""
    path = path.resolve()
    if sys.platform == "win32":
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", WINDOWS_COPY]
        env = {**os.environ, "RESUME_PATH": str(path)}
        stdin = None
    elif sys.platform == "darwin":
        cmd = ["osascript", "-e", "on run argv", "-e", "set the clipboard to (POSIX file (item 1 of argv))",
               "-e", "end run", str(path)]
        env, stdin = None, None
    else:
        cmd = ["xclip", "-selection", "clipboard", "-t", "text/uri-list"]
        env, stdin = None, path.as_uri().encode()
    try:
        done = subprocess.run(cmd, input=stdin, env=env, capture_output=True, timeout=15)
    except FileNotFoundError:
        raise db.ValidationError(f"Copying files needs {cmd[0]}; use Show in folder instead") from None
    except subprocess.TimeoutExpired:
        raise db.ValidationError("Copying took too long; try again") from None
    if done.returncode != 0:
        raise db.ValidationError(f"Couldn't copy the file: {done.stderr.decode(errors='replace').strip()[:200]}")


def _launch(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - our own folder, opened in Explorer
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
