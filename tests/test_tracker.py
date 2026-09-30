import csv
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from jobtracker import config, db, exporter, importer, server

STATUSES = config.DEFAULTS["statuses"]

SHEET = """\
Names,Website,Is Big Tech?,Remote?,Priority,Job post,References,Position,Technologies,Date Applied,Days Since Applied,Weeks Since Applied,Status,Offer,Joining Date,Responsibilities,Comments,Recruiter
Acme,https://acme.com,FALSE,Yes,TRUE,https://jobs.lever.co/acme/1,Jane,Backend Engineer,"Python, AWS, python",15/01/2025,100,14,interviewing,,,Build APIs,Nice team,Bob
Acme,,,,,https://jobs.lever.co/acme/2,,Data Engineer,SQL,20/01/2025,,,rejected,,,,,
Globex,globex.com,TRUE,Hybrid,,,,,,,,,,,,,Apply after they reopen,
,,,,,,,,,,,,,,,,,
Initech,,No,No,,,,QA,,2025-13-40,,,Something odd,,,,,
"""


def memory_db() -> sqlite3.Connection:
    return db.connect(":memory:", init=True)


class ParsingTests(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(importer.parse_date("2025-01-15"), "2025-01-15")
        self.assertEqual(importer.parse_date("1/15/2025", "mdy"), "2025-01-15")
        self.assertEqual(importer.parse_date("15/1/2025", "dmy"), "2025-01-15")
        self.assertEqual(importer.parse_date("Jan 15, 2025"), "2025-01-15")
        self.assertEqual(importer.parse_date("15-Jan-25"), "2025-01-15")
        self.assertEqual(importer.parse_date("45672"), "2025-01-15")
        self.assertEqual(importer.parse_date(""), "")
        self.assertIsNone(importer.parse_date("soon"))
        self.assertIsNone(importer.parse_date("31/13/2025", "dmy"))

    def test_date_order_detection(self):
        self.assertEqual(importer.detect_date_order(["15/01/2025", "02/03/2025"]), "dmy")
        self.assertEqual(importer.detect_date_order(["01/15/2025"]), "mdy")
        self.assertEqual(importer.detect_date_order(["01/02/2025"]), "mdy")

    def test_values(self):
        self.assertEqual(importer.parse_remote("Remote (EU only)"), "remote")
        self.assertEqual(importer.parse_remote("No"), "onsite")
        self.assertEqual(importer.parse_remote("hybrid"), "hybrid")
        self.assertEqual(importer.normalize_status("interviewing", STATUSES), "Interview")
        self.assertEqual(importer.normalize_status("APPLIED", STATUSES), "Applied")
        self.assertEqual(importer.normalize_status("Odd", STATUSES), "Odd")
        self.assertTrue(importer.parse_bool("TRUE", importer.URGENT_WORDS))
        self.assertEqual(db.join_tools("Python, AWS, python,, SQL"), "Python, AWS, SQL")


class ImportTests(unittest.TestCase):
    def test_import_sheet(self):
        conn = memory_db()
        r = importer.import_csv(conn, SHEET, statuses=STATUSES)
        self.assertEqual(r.date_order, "dmy")
        self.assertEqual((r.companies_created, r.applications_created, r.rows_skipped), (3, 3, 0))
        self.assertEqual(r.unknown_columns, ["Recruiter"])
        acme = db.find_company(conn, "acme")
        self.assertTrue(acme["urgent"])
        self.assertEqual(acme["remote"], "remote")
        self.assertEqual(acme["refs"], "Jane")
        apps = conn.execute("SELECT * FROM applications WHERE company_id = ? ORDER BY id", (acme["id"],)).fetchall()
        self.assertEqual(apps[0]["status"], "Interview")
        self.assertEqual(apps[0]["tools"], "Python, AWS")
        self.assertEqual(apps[0]["date_applied"], "2025-01-15")
        self.assertIn("Recruiter: Bob", apps[0]["comments"])
        self.assertEqual(apps[1]["status"], "Rejected")
        globex = db.find_company(conn, "Globex")
        self.assertTrue(globex["is_big_tech"])
        self.assertEqual(globex["notes"], "Apply after they reopen")
        initech = conn.execute("SELECT a.* FROM applications a JOIN companies c ON c.id = a.company_id WHERE c.name = 'Initech'").fetchone()
        self.assertEqual(initech["date_applied"], "")
        self.assertIn("2025-13-40", initech["comments"])
        self.assertTrue(any("Something odd" in w for w in r.warnings))

    def test_reimport_is_idempotent_and_dry_run_saves_nothing(self):
        conn = memory_db()
        dry = importer.import_csv(conn, SHEET, statuses=STATUSES, dry_run=True)
        self.assertEqual(dry.applications_created, 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 0)
        importer.import_csv(conn, SHEET, statuses=STATUSES)
        again = importer.import_csv(conn, SHEET, statuses=STATUSES)
        self.assertEqual((again.companies_created, again.applications_created, again.duplicates_skipped), (0, 0, 3))

    def test_missing_header(self):
        with self.assertRaises(db.ValidationError):
            importer.import_csv(memory_db(), "a,b\n1,2\n", statuses=STATUSES)


class DbTests(unittest.TestCase):
    def test_status_change_is_logged(self):
        conn = memory_db()
        with conn:
            out = db.quick_add(conn, {"name": "Acme"}, {"position": "Dev", "status": "Applied"})
            db.update(conn, "applications", out["application_id"], {"status": "Interview"})
        events = db.all_rows(conn, "events")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "status")
        self.assertIn("Applied → Interview", events[0]["body"])

    def test_quick_add_reuses_company_and_company_only(self):
        conn = memory_db()
        first = db.quick_add(conn, {"name": "Acme"}, {"position": "Dev"})
        second = db.quick_add(conn, {"name": "ACME "}, {"job_url": "https://x"})
        self.assertEqual(first["company_id"], second["company_id"])
        self.assertFalse(second["created_company"])
        only = db.quick_add(conn, {"name": "Later Inc"}, {"position": "", "job_url": ""})
        self.assertIsNone(only["application_id"])

    def test_validation(self):
        conn = memory_db()
        cid = db.insert(conn, "companies", {"name": "A"})["id"]
        other = db.insert(conn, "companies", {"name": "B"})["id"]
        app = db.insert(conn, "applications", {"company_id": other})
        for bad in ({"name": ""}, {"remote": "moon"}):
            with self.assertRaises(db.ValidationError):
                db.update(conn, "companies", cid, bad)
        with self.assertRaises(db.ValidationError):
            db.insert(conn, "companies", {"name": "a"})
        with self.assertRaises(db.ValidationError):
            db.insert(conn, "events", {"company_id": cid, "event_date": "2025-02-30"})
        with self.assertRaises(db.ValidationError):
            db.insert(conn, "events", {"company_id": cid, "application_id": app["id"]})
        ev = db.insert(conn, "events", {"company_id": cid, "event_time": "9:05", "kind": "Interview"})
        self.assertEqual((ev["event_time"], ev["kind"]), ("09:05", "interview"))
        ev = db.insert(conn, "events", {"company_id": cid, "kind": "Onboarding", "event_date": "2026-04-22", "event_time": "22:00"})
        self.assertEqual((ev["kind"], ev["event_date"], ev["event_time"]), ("onboarding", "2026-04-22", "22:00"))
        with self.assertRaises(db.ValidationError):
            db.insert(conn, "events", {"company_id": cid, "kind": "party"})
        for bad in ({"company_id": cid}, {"company_id": cid, "name": "  "}):
            with self.assertRaises(db.ValidationError):
                db.insert(conn, "contacts", bad)
        person = db.insert(conn, "contacts", {"company_id": cid, "name": "Sam", "role": "Recruiter"})
        with self.assertRaises(db.ValidationError):
            db.update(conn, "contacts", person["id"], {"name": ""})
        self.assertEqual(db.update(conn, "contacts", person["id"], {"notes": "Friendly"})["name"], "Sam")

    def test_exports(self):
        conn = memory_db()
        importer.import_csv(conn, SHEET, statuses=STATUSES)
        apps = list(csv.reader(io.StringIO(exporter.applications_csv(conn, today=date(2025, 1, 25)))))
        self.assertEqual(apps[0][:3], ["Names", "Website", "Is Big Tech?"])
        self.assertEqual(len(apps), 5)  # header + 3 applications + Globex without one
        self.assertEqual(apps[1][9:13], ["2025-01-15", "10", "1", "Interview"])
        companies = list(csv.reader(io.StringIO(exporter.companies_csv(conn))))
        self.assertEqual(len(companies), 4)
        ctype, body = exporter.render(conn, "companies.csv")
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))


