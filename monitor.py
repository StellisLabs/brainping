#!/usr/bin/env python3
"""
Job Watcher — a free, self-hosted Visualping for careers pages.

Each run:
  1. fetches the current job list for every company in config.yaml
  2. compares job IDs against state.json (what we've already seen)
  3. emails you a digest of anything new that passes your filters

Usage:
  python monitor.py                 # normal run (sends email if new jobs)
  python monitor.py --dry-run       # fetch + print, no email, don't save state
  python monitor.py --only Neuralink --dry-run
  python monitor.py --check         # health check: job counts per company
  python monitor.py --test-email    # send yourself a test email
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin, quote, urlencode, urlparse, parse_qsl, urlunparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("JOBWATCH_CONFIG", ROOT / "config.yaml"))
STATE_PATH = Path(os.environ.get("JOBWATCH_STATE", ROOT / "state" / "state.json"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
TIMEOUT = 30


# ─────────────────────────── data model ────────────────────────────
@dataclass
class Job:
    id: str
    title: str
    url: str
    location: str = ""


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def title_from_slug(slug: str) -> str:
    return clean(slug.replace("-", " ").replace("_", " ")).title()


# ─────────────────────────── fetchers ──────────────────────────────
# Each fetcher returns list[Job]. Raise on failure; the caller handles it.

def get_json(url, **kw):
    r = HTTP.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r.json()


def fetch_greenhouse(c):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs")
    return [Job(str(j["id"]), clean(j.get("title")), j.get("absolute_url", ""),
                clean((j.get("location") or {}).get("name")))
            for j in data.get("jobs", [])]


def fetch_lever(c):
    data = get_json(f"https://api.lever.co/v0/postings/{c['slug']}?mode=json")
    return [Job(str(j["id"]), clean(j.get("text")), j.get("hostedUrl", ""),
                clean((j.get("categories") or {}).get("location")))
            for j in data]


def fetch_ashby(c):
    slug = quote(c["slug"])
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        out.append(Job(str(j["id"]), clean(j.get("title")),
                       j.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{j['id']}",
                       clean(j.get("location"))))
    return out


def fetch_smartrecruiters(c):
    out, offset = [], 0
    while True:
        data = get_json(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings",
                        params={"limit": 100, "offset": offset})
        for j in data.get("content", []):
            loc = j.get("location") or {}
            out.append(Job(str(j["id"]), clean(j.get("name")),
                           f"https://jobs.smartrecruiters.com/{c['slug']}/{j['id']}",
                           clean(", ".join(filter(None, [loc.get("city"), loc.get("country")])))))
        offset += len(data.get("content") or [])
        if offset >= data.get("totalFound", 0) or not data.get("content"):
            return out


def fetch_amazon(c):
    params = dict(c.get("params") or {})
    limit = int(params.get("result_limit", 100))
    out = []
    for page in range(int(c.get("pages", 1))):
        params["offset"] = page * limit
        data = get_json("https://www.amazon.jobs/en/search.json", params=params)
        for j in data.get("jobs", []):
            jid = str(j.get("id_icims") or j.get("id"))
            out.append(Job(jid, clean(j.get("title")),
                           urljoin("https://www.amazon.jobs", j.get("job_path", f"/en/jobs/{jid}")),
                           clean(j.get("normalized_location") or j.get("location"))))
        if len(data.get("jobs", [])) < limit:
            break
    return out


def with_page(url: str, page: int) -> str:
    if page == 1:
        return url
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query))
    q["page"] = str(page)
    return urlunparse(parts._replace(query=urlencode(q)))


def fetch_apple(c):
    """Apple server-renders its results into window.__staticRouterHydrationData."""
    out = []
    for page in range(1, int(c.get("pages", 1)) + 1):
        r = HTTP.get(with_page(c["url"], page), timeout=TIMEOUT)
        r.raise_for_status()
        jobs = parse_apple_html(r.text)
        out.extend(jobs)
        if not jobs:
            break
    return out


def parse_apple_html(page_html: str) -> list[Job]:
    jobs = []
    m = re.search(r'__staticRouterHydrationData\s*=\s*JSON\.parse\("(.*?)"\);', page_html, re.S)
    if m:
        try:
            data = json.loads(json.loads(f'"{m.group(1)}"'))
            results = data["loaderData"]["search"]["searchResults"]
            for j in results:
                pid = str(j.get("positionId") or j.get("id"))
                slug = j.get("transformedPostingTitle", "")
                team = (j.get("team") or {}).get("teamCode", "")
                locs = ", ".join(clean(l.get("name")) for l in j.get("locations", []) if l.get("name"))
                url = f"https://jobs.apple.com/en-us/details/{pid}/{slug}"
                if team:
                    url += f"?team={team}"
                jobs.append(Job(pid, clean(j.get("postingTitle")), url, locs))
            return jobs
        except (KeyError, ValueError, TypeError):
            pass  # fall back to link scraping below
    return extract_links(page_html, "https://jobs.apple.com/",
                         r"/details/(?P<id>[\w-]+?)/(?P<slug>[\w-]+)")


# ── Generic "Visualping" fetcher: collect links that look like job postings ──
GENERIC_LINK_TEXT = {"", "apply", "apply now", "learn more", "view", "view job",
                     "see details", "details", "read more", "more", "new", "hot",
                     "featured", "urgent", "just posted", "promoted"}
_PREFIX_NOISE = re.compile(r"^(learn more about|apply (now )?for|view job:?|job:)\s+", re.I)


def _segments(a) -> list[str]:
    """Visible text pieces inside a link, minus badges like 'New' or 'Apply'."""
    out = []
    for s in a.stripped_strings:
        s = clean(_LOC_NOISE.sub(" ", _PREFIX_NOISE.sub("", clean(s))))  # '(opens in new tab)', '→'
        if s and s.lower() not in GENERIC_LINK_TEXT:
            out.append(s)
    if not out and a.get("aria-label"):
        out = [_PREFIX_NOISE.sub("", clean(a["aria-label"]))]
    return out


def _job_from_match(m, href, segs: list[str]) -> tuple[Job, bool]:
    """Returns (job, title_is_real). A job card link usually contains the title first and the
    location after it as separate pieces of text, so we split them instead of gluing them."""
    gd = m.groupdict()
    if not segs or len(segs[0]) > 200:
        return Job(gd["id"], title_from_slug(gd.get("slug") or "") or f"Job {gd['id']}", href), False
    location = clean(" ".join(segs[1:]))[:200]
    return Job(gd["id"], segs[0], href, location), True


def _json_in_scripts(soup) -> dict:
    """Job records hidden in embedded JSON (e.g. Next.js __NEXT_DATA__) → {id: (title, loc)}."""
    found: dict = {}
    for s in soup.find_all("script"):
        typ = (s.get("type") or "").lower()
        if s.get("id") == "__NEXT_DATA__" or "json" in typ:
            try:
                walk_json_for_jobs(json.loads(s.string or ""), found)
            except (ValueError, TypeError):
                pass
    return found


def extract_links(page_html: str, base_url: str, pattern: str) -> list[Job]:
    rx = re.compile(pattern)
    found: dict[str, Job] = {}
    real: set[str] = set()
    soup = BeautifulSoup(page_html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        m = rx.search(href)
        if not m:
            continue
        # A card often has several links to the same job; keep the best title.
        job, is_real = _job_from_match(m, href, _segments(a))
        if job.id not in found or (is_real and job.id not in real):
            found[job.id] = job
            if is_real:
                real.add(job.id)
    # Fallback: hrefs inside JSON/script blobs that aren't real <a> tags
    if not found:
        for m in rx.finditer(page_html):
            jid = m.group("id")
            if jid not in found:
                href = m.group(0)
                if re.match(r"[\w.-]+\.[a-z]{2,}/", href):  # matched 'domain.com/...' w/o scheme
                    href = "https://" + href
                found[jid] = _job_from_match(m, urljoin(base_url, href), [])[0]
    # Fill guessed titles from JSON embedded in the page, when there is any
    missing = [j for j in found.values() if j.id not in real]
    if missing:
        embedded = _json_in_scripts(soup)
        for j in missing:
            if j.id in embedded:
                j.title, loc = embedded[j.id]
                j.location = j.location or loc
    return list(found.values())


def fetch_detail_title(job: Job) -> None:
    """Last resort for 'Job 12345' titles: open the posting itself and read its heading."""
    try:
        r = HTTP.get(job.url, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        og = soup.find("meta", property="og:title")
        h1 = soup.find("h1")
        for cand in (og.get("content") if og else None,
                     h1.get_text(" ") if h1 else None,
                     soup.title.string if soup.title else None):
            cand = clean(cand)
            if cand and cand.lower() not in GENERIC_LINK_TEXT and len(cand) < 200:
                job.title = cand
                return
    except Exception:
        pass  # keep the placeholder; the link still works


def walk_json_for_jobs(obj, out: dict, depth=0):
    """Find dicts that look like job records ({id, title|name}) in arbitrary JSON."""
    if depth > 25:
        return
    if isinstance(obj, dict):
        jid = obj.get("id") or obj.get("jobId") or obj.get("job_id")
        title = obj.get("title") or obj.get("name") or obj.get("postingTitle")
        if jid and isinstance(title, str) and re.fullmatch(r"[\w-]{4,}", str(jid)) and 3 < len(title) < 200:
            loc = obj.get("location") or obj.get("locations") or ""
            if isinstance(loc, list):
                loc = ", ".join(x if isinstance(x, str) else str(x.get("name", "")) for x in loc[:3])
            elif isinstance(loc, dict):
                loc = loc.get("name", "")
            out.setdefault(str(jid), (clean(title), clean(str(loc))))
        for v in obj.values():
            walk_json_for_jobs(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            walk_json_for_jobs(v, out, depth + 1)


class Browser:
    """Lazy Playwright Chromium, shared across companies in one run."""
    _pw = _browser = None

    @classmethod
    def render(cls, url: str, json_hint: str | None):
        from playwright.sync_api import sync_playwright  # imported only if needed
        if cls._browser is None:
            cls._pw = sync_playwright().start()
            cls._browser = cls._pw.chromium.launch(headless=True)
        ctx = cls._browser.new_context(user_agent=UA, locale="en-US",
                                       viewport={"width": 1366, "height": 2000})
        page = ctx.new_page()
        captured = []
        if json_hint:
            def on_response(resp):
                try:
                    if (json_hint == "*" or json_hint in resp.url) and \
                            "json" in (resp.headers.get("content-type") or ""):
                        captured.append({"__url": resp.url, "data": resp.json()})
                except Exception:
                    pass
            page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            for _ in range(4):  # nudge lazy-loading lists
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1200)
            return page.content(), page.url, captured
        finally:
            ctx.close()

    @classmethod
    def close(cls):
        if cls._browser:
            cls._browser.close()
            cls._pw.stop()
            cls._browser = cls._pw = None


def fetch_links(c):
    out: dict[str, Job] = {}
    for page in range(1, int(c.get("pages", 1)) + 1):
        url = with_page(c["url"], page)
        if c.get("mode", "http") == "browser":
            page_html, final_url, captured = Browser.render(url, c.get("json_hint"))
        else:
            r = HTTP.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            page_html, final_url, captured = r.text, r.url, []
        jobs = extract_links(page_html, final_url, c["pattern"])
        # If the page builds its list from an API call, use that data too.
        if captured and c.get("url_template"):
            from_json: dict = {}
            for blob in captured:
                walk_json_for_jobs(blob, from_json)
            link_ids = {j.id for j in jobs}
            for jid, (title, loc) in from_json.items():
                if jid in link_ids:  # enrich titles/locations
                    for j in jobs:
                        if j.id == jid:
                            j.title = j.title if not j.title.startswith("Job ") else title
                            j.location = j.location or loc
                elif not link_ids:  # page had no usable links → trust the API data
                    jobs.append(Job(jid, title, c["url_template"].format(id=jid), loc))
        new_here = [j for j in jobs if j.id not in out]
        for j in jobs:
            out.setdefault(j.id, j)
        if page > 1 and not new_here:
            break
    return list(out.values())


def inspect(url: str, browser: bool):
    """Debug helper for a careers page: shows which link shapes and JSON API calls it has,
    so you can write a `pattern` / `json_hint` for config.yaml."""
    if browser:
        page_html, final_url, captured = Browser.render(url, "*")
        Browser.close()
    else:
        r = HTTP.get(url, timeout=TIMEOUT)
        page_html, final_url, captured = r.text, r.url, []
    soup = BeautifulSoup(page_html, "html.parser")
    shapes: dict[str, list] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(final_url, a["href"])
        if not re.search(r"\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}", href):
            continue  # job links almost always contain a long ID
        shape = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}", "<uuid>", href.split("?")[0])
        shape = re.sub(r"\d{4,}", "<id>", shape)
        shapes.setdefault(shape, []).append((href, " / ".join(_segments(a))[:80]))
    print(f"\nPage: {final_url}\n{len(page_html):,} bytes of HTML\n")
    print("Link shapes with IDs (most common first) — a job pattern is usually the top one:")
    for shape, items in sorted(shapes.items(), key=lambda kv: -len(kv[1]))[:8]:
        print(f"\n  {len(items):>3}×  {shape}")
        for href, text in items[:3]:
            print(f"        {text or '(no text)'}\n          {href}")
    if not shapes:
        print("  (none — try --browser, the list is probably built by JavaScript)")
    if captured:
        print("\nJSON responses the page loaded (a `json_hint` is any unique bit of the URL):")
        for c in captured:
            found: dict = {}
            walk_json_for_jobs(c["data"], found)
            print(f"  {len(found):>3} job-like records  {c['__url'][:150]}")
    print()


# Extra places recognised by the --locations report (filtering accepts any name too).
TALLY_PLACES = {
    "italy": ["italy", "milan", "rome"],
    "belgium": ["belgium", "brussels"],
    "romania": ["romania", "rou", "iasi", "bucharest"],
    "turkey": ["turkey", "türkiye", "turkiye", "istanbul"],
    "saudi arabia": ["saudi arabia", "riyadh", "jeddah"],
    "thailand": ["thailand", "bangkok"],
    "indonesia": ["indonesia", "jakarta"],
    "philippines": ["philippines", "manila"],
    "netherlands": ["netherlands", "amsterdam"],
    "switzerland": ["switzerland", "zurich", "zürich", "geneva"],
    "sweden": ["sweden", "stockholm"],
    "denmark": ["denmark", "copenhagen"],
    "spain": ["spain", "madrid", "barcelona"],
    "poland": ["poland", "warsaw", "krakow", "kraków"],
    "israel": ["israel", "tel aviv", "haifa"],
    "uae": ["united arab emirates", "uae", "dubai", "abu dhabi"],
    "india": ["india", "bangalore", "bengaluru", "hyderabad", "mumbai", "pune", "gurgaon",
              "gurugram", "chennai", "new delhi"],
    "china (mainland)": ["china", "beijing", "shanghai", "shenzhen"],
    "taiwan": ["taiwan", "taipei"],
    "brazil": ["brazil", "são paulo", "sao paulo"],
    "mexico": ["mexico", "mexico city"],
}


def location_report(cfg):
    """Fetch every company with NO filters and count open jobs per country."""
    from collections import Counter, defaultdict
    known = list(COUNTRY_ALIASES) + list(TALLY_PLACES)
    rx = {k: _location_rx(k) for k in COUNTRY_ALIASES}
    rx.update({k: re.compile("|".join(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])" for w in v))
               for k, v in TALLY_PLACES.items()})
    totals, by_company = Counter(), defaultdict(Counter)
    unmatched = Counter()
    sampled = []
    try:
        for c in cfg["companies"]:
            c = dict(c)
            if c["source"] == "amazon":  # drop the country restriction for this survey
                c["params"] = {k: v for k, v in (c.get("params") or {}).items()
                               if "country" not in k}
            try:
                jobs = FETCHERS[c["source"]](c)
            except Exception as e:
                print(f"✗ {c['name']}: {type(e).__name__}: {str(e)[:120]}")
                continue
            if c["source"] in ("links", "apple", "amazon"):
                sampled.append(c["name"])
            print(f"✓ {c['name']:<24} {len(jobs)} jobs")
            for j in jobs:
                norm = normalize_location(j.location)
                loc = norm.lower()
                hits = [k for k in known if loc and rx[k].search(loc)]
                if not loc:
                    hits = ["(location not listed)"]
                elif REMOTE_ANYWHERE.match(norm):
                    hits = ["remote / anywhere"]
                elif not hits:
                    hits = ["other"]
                    unmatched[norm[:60]] += 1
                for h in hits:
                    totals[h] += 1
                    by_company[h][c["name"]] += 1
    finally:
        Browser.close()

    print("\nOpen jobs by country (a job listed in several countries counts in each):\n")
    for place, n in totals.most_common():
        top = ", ".join(f"{name} {k}" for name, k in by_company[place].most_common(6))
        print(f"  {place:<22} {n:>5}   {top}")
    if unmatched:
        print("\n  Unrecognised locations (most common):")
        for loc, n in unmatched.most_common(15):
            print(f"      {n:>4}  {loc}")
    if sampled:
        print(f"\nNote: {', '.join(sampled)} only show their newest postings to us, so their "
              "numbers are a sample, not the full picture.")


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "amazon": fetch_amazon,
    "apple": fetch_apple,
    "links": fetch_links,
}


# ─────────────────────────── filtering ─────────────────────────────
def _has_term(title_lc: str, term: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", title_lc) is not None


# Every site writes locations differently ("Mountain View, CA, USA", "Fremont, California,
# United States", "London", "HKG"), so each country gets a list of names, codes and big
# tech cities. Short codes (CA, NY, UK...) only count at the end of a comma-separated
# segment, e.g. "Seattle, WA" — so "CA" inside a word never matches.
_US_STATES = ("alabama al alaska ak arizona az arkansas ar california ca colorado co connecticut ct "
              "delaware de florida fl georgia ga hawaii hi idaho id illinois il indiana in iowa ia "
              "kansas ks kentucky ky louisiana la maine me maryland md massachusetts ma michigan mi "
              "minnesota mn mississippi ms missouri mo montana mt nebraska ne nevada nv ohio oh "
              "oklahoma ok oregon or pennsylvania pa tennessee tn texas tx utah ut vermont vt "
              "virginia va washington wa wisconsin wi wyoming wy").split()
COUNTRY_ALIASES = {
    "us": {
        "names": ["united states", "united states of america", "usa", "u.s.", "u.s.a.", "us",
                  "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
                  "north dakota", "rhode island", "south carolina", "south dakota",
                  "west virginia", "district of columbia", "washington dc", "washington d.c.",
                  "san francisco", "bay area", "mountain view", "sunnyvale", "palo alto",
                  "menlo park", "cupertino", "san jose", "santa clara", "redmond", "seattle",
                  "bellevue", "new york city", "nyc", "brooklyn", "boston", "cambridge, ma",
                  "austin", "los angeles", "chicago", "pittsburgh", "philadelphia", "atlanta",
                  "denver", "salt lake city", "fremont", "oakland", "phoenix", "miami", "dallas"],
        # "in" (Indiana) is left out on purpose: sites also write India as "Bangalore, IN".
        "codes": [s for s in _US_STATES if len(s) == 2 and s != "in"]
                 + ["nh", "nj", "nm", "ny", "nc", "nd", "ri", "sc", "sd", "wv", "dc"],
        "long_states": [s for s in _US_STATES if len(s) > 2],
    },
    "hong kong": {"names": ["hong kong", "hong kong sar", "hkg", "kowloon"], "codes": ["hk"]},
    "germany": {"names": ["germany", "deutschland", "deu", "berlin", "munich", "münchen",
                          "hamburg", "frankfurt", "cologne", "köln", "stuttgart", "düsseldorf"],
                "codes": ["de"]},
    "uk": {"names": ["united kingdom", "uk", "u.k.", "gbr", "great britain", "england",
                     "scotland", "wales", "northern ireland", "london", "edinburgh",
                     "manchester", "oxford", "bristol", "cambridge, uk"], "codes": ["gb"]},
    "new zealand": {"names": ["new zealand", "nzl", "aotearoa", "auckland", "wellington",
                              "christchurch"], "codes": ["nz"]},
    "japan": {"names": ["japan", "jpn", "tokyo", "osaka", "kyoto", "yokohama", "fukuoka"],
              "codes": ["jp"]},
    "singapore": {"names": ["singapore", "sgp"], "codes": ["sg"]},
    "ireland": {"names": ["ireland", "irl", "dublin", "cork", "galway", "limerick"],
                "codes": ["ie"]},
    "australia": {"names": ["australia", "aus", "sydney", "melbourne", "brisbane", "perth",
                            "adelaide", "canberra"], "codes": ["au"]},
    "france": {"names": ["france", "fra", "paris", "lyon", "grenoble", "toulouse", "sophia antipolis"],
               "codes": ["fr"]},
    # "ca" is left out on purpose: it almost always means California. "can" is the ISO code.
    "canada": {"names": ["canada", "can", "toronto", "vancouver", "montreal", "montréal",
                         "ottawa", "waterloo", "calgary", "brampton", "ontario",
                         "british columbia", "quebec", "québec"], "codes": []},
    "south korea": {"names": ["south korea", "korea", "republic of korea", "kor", "seoul",
                              "pangyo", "seongnam"], "codes": ["kr"]},
}
REMOTE_ANYWHERE = re.compile(r"^\s*(remote|anywhere|worldwide|global|remote\s*[-–(]\s*(global|"
                             r"worldwide|anywhere)\)?)\s*$", re.I)


def _location_rx(country: str) -> re.Pattern:
    a = COUNTRY_ALIASES.get(country.lower())
    if a is None:  # a country/city we have no table for: plain whole-word match
        return re.compile(r"(?<![a-z])" + re.escape(country.lower()) + r"(?![a-z])")
    words = a["names"] + a.get("long_states", [])
    parts = [r"(?<![a-z])" + re.escape(w) + r"(?![a-z])" for w in words]
    # codes: only as a whole comma/pipe-separated segment, e.g. "Seattle, WA" or "WA, USA"
    parts += [r"(?:^|[,|/;]\s*)" + re.escape(c) + r"\s*(?=$|[,|/;)])" for c in a["codes"]]
    return re.compile("|".join(parts))


_RX_CACHE: dict[str, re.Pattern] = {}
_LOC_NOISE = re.compile(r"\((opens in (a )?new (tab|window))\)|[→›»↗]", re.I)


def normalize_location(location: str) -> str:
    """'(opens in new tab) San Diego, CA →' → 'San Diego, CA';
    'Menlo Park, CA ⋅ Redmond, WA' → 'Menlo Park, CA | Redmond, WA'."""
    loc = _LOC_NOISE.sub(" ", location or "")
    loc = re.sub(r"\s*[⋅·•]\s*", " | ", loc)
    return clean(loc).strip(" |")


def location_ok(location: str, wanted: list[str] | None, include_remote: bool = True) -> bool:
    """True if the job is in one of the wanted countries. Jobs with no location
    listed pass (we can't tell, and a missed job is worse than an extra one)."""
    location = normalize_location(location)
    if not wanted or not location:
        return True
    loc = location.lower()
    if include_remote and any(REMOTE_ANYWHERE.match(p) for p in re.split(r"[|;/]", location)):
        return True
    for country in wanted:
        rx = _RX_CACHE.setdefault(country.lower(), _location_rx(country))
        if rx.search(loc):
            return True
    return False


def passes(job: Job, f: dict | None, locations: list[str] | None = None,
           include_remote: bool = True) -> bool:
    f = f or {}
    t = job.title.lower()
    if any(_has_term(t, x) for x in f.get("exclude", [])):
        return False
    inc = f.get("include")
    if inc and not any(_has_term(t, x) for x in inc):
        return False
    return location_ok(job.location, f.get("locations", locations), include_remote)


# ─────────────────────────── state ─────────────────────────────────
def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"companies": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def diff_company(cstate: dict, jobs: list[Job], now_iso: str) -> list[Job]:
    """Return jobs never seen before. Records every fetched ID (pre-filter) as seen.
    The very first successful fetch only seeds the list, so you don't get
    hundreds of 'new' jobs on day one."""
    seen = cstate.setdefault("seen", {})
    first_time = not cstate.get("seeded")
    new = [j for j in jobs if j.id not in seen]
    for j in jobs:
        seen.setdefault(j.id, now_iso)
    cstate["seeded"] = True
    # forget IDs not seen in a year so the file doesn't grow forever
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    current = {j.id for j in jobs}
    for jid in [k for k, v in seen.items() if v < cutoff and k not in current]:
        del seen[jid]
    return [] if first_time else new


# ─────────────────────────── email ─────────────────────────────────
def smtp_settings():
    return {
        "host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.environ.get("SMTP_PORT", "465")),
        "user": os.environ.get("SMTP_USER"),
        "password": os.environ.get("SMTP_PASS"),
        "to": os.environ.get("EMAIL_TO") or os.environ.get("SMTP_USER"),
    }


def send_email(subject: str, html_body: str, text_body: str) -> bool:
    s = smtp_settings()
    if not (s["user"] and s["password"] and s["to"]):
        print("  (email not configured — set SMTP_USER, SMTP_PASS, EMAIL_TO)")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, s["user"], s["to"]
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    if s["port"] == 465:
        with smtplib.SMTP_SSL(s["host"], s["port"], timeout=30) as srv:
            srv.login(s["user"], s["password"])
            srv.sendmail(s["user"], [x.strip() for x in s["to"].split(",")], msg.as_string())
    else:
        with smtplib.SMTP(s["host"], s["port"], timeout=30) as srv:
            srv.starttls()
            srv.login(s["user"], s["password"])
            srv.sendmail(s["user"], [x.strip() for x in s["to"].split(",")], msg.as_string())
    return True


def build_digest(new_by_company: dict[str, list[Job]], cap: int):
    total = sum(len(v) for v in new_by_company.values())
    names = ", ".join(new_by_company)
    subject = f"🆕 {total} new job{'s' * (total != 1)}: {names}"
    if len(subject) > 120:
        subject = subject[:117] + "…"
    parts, text = [], []
    for company, jobs in new_by_company.items():
        rows = []
        text.append(f"\n== {company} ({len(jobs)}) ==")
        for j in jobs[:cap]:
            loc = f" — {html.escape(j.location)}" if j.location else ""
            rows.append(f'<li><a href="{html.escape(j.url)}">{html.escape(j.title)}</a>'
                        f'<span style="color:#666">{loc}</span></li>')
            text.append(f"- {j.title}{' — ' + j.location if j.location else ''}\n  {j.url}")
        if len(jobs) > cap:
            rows.append(f"<li><i>…and {len(jobs) - cap} more</i></li>")
        parts.append(f"<h3 style='margin:18px 0 6px'>{html.escape(company)} "
                     f"<span style='color:#888;font-weight:normal'>({len(jobs)})</span></h3>"
                     f"<ul style='margin:0;padding-left:20px'>{''.join(rows)}</ul>")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = (f"<div style='font-family:system-ui,Segoe UI,sans-serif;font-size:14px'>"
            f"{''.join(parts)}<p style='color:#999;font-size:12px;margin-top:24px'>"
            f"Job Watcher · checked {stamp}</p></div>")
    return subject, body, "\n".join(text)


# ─────────────────────────── main loop ─────────────────────────────
def run(args):
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    filters = cfg.get("filters", {})
    settings = cfg.get("settings", {})
    alert_after = int(settings.get("failure_alert_after", 4))
    cap = int(settings.get("max_jobs_per_company_in_email", 25))
    state = load_state()
    now_iso = datetime.now(timezone.utc).isoformat()

    new_by_company: dict[str, list[Job]] = {}
    broken: list[str] = []
    try:
        for c in cfg["companies"]:
            name = c["name"]
            if args.only and name.lower() not in [o.lower() for o in args.only]:
                continue
            cstate = state["companies"].setdefault(name, {})
            t0 = time.time()
            try:
                jobs = FETCHERS[c["source"]](c)
                # A scraped page returning zero links almost always means the page
                # changed or blocked us — treat it as a failure, not "no jobs".
                if not jobs and c["source"] in ("links", "apple"):
                    raise RuntimeError("0 job links found (page layout changed or request blocked?)")
            except Exception as e:  # one broken site never stops the others
                cstate["fail_streak"] = cstate.get("fail_streak", 0) + 1
                cstate["last_error"] = f"{type(e).__name__}: {e}"[:300]
                print(f"✗ {name:<24} FAILED ({cstate['fail_streak']}x): {cstate['last_error']}")
                if args.verbose:
                    traceback.print_exc()
                if cstate["fail_streak"] >= alert_after and not cstate.get("alerted"):
                    broken.append(f"{name}: {cstate['last_error']}")
                    cstate["alerted"] = True
                continue

            if cstate.get("alerted"):
                print(f"  {name} recovered")
            cstate.update(fail_streak=0, alerted=False, last_ok=now_iso, last_count=len(jobs))
            cstate.pop("last_error", None)
            was_seeded = bool(cstate.get("seeded"))
            new = diff_company(cstate, jobs, now_iso)
            for j in new:  # only new jobs, so this costs a request only when something appears
                if re.fullmatch(r"Job \S+", j.title):
                    fetch_detail_title(j)
            locs = c.get("locations", settings.get("locations"))   # per-company override
            matched = [j for j in new if passes(j, filters.get(c.get("filter")), locs,
                                                settings.get("include_remote", True))]
            tag = "seeded" if not was_seeded else f"{len(new)} new, {len(matched)} match filter"
            print(f"✓ {name:<24} {len(jobs):>5} jobs  ({tag})  {time.time() - t0:.1f}s")
            if args.check:
                for j in jobs[:3]:
                    print(f"      e.g. {j.title[:70]}  [{j.id}]")
            if matched:
                new_by_company[name] = matched
    finally:
        Browser.close()

    # The same posting can show up under two entries (e.g. Meta and Meta Reality Labs).
    # Keep it only under the one listed later in config.yaml, which is the more specific one.
    claimed: set[str] = set()
    for name in reversed(list(new_by_company)):
        kept = [j for j in new_by_company[name] if (j.url or j.id) not in claimed]
        claimed.update(j.url or j.id for j in kept)
        if kept:
            new_by_company[name] = kept
        else:
            del new_by_company[name]

    if new_by_company:
        subject, body, text = build_digest(new_by_company, cap)
        print(f"\n{subject}\n{text}")
        if not args.dry_run:
            send_email(subject, body, text)
    else:
        print("\nNo new matching jobs.")

    if broken and not args.dry_run:
        send_email("⚠️ Job Watcher: a source stopped working",
                   "<p>These sources have failed several checks in a row. The careers page "
                   "probably changed — update its entry in config.yaml.</p><ul>"
                   + "".join(f"<li>{html.escape(b)}</li>" for b in broken) + "</ul>",
                   "\n".join(broken))

    if not args.dry_run:
        save_state(state)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="don't email, don't save state")
    p.add_argument("--check", action="store_true", help="print sample jobs per company (implies --dry-run)")
    p.add_argument("--only", nargs="+", help="only these company names")
    p.add_argument("--test-email", action="store_true", help="send a test email and exit")
    p.add_argument("--inspect", metavar="URL", help="show the job-link shapes on a careers page")
    p.add_argument("--browser", action="store_true", help="with --inspect: render with Chromium")
    p.add_argument("--locations", action="store_true",
                   help="survey: count every company's open jobs by country (no filters)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    if args.inspect:
        inspect(args.inspect, args.browser)
        return
    if args.locations:
        location_report(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")))
        return
    if args.check:
        args.dry_run = True
    if args.test_email:
        ok = send_email("✅ Job Watcher test", "<p>Email works! 搞掂 🎉</p>", "Email works!")
        sys.exit(0 if ok else 1)
    run(args)


if __name__ == "__main__":
    main()
