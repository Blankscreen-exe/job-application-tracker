# Job Tracker

A local-only app for keeping track of the companies you're interested in and the jobs you've applied to.
It runs in your browser, keeps everything in one file on your computer, and needs nothing but Python 3.10+.

- **Quick entry.** Paste a job link and the company is filled in for you (works with Greenhouse, Lever,
  Ashby, Workable, Workday, Recruitee and company career sites). Save with `Ctrl+Enter`, or
  "Save & add another" for a batch.
- **A tidy company panel.** Click a row to open it. Details, applications and people read as plain text;
  Edit on any part opens a form right there (`Ctrl+Enter` saves, `Esc` cancels).
- **A log per company.** Calls, emails, interviews and notes, with an optional date and time. Future
  entries appear under *Coming up* at the top of the page. Status changes are logged automatically.
- **People.** Recruiters, interviewers and referrers as cards, with what to remember about each.
  "+ Add person" opens a small form; a name is required.
- **Interview brief.** One click (or `b`) makes a PNG summary of the company: what they do, the role,
  tools, the people you'll meet, your notes and recent history. Download it or copy it to the clipboard.
- **Dashboard.** The page the tracker opens on: headline numbers, reminders for everything scheduled in the next 7 days (interviews
  highlighted), applications per week, the status breakdown, how often you hear back by month, and
  the tools that come up most.
- **Calendar.** A month view of interviews, onboarding, calls and other dated entries, plus joining dates.
  Click a day to add an event (company, which application, what, date and time), or an event to edit it.
- **Follow-ups.** Applications with no activity for 14 days (configurable) are flagged.
- **Filters and search.** Urgent, remote, big tech, active, needs a follow-up, not applied yet, by status,
  plus search across everything.
- **Import and export.** Import a CSV from your spreadsheet; export companies or applications as CSV
  (opens in Excel and Google Sheets), or everything as JSON.

## Run it

```sh
python jobs.py            # opens the tracker in your browser
```

Or put `bin/` on your PATH and run `jobs` from anywhere (`bin/jobs.cmd` on Windows, `bin/jobs` on
Linux/macOS).

```text
jobs                          open the tracker (same as `jobs serve`)
jobs serve --port 9000        use another port; --no-open to skip opening the browser
jobs import sheet.csv         import a spreadsheet (add --dry-run to preview)
jobs export companies         write companies as CSV (also: applications, backup)
jobs where                    show where the data lives
```

## Importing from Google Sheets or Excel

In Google Sheets choose **File > Download > Comma-separated values (.csv)**; in Excel, **Save As > CSV UTF-8**.
Then either run `jobs import file.csv` or use **⋯ > Import from CSV…** in the app, which shows a preview
before saving anything.

Columns are matched by name and loosely, so `Names`, `Company` and `Company name` all work. Recognised:

| Column | Also accepted |
|---|---|
| Names | Company, Name |
| Website | |
| Is Big Tech? | Big tech |
| Remote? | Remote, Work mode (Yes/No/Hybrid/Remote/On-site) |
| Priority | Urgent (TRUE/Yes/High/P1 count as urgent) |
| Job post | Job link, Job URL |
| References | Referral |
| Position | Role, Title |
| Tools | Technologies, Tech stack, Skills |
| Date Applied | Applied, Application date |
| Status | Stage |
| Offer | Salary |
| Joining Date | Start date |
| Responsibilities | |
| Comments | Notes |

`Days Since Applied` and `Weeks Since Applied` are ignored; the app works them out. Any other column is
kept, added to the application's comments as `Header: value`. Dates like `03/04/2025` are read as
month/day unless the file shows otherwise (use `--date-order dmy` to force it). Importing the same file
again doesn't create duplicates.

## Your data

Everything lives in the `data/` folder inside the project, which git ignores:

- `jobs.sqlite3`: the database
- `config.json`: your settings
- `backups/`: a copy of the database saved once a day (the last 14 are kept)

To back up or move your data, copy that folder. Set `JOBTRACKER_HOME` to keep the data somewhere else,
for example a synced folder.

## Settings

Edit `data/config.json` (created on first run):

```json
{
  "port": 8765,
  "statuses": ["Applied", "Screening", "Interview", "Offer", "Rejected", "Ghosted", "Withdrawn"],
  "active_statuses": ["Applied", "Screening", "Interview", "Offer"],
  "stale_statuses": ["Applied", "Screening", "Interview"],
  "stale_days": 14,
  "upcoming_days": 14,
  "reminder_days": 7,
  "backups_to_keep": 14
}
```

## Keyboard

`n` new (an application, a company on the Companies tab, an event on the Calendar) · `/` search · `j`/`k` move · `Enter` open · `b` brief ·
`1`–`4` Dashboard / Applications / Companies / Calendar · `←`/`→` change month on the Calendar · `Esc` close ·
`Ctrl+Enter` save a form or add a log entry · `?` all shortcuts

## Privacy

The server only listens on `127.0.0.1`. Each run creates a random token that the page must send with
every request, and requests with a foreign `Host` header are refused, so other websites open in your
browser can't read or change your data. Nothing is sent anywhere.

## Development

```sh
python -m unittest discover -s tests
```