PROJECTS = """\
Project,Summary,Built with,GitHub,Live,My role,When,Impact,Stars
Tracker,Job tracker app,"Python, SQLite",github.com/me/tracker,,Solo,2026,Used daily,12
Blog,Static blog,Hugo,,https://me.dev,,,,
,no name here,,,,,,,
"""


class ProfileAndProjectTests(unittest.TestCase):
    def test_old_database_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            old = sqlite3.connect(path)
            old.executescript(db.SCHEMA.split("CREATE TABLE IF NOT EXISTS profile")[0]
                              .replace("    told             TEXT NOT NULL DEFAULT '[]',\n", "")
                              .replace("    remind         INTEGER NOT NULL DEFAULT 0,\n", ""))
            old.execute("INSERT INTO companies (name, created_at, updated_at) VALUES ('A', 'x', 'x')")
            old.execute("INSERT INTO events (company_id, kind, event_date, created_at, updated_at) VALUES (1, 'interview', '2030-01-01', 'x', 'x')")
            old.commit()
            old.close()
            conn = db.connect(path, init=True)
            self.assertFalse(db.all_rows(conn, "events")[0]["remind"])  # existing events start without a reminder
            self.assertEqual(len(db.all_rows(conn, "profile")), 1)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            db.connect(path, init=True).close()  # running it again is harmless
            conn.close()

    def test_reminder_defaults(self):
        conn = memory_db()
        cid = db.insert(conn, "companies", {"name": "A"})["id"]
        self.assertTrue(db.insert(conn, "events", {"company_id": cid, "kind": "interview"})["remind"])
        self.assertFalse(db.insert(conn, "events", {"company_id": cid, "kind": "onboarding"})["remind"])
        self.assertFalse(db.insert(conn, "events", {"company_id": cid, "kind": "interview", "remind": False})["remind"])
        self.assertTrue(db.insert(conn, "events", {"company_id": cid, "kind": "call", "remind": True})["remind"])

    def test_told_rows(self):
        conn = memory_db()
        cid = db.insert(conn, "companies", {"name": "A"})["id"]
        rows = [{"label": " Salary ", "value": "will discuss"}, {"label": "", "value": ""}, ["Resume", "v3"]]
        app = db.insert(conn, "applications", {"company_id": cid, "position": "Dev", "told": rows})
        self.assertEqual(app["told"], [{"label": "Salary", "value": "will discuss"}, {"label": "Resume", "value": "v3"}])
        with self.assertRaises(db.ValidationError):
            db.update(conn, "applications", app["id"], {"told": "not json"})
        cell = list(csv.reader(io.StringIO(exporter.applications_csv(conn))))[1][-1]
        self.assertEqual(cell, "Salary: will discuss; Resume: v3")
        self.assertEqual(importer.parse_told(cell), app["told"])

    def test_profile_is_single(self):
        conn = memory_db()
        self.assertEqual(db.update(conn, "profile", 1, {"name": "Me", "skills": "Go, go, SQL"})["skills"], "Go, SQL")
        with self.assertRaises(db.ValidationError):
            db.insert(conn, "profile", {"name": "Two"})
        with self.assertRaises(db.ValidationError):
            db.delete(conn, "profile", 1)

    def test_import_projects(self):
        conn = memory_db()
        dry = importer.import_projects(conn, PROJECTS, dry_run=True)
        self.assertEqual((dry.rows, dry.created, dry.rows_skipped), (3, 2, 1))
        self.assertEqual(db.all_rows(conn, "projects"), [])
        importer.import_projects(conn, PROJECTS)
        again = importer.import_projects(conn, PROJECTS)
        self.assertEqual((again.created, again.updated), (0, 2))
        tracker = db.all_rows(conn, "projects")[0]
        self.assertEqual((tracker["tools"], tracker["repo_url"], tracker["role"], tracker["results"]),
                         ("Python, SQLite", "github.com/me/tracker", "Solo", "Used daily"))
        self.assertIn("Stars: 12", tracker["description"])
        with self.assertRaises(db.ValidationError):
            db.insert(conn, "projects", {"description": "no name"})
        exported = list(csv.reader(io.StringIO(exporter.projects_csv(conn))))
        self.assertEqual(exported[0], exporter.PROJECT_COLUMNS)
        self.assertEqual(len(exported), 3)
        with self.assertRaises(db.ValidationError):
            importer.import_projects(conn, "Foo,Bar\n1,2\n")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name) / "jobs.sqlite3"
        db.connect(path, init=True).close()
        cls.srv = server.TrackerServer(("127.0.0.1", 0), path, dict(config.DEFAULTS))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = cls.srv.url.rstrip("/")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, token=True, raw=False):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        if token:
            req.add_header("X-Token", self.srv.token)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req) as res:
            data = res.read()
            return data if raw else json.loads(data)

    def test_flow(self):
        page = urllib.request.urlopen(self.base + "/").read().decode()
        self.assertIn(self.srv.token, page)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("GET", "/api/data", token=False)
        self.assertEqual(ctx.exception.code, 403)
        out = self.call("POST", "/api/quick-add", {"company": {"name": "Acme"}, "application": {"position": "Dev", "status": "Applied"}})
        self.call("PATCH", f"/api/applications/{out['application_id']}", {"status": "Interview"})
        self.call("POST", "/api/events", {"company_id": out["company_id"], "kind": "interview", "body": "Call", "event_date": "2030-01-01", "event_time": "14:00"})
        data = self.call("GET", "/api/data")
        self.assertEqual(len(data["events"]), 2)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("PATCH", f"/api/companies/{out['company_id']}", {"remote": "moon"})
        self.assertEqual(ctx.exception.code, 400)
        req = urllib.request.Request(self.base + "/api/import-projects?dry=0", method="POST", data=PROJECTS.encode(),
                                     headers={"X-Token": self.srv.token})
        with urllib.request.urlopen(req) as res:
            self.assertEqual(json.loads(res.read())["created"], 2)
        self.assertEqual(len(self.call("GET", "/api/data")["projects"]), 2)
        self.call("PATCH", "/api/profile/1", {"name": "Me"})
        self.assertEqual(self.call("GET", "/api/data")["profile"][0]["name"], "Me")
        csv_bytes = self.call("GET", f"/export/companies.csv?t={self.srv.token}", token=False, raw=True)
        self.assertIn(b"Acme", csv_bytes)
        self.call("DELETE", f"/api/companies/{out['company_id']}")
        data = self.call("GET", "/api/data")
        self.assertEqual((data["companies"], data["applications"], data["events"]), ([], [], []))

    def test_bad_host_rejected(self):
        req = urllib.request.Request(self.base + "/", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
