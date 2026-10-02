"""Resume versions: the files live in a folder (resumes/ in the project), the tracker keeps a label,
a note and which one is the default.

The folder is the source of truth. Drop a file in and it shows up; upload one from the app and it is
written there. Deleting from the app moves the file to removed/ inside the folder rather than erasing it.

Each version can have its own subfolder, so every file can carry the name you send it under:
python/Ali Khan - Resume.pdf and nodejs/Ali Khan - Resume.pdf. A resume's filename is its path inside the
folder, with "/" between the subfolder and the file.
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

EXTENSIONS = (".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md")
REMOVED = "removed"
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _is_resume(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in EXTENSIONS and not path.name.startswith(("~$", "."))


def _rel(sub: str, name: str) -> str:
    return f"{sub}/{name}" if sub else name


def files(folder: Path) -> list[str]:
    """Resume files in the folder and in its version subfolders (not removed/), as paths inside it."""
    out = []
    for path in folder.iterdir():
        if path.is_dir() and path.name.lower() != REMOVED and not path.name.startswith("."):
            out += [_rel(path.name, p.name) for p in path.iterdir() if _is_resume(p)]
        elif _is_resume(path):
            out.append(path.name)
    return sorted(out, key=str.lower)


def sync(conn: sqlite3.Connection, folder: Path) -> None:
    """Add a row for every file in the folder that the tracker hasn't seen yet."""
    known = {r["filename"].lower() for r in conn.execute("SELECT filename FROM resumes")}
    for rel in files(folder):
        if rel.lower() not in known:
            _insert(conn, rel)


def _insert(conn: sqlite3.Connection, filename: str) -> dict:
    stamp = db.now()
    first = conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0] == 0
    cur = conn.execute("INSERT INTO resumes (filename, is_default, created_at, updated_at) VALUES (?, ?, ?, ?)",
                       (filename, int(first), stamp, stamp))
    return db.get(conn, "resumes", cur.lastrowid)  # type: ignore[return-value]


def listing(conn: sqlite3.Connection, folder: Path) -> list[dict]:
    """Every resume row, with whether its file is still there, its size and when it last changed."""
    out = []
    for row in db.all_rows(conn, "resumes"):
        path = folder / row["filename"]
        stat = path.stat() if path.is_file() else None
        out.append({**row, "missing": stat is None, "size": stat.st_size if stat else 0,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds") if stat else ""})
    return out


def safe_name(name: str) -> str:
    """A filename that is fine on every OS, keeping the extension; refuses types that aren't resumes."""
    name = UNSAFE.sub("_", Path(name.replace("\\", "/")).name).strip(" .")
    stem, ext = os.path.splitext(name)
    if ext.lower() not in EXTENSIONS:
        raise db.ValidationError(f"Resumes can be {', '.join(e.lstrip('.').upper() for e in EXTENSIONS)}")
    return (stem.strip() or "resume") + ext.lower()


def safe_folder(name: str) -> str:
    """A version subfolder name ("python"); blank means the resumes folder itself."""
    name = UNSAFE.sub("_", name or "").strip(" .")
    if name.lower() == REMOVED:
        raise db.ValidationError(f"{REMOVED} is where deleted resumes go; pick another folder name")
    return name


def _taken(conn: sqlite3.Connection | None, folder: Path, rel: str) -> bool:
    """Whether a file has that name (ignoring case, as Windows and macOS do) or a resume row has that path."""
    parent, name = (folder / rel).parent, Path(rel).name.lower()
    if parent.is_dir() and any(p.name.lower() == name for p in parent.iterdir()):
        return True
    return conn is not None and conn.execute("SELECT 1 FROM resumes WHERE filename = ?", (rel,)).fetchone() is not None


def _free(conn: sqlite3.Connection | None, folder: Path, sub: str, name: str) -> str:
    """sub/name, or sub/'name (2).ext' and so on if that is taken."""
    stem, ext, n, cand = Path(name).stem, Path(name).suffix, 2, name
    while _taken(conn, folder, _rel(sub, cand)):
        cand, n = f"{stem} ({n}){ext}", n + 1
    return _rel(sub, cand)


def upload(conn: sqlite3.Connection, folder: Path, name: str, data: bytes, subfolder: str = "") -> dict:
    if not data:
        raise db.ValidationError("That file is empty")
    sub = safe_folder(subfolder)
    rel = _free(conn, folder, sub, safe_name(name))
    path = folder / rel
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    try:
        return _insert(conn, rel)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def move(conn: sqlite3.Connection, folder: Path, row_id: int, subfolder: str, name: str) -> dict:
    """Put a resume's file in another subfolder and/or rename it, keeping its extension. The row stays the
    same, so applications that were sent it still point to it."""
    old = path_of(conn, folder, row_id)
    current = db.get(conn, "resumes", row_id)["filename"]  # type: ignore[index]
    stem = UNSAFE.sub("_", name or "").strip(" .")
    if stem.lower().endswith(old.suffix.lower()):
        stem = stem[: -len(old.suffix)].rstrip(" .")
    if not stem:
        raise db.ValidationError("A resume needs a file name")
    rel = _rel(safe_folder(subfolder), stem + old.suffix.lower())
    if rel == current:
        return db.get(conn, "resumes", row_id)  # type: ignore[return-value]
    if rel.lower() != current.lower() and _taken(conn, folder, rel):
        raise db.ValidationError(f"There is already a {rel}")
    new = folder / rel
    new.parent.mkdir(exist_ok=True)
    old.rename(new)
    if old.parent != folder and not any(old.parent.iterdir()):
        old.parent.rmdir()  # the subfolder it left is empty now
    conn.execute("UPDATE resumes SET filename = ?, updated_at = ? WHERE id = ?", (rel, db.now(), row_id))
    return db.get(conn, "resumes", row_id)  # type: ignore[return-value]


def path_of(conn: sqlite3.Connection, folder: Path, row_id: int) -> Path:
    row = db.get(conn, "resumes", row_id)
    if row is None:
        raise db.NotFound(f"resume {row_id}")
    path = folder / row["filename"]
    if not path.is_file():
        raise db.ValidationError(f"{row['filename']} is no longer in the resumes folder")
    return path


def remove(conn: sqlite3.Connection, folder: Path, row_id: int) -> None:
    """Forget a resume and move its file to removed/, so it doesn't come back on the next sync."""
    row = db.get(conn, "resumes", row_id)
    if row is None:
        raise db.NotFound(f"resume {row_id}")
    path = folder / row["filename"]
    if path.is_file():
        (folder / REMOVED).mkdir(exist_ok=True)
        shutil.move(str(path), str(folder / _free(None, folder, REMOVED, path.name)))
    db.delete(conn, "resumes", row_id)
    if row["is_default"]:
        conn.execute("UPDATE resumes SET is_default = 1 WHERE id = (SELECT MIN(id) FROM resumes)")


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
