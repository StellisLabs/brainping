# Job Watcher: context for Claude Code

## What this is
A free, self-hosted Visualping for careers pages. Every ~15 min it fetches open jobs from
19 companies, diffs job IDs against `state/state.json`, and emails a digest of new postings
that pass title + location filters. Built in a claude.ai chat (Nov 2026), now maintained here.

## Working with Andrew (the owner)
- **Never run `git commit` or `git push`.** Andrew commits himself. Leave changes uncommitted
  and summarise what changed so he can review the diff.
- Learning is his top priority: give a short, plain explanation of *why* for each change.
- Friendly, informal tone; a bit of Cantonese is welcome (primarily English).
- He's on Windows (PowerShell, VS Code). Give PowerShell commands, not bash.

## Commands
```powershell
.venv\Scripts\activate
python -m pytest -q                         # offline tests (no network), run after every change
python monitor.py --check                   # live health check: ✓/✗ + sample jobs per company
python monitor.py --check --only Uber -v    # one company, full traceback
python monitor.py --dry-run                 # full run, prints digest, no email, no state saved
python monitor.py --locations               # survey: open jobs per country, filters ignored
python monitor.py --inspect URL --browser   # debug a careers page: link shapes + JSON calls
python monitor.py --test-email              # needs SMTP_USER / SMTP_PASS / EMAIL_TO env vars
```

## Architecture (all in monitor.py)
1. **Fetch**: `FETCHERS[source](company_cfg)` → `list[Job(id, title, url, location)]`.
   Sources, most → least reliable:
   - `greenhouse`, `lever`, `ashby`, `smartrecruiters`: official public ATS JSON APIs
   - `amazon` (`search.json`), `apple` (parses `window.__staticRouterHydrationData`)
   - `links`: generic scraper. Loads `url` (`mode: http` or `mode: browser` = Playwright
     Chromium), keeps `<a>` hrefs matching `pattern` (must have `(?P<id>…)`, optional
     `(?P<slug>…)`). The first text segment of the link is the title, the rest is the location.
     Optional `json_hint` + `url_template` read the JSON API calls the page makes.
2. **Diff**: `diff_company()`: the first successful fetch only seeds (no alerts). All IDs are
   recorded as seen *before* filtering, so changing filters never resurfaces old jobs.
3. **Filter**: `passes()`: title include/exclude (word-boundary regex) + `location_ok()`.
   `COUNTRY_ALIASES` maps countries to names, codes and cities. Short codes like `CA` only
   match as a whole comma-separated segment. `normalize_location()` strips screen-reader
   noise and `⋅`/`·` separators. Jobs with an empty location pass on purpose.
4. **Dedupe** across companies (a job keeps the later config entry, e.g. Meta Reality Labs).
5. **Email**: one digest per run via Gmail SMTP (465). A breakage alert fires once after
   `failure_alert_after` consecutive failures. A scraped page returning 0 links = failure.
6. **State** saved atomically (temp file + rename).

Deploy: `.github/workflows/job-watcher.yml` (cron every 15 min, state kept in the Actions
cache, never commits). Alternative: `run_windows.bat` + Task Scheduler (reads `secrets.bat`).

## Conventions
- Behaviour lives in `config.yaml` where possible (companies, filters, locations).
- One broken site must never stop the others (per-company try/except in `run()`).
- Every parser fix gets an offline test in `tests/test_monitor.py` using a markup sample
  shaped like the real site. Tests must not hit the network.

## History / known issues
- Synchron left Greenhouse for ADP → scraped from synchron.com/careers (`jobId=` links).
- Uber moved to jobs.uber.com (Oracle Recruiting) → `jobs.uber.com/en/jobs/<id>/`.
- Browser-mode sources (Meta, Meta Reality Labs, Microsoft, Uber, Precision) only see the
  first page of results. Meta/Microsoft may block GitHub Actions IPs; if so, run those
  locally via Task Scheduler.
- Precision titles can show as "Job <id>" in `--check`. The real title is fetched from the
  posting page only for *new* jobs (`fetch_detail_title`).

## Open decision
The `--locations` survey (Nov 2, 2026) found **0 jobs in Hong Kong and New Zealand** across all
19 companies. Biggest: US 1990, UK 159, Japan 66, Singapore 58, India 52, Germany 44,
Ireland 37, Australia 33, France 31, Canada 29, South Korea 25. Andrew is deciding which
countries to add to `settings.locations` (Hong Kong is his long-term home base).
