# Job Watcher 👀

A free, self-hosted Visualping for careers pages. Every ~15 minutes it checks 19 companies and emails you a digest the moment a new posting appears.

## How it works (the 30-second version)

```
config.yaml ──► fetch each company's job list ──► compare IDs with state.json ──► email anything new
```

The key idea: **track job IDs, not page pixels.** Visualping diffs a screenshot or page text, so a moved banner triggers a false alarm. This script pulls each job's unique ID, so "new" means *a new posting*, nothing else.

It reads jobs three ways, from most to least reliable:

| Method | Companies | Why |
|---|---|---|
| Official ATS JSON APIs | Airbnb, Anthropic, Duolingo, Waymo, Neuralink, Synchron (Greenhouse) · Spotify (Lever) · OpenAI, Merge Labs (Ashby) · Neurable (SmartRecruiters) | Public endpoints built for embedding careers pages — clean, stable |
| Company endpoints | Amazon (`search.json`), Apple (data embedded in page) | Big companies with their own systems |
| Link scraping ("Visualping mode") | Google, Paradromics (plain HTTP) · Meta, Meta Reality Labs, Microsoft, Uber, Precision (headless Chrome) | No public API; these pages build the list with JavaScript, so a real browser renders them and the script collects links matching a pattern |

Safety features:
- **First check seeds silently** — you won't get 600 "new" Anthropic jobs on day one.
- **One broken site never stops the others.**
- **Breakage alert** — if a site fails 4 checks in a row (usually a redesign), you get a "⚠️ source stopped working" email instead of silent failure.
- **Filters** — big companies post hundreds of roles/day, so they use the `early_career_tech` filter (SWE/ML/AI/data/research/intern/new-grad, excluding senior/staff/manager…). Neurotech companies get almost everything. Edit `config.yaml` to taste; remove a company's `filter:` line to get every posting.

### Location filter
`settings.locations` in `config.yaml` limits alerts to **US, Hong Kong, Germany, UK and New Zealand** (plus fully-remote roles). Each country has a list of names, codes and big tech cities, because sites write the same place many ways ("Seattle, WA" / "Fremont, California, United States" / "London" / "HKG"). Jobs with **no location listed still alert**, so you never miss one just because a site left it blank. To add a place like Singapore, just append `singapore` to the list. Amazon is also asked directly for jobs in only those countries.

## Setup

### 1. Gmail app password (2 min)
Gmail won't accept your normal password from scripts.
1. Turn on 2-Step Verification: myaccount.google.com → Security.
2. Go to **App passwords**, create one called "Job Watcher", copy the 16 characters.

### 2. Try it locally first (Windows, PowerShell)
```powershell
cd job-watcher
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium

python monitor.py --check          # health check: shows job counts + samples per company
```
Every company should show ✓. If one shows ✗, see *When a site breaks* below.

Then test email:
```powershell
$env:SMTP_USER="you@gmail.com"; $env:SMTP_PASS="abcdefghijklmnop"; $env:EMAIL_TO="you@gmail.com"
python monitor.py --test-email
```

### 3. Run it on a schedule — pick one

**Option A: GitHub Actions (recommended — runs 24/7, even when your laptop sleeps)**
1. Create a new repo and push this folder yourself (the workflow never commits anything; job history lives in the Actions cache).
2. Repo → Settings → Secrets and variables → Actions → add `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO`.
3. Actions tab → *Job Watcher* → **Run workflow** once to seed it.

Things to know:
- **Make the repo public.** Standard runners are free & unlimited for public repos. Private repos only get 2,000 min/month, and checking every 15 min needs ~4,000+. (Your secrets stay hidden either way; only the code is public.) If you want private, change the cron to hourly: `"17 * * * *"`.
- GitHub may delay scheduled runs at busy times, so "every 15 min" is really "every 15–30 min".
- In public repos GitHub **auto-disables schedules after 60 days with no repo activity** — push any small commit every ~2 months, or click "Enable workflow" if it stops.
- Some sites (most likely Meta/Microsoft) may block GitHub's datacenter IPs. If they keep failing there but work locally, use Option B for those.

**Option B: Windows Task Scheduler (runs only while your PC is on)**
1. Create `secrets.bat` next to `run_windows.bat` (it's git-ignored):
   ```bat
   set SMTP_USER=you@gmail.com
   set SMTP_PASS=abcdefghijklmnop
   set EMAIL_TO=you@gmail.com
   ```
2. Task Scheduler → Create Task → Trigger: daily, *repeat every 15 minutes, indefinitely* → Action: start `run_windows.bat`.
3. Output goes to `state\log.txt`.

## Everyday commands
```
python monitor.py --check                    # health check, nothing saved/sent
python monitor.py --dry-run                  # full run, print instead of email
python monitor.py --only Neuralink Synchron  # just some companies
python monitor.py --locations                # survey: every company's open jobs by country
python monitor.py --inspect URL [--browser]  # debug a careers page: link shapes + JSON calls
```

## Adding a company
1. Open their careers page and click a job. Look at the URL:
   - `boards.greenhouse.io/X` or `job-boards.greenhouse.io/X` → `source: greenhouse, slug: X`
   - `jobs.lever.co/X` → `source: lever, slug: X`
   - `jobs.ashbyhq.com/X` → `source: ashby, slug: X`
   - `jobs.smartrecruiters.com/X` → `source: smartrecruiters, slug: X`
2. Anything else → `source: links`: paste the job-list URL (with your filters applied in the browser) and write a regex `pattern` with a `(?P<id>...)` group that matches job links. Try `mode: http` first; use `mode: browser` if `--check` finds 0 links.

## When a site breaks
Big-company careers sites get redesigned. If `--check` shows ✗ or 0 jobs:
1. Run `python monitor.py --inspect <careers URL> --browser`. It lists the link shapes on the page that contain IDs; the job links are usually the top one. Turn that into `pattern`, with `(?P<id>...)` around the ID.
2. For browser-mode sites, open DevTools → Network → filter *Fetch/XHR* and look for the request returning jobs. Put a piece of its URL in `json_hint` so the script reads that data directly.
3. Run `python monitor.py --check --only <Company> -v` to see the full error.

## Files
```
monitor.py                      the whole program (~400 lines, commented)
config.yaml                     companies, filters, settings — edit this
tests/test_monitor.py           offline tests: python -m pytest -q
.github/workflows/job-watcher.yml   GitHub Actions schedule
run_windows.bat                 Task Scheduler entry point
state/state.json                created automatically: job IDs already seen
```
