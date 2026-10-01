"""Command line: `jobs` opens the tracker; `jobs import`, `jobs export` and `jobs where` do the rest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, db, exporter, importer, paths


def _open_db() -> tuple[Path, "db.sqlite3.Connection"]:
    path = paths.db_path()
    return path, db.connect(path, init=True)


def cmd_serve(args, cfg) -> int:
    from . import server

    path, conn = _open_db()
    conn.close()
    db.backup(path, paths.backup_dir(), cfg["backups_to_keep"])
    server.serve(path, cfg, port=args.port, open_browser=not args.no_open)
    return 0


def cmd_import(args, cfg) -> int:
    try:
        text = Path(args.file).read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    path, conn = _open_db()
    if not args.dry_run:
        db.backup(path, paths.backup_dir(), cfg["backups_to_keep"])
    try:
        result = importer.import_csv(conn, text, statuses=cfg["statuses"],
                                     date_order=args.date_order, dry_run=args.dry_run)
    except db.ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    verb = "Would import" if args.dry_run else "Imported"
    print(f"{verb} {result.rows} rows (dates read as {result.date_order.upper()}):")
    print(f"  companies:    {result.companies_created} new, {result.companies_updated} filled in")
    print(f"  applications: {result.applications_created} new, {result.duplicates_skipped} already there")
    if result.rows_skipped:
        print(f"  skipped:      {result.rows_skipped} rows with no company name")
    if result.unknown_columns:
        print(f"  kept in comments: {', '.join(result.unknown_columns)}")
    for warning in result.warnings:
        print(f"  ! {warning}")
    return 0


def cmd_export(args, cfg) -> int:
    name = {"companies": "companies.csv", "applications": "applications.csv", "backup": "backup.json"}[args.what]
    _, conn = _open_db()
    try:
        _, body = exporter.render(conn, name)
    finally:
        conn.close()
    out = Path(args.output or f"job-tracker-{name}")
    out.write_bytes(body)
    print(f"Wrote {out.resolve()}")
    return 0


def cmd_where(args, cfg) -> int:
    print(f"data folder: {paths.data_dir()}")
    print(f"database:    {paths.db_path()}")
    print(f"config:      {paths.config_path()}")
    print(f"backups:     {paths.backup_dir()}")
    print(f"resumes:     {paths.resumes_dir()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobs", description="Keep track of companies and job applications.")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="open the tracker in your browser (the default)")
    s.add_argument("--port", type=int, help="port to try first (default from config: 8765)")
    s.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    s.set_defaults(fn=cmd_serve)

    i = sub.add_parser("import", help="import a CSV exported from your spreadsheet")
    i.add_argument("file")
    i.add_argument("--dry-run", action="store_true", help="show what would be imported, save nothing")
    i.add_argument("--date-order", choices=["auto", "mdy", "dmy"], default="auto",
                   help="how to read dates like 03/04/2025 (default: guess from the file)")
    i.set_defaults(fn=cmd_import)

    e = sub.add_parser("export", help="export companies or applications as CSV, or everything as JSON")
    e.add_argument("what", choices=["companies", "applications", "backup"])
    e.add_argument("-o", "--output", help="file to write (default: job-tracker-<what> in this folder)")
    e.set_defaults(fn=cmd_export)

    w = sub.add_parser("where", help="show where the data and config live")
    w.set_defaults(fn=cmd_where)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv.insert(0, "serve")  # `jobs --port 9000` means `jobs serve --port 9000`
    args = build_parser().parse_args(argv)
    cfg = config.load()
    return args.fn(args, cfg)
