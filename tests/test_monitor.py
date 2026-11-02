"""Offline tests: every parser is fed a fake response, no network needed.
Run:  python -m pytest -q
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor as m  # noqa: E402


class FakeResp:
    def __init__(self, payload=None, text="", url="https://x/"):
        self._p, self.text, self.url = payload, text, url

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


def fake_get(payload=None, text=""):
    return lambda url, **kw: FakeResp(payload, text, url)


# ── API fetchers ────────────────────────────────────────────────
def test_greenhouse(monkeypatch):
    monkeypatch.setattr(m.HTTP, "get", fake_get({"jobs": [
        {"id": 123, "title": "Software Engineer, New Grad", "absolute_url": "https://g/123",
         "location": {"name": "Fremont, CA"}}]}))
    [j] = m.fetch_greenhouse({"slug": "neuralink"})
    assert (j.id, j.title, j.location) == ("123", "Software Engineer, New Grad", "Fremont, CA")


def test_lever(monkeypatch):
    monkeypatch.setattr(m.HTTP, "get", fake_get([
        {"id": "abc", "text": "ML Engineer", "hostedUrl": "https://l/abc",
         "categories": {"location": "Stockholm"}}]))
    [j] = m.fetch_lever({"slug": "spotify"})
    assert j.id == "abc" and j.location == "Stockholm"


def test_ashby_skips_unlisted_and_encodes_slug(monkeypatch):
    seen = {}

    def get(url, **kw):
        seen["url"] = url
        return FakeResp({"jobs": [
            {"id": "u1", "title": "Data Analyst", "location": "SF", "jobUrl": "https://a/u1"},
            {"id": "u2", "title": "Hidden", "isListed": False}]})
    monkeypatch.setattr(m.HTTP, "get", get)
    jobs = m.fetch_ashby({"slug": "Merge Labs"})
    assert [j.id for j in jobs] == ["u1"]
    assert "Merge%20Labs" in seen["url"]


def test_smartrecruiters_paginates(monkeypatch):
    pages = iter([
        {"totalFound": 2, "content": [{"id": "1", "name": "Firmware Engineer",
                                       "location": {"city": "Boston", "country": "us"}}]},
        {"totalFound": 2, "content": [{"id": "2", "name": "ML Scientist", "location": {}}]},
    ])
    monkeypatch.setattr(m.HTTP, "get", lambda url, **kw: FakeResp(next(pages)))
    jobs = m.fetch_smartrecruiters({"slug": "NeurableInc"})
    assert [j.id for j in jobs] == ["1", "2"]
    assert jobs[0].url == "https://jobs.smartrecruiters.com/NeurableInc/1"


def test_amazon(monkeypatch):
    monkeypatch.setattr(m.HTTP, "get", fake_get({"jobs": [
        {"id_icims": "300001", "title": "SDE I", "job_path": "/en/jobs/300001/sde-i",
         "normalized_location": "Seattle, WA, USA"}]}))
    [j] = m.fetch_amazon({"params": {"result_limit": 100}, "pages": 3})
    assert j.url == "https://www.amazon.jobs/en/jobs/300001/sde-i"


# ── page scrapers ───────────────────────────────────────────────
def test_apple_hydration_json():
    data = {"loaderData": {"search": {"searchResults": [
        {"positionId": "200600001", "postingTitle": "ML Engineer, Siri",
         "transformedPostingTitle": "ml-engineer-siri", "team": {"teamCode": "MLAI"},
         "locations": [{"name": "Cupertino"}]}]}}}
    blob = json.dumps(json.dumps(data))[1:-1]  # same double-encoding Apple uses
    page = f'<script>window.__staticRouterHydrationData = JSON.parse("{blob}");</script>'
    [j] = m.parse_apple_html(page)
    assert j.id == "200600001" and j.title == "ML Engineer, Siri"
    assert j.url.endswith("/details/200600001/ml-engineer-siri?team=MLAI")


def test_apple_falls_back_to_links():
    page = '<a href="/en-us/details/200600002-0836/swe-intern?team=STDNT">SWE Intern</a>'
    [j] = m.parse_apple_html(page)
    assert j.id == "200600002-0836" and j.title == "SWE Intern"


def test_google_style_links_title_from_slug():
    page = """
      <a href="jobs/results/111-software-engineer-university-graduate-2027?q=">Learn more</a>
      <a href="jobs/results/111-software-engineer-university-graduate-2027">Learn more</a>
      <a href="/about">About</a>"""
    jobs = m.extract_links(page, "https://www.google.com/about/careers/applications/",
                           r"jobs/results/(?P<id>\d+)-(?P<slug>[a-z0-9-]+)")
    assert len(jobs) == 1
    assert jobs[0].title == "Software Engineer University Graduate 2027"


def test_links_prefers_real_text_over_slug():
    page = """<a href="https://p.applytojob.com/apply/AbC123xyz/Software-Engineer">Apply</a>
              <a href="https://p.applytojob.com/apply/AbC123xyz/Software-Engineer">Software Engineer Oakland, CA</a>"""
    [j] = m.extract_links(page, "https://p.applytojob.com/apply/",
                          r"applytojob\.com/apply/(?P<id>[A-Za-z0-9]{6,})/(?P<slug>[\w-]+)")
    assert j.title == "Software Engineer Oakland, CA"


def test_raw_html_fallback_adds_scheme():
    page = '<script>{"u":"metacareers.com/profile/job_details/987654321/"}</script>'
    [j] = m.extract_links(page, "https://www.metacareers.com/jobsearch/",
                          r"metacareers\.com/(?:profile/job_details|jobs)/(?P<id>\d{6,})")
    assert j.url == "https://metacareers.com/profile/job_details/987654321"


def test_json_walker_finds_job_records():
    blob = {"data": {"results": [{"id": "1234567", "title": "Research Scientist, Reality Labs",
                                  "locations": ["Redmond, WA"]}, {"id": 1, "name": "x"}]}}
    out = {}
    m.walk_json_for_jobs(blob, out)
    assert out == {"1234567": ("Research Scientist, Reality Labs", "Redmond, WA")}


# ── filtering ───────────────────────────────────────────────────
F = {"include": ["software", "ml", "ai", "intern"], "exclude": ["senior", "sr", "staff"]}


@pytest.mark.parametrize("title,ok", [
    ("Software Engineer, New Grad", True),
    ("ML Engineer", True),
    ("AI Research Intern", True),
    ("Senior Software Engineer", False),
    ("Sr. Software Engineer", False),
    ("Staff ML Engineer", False),
    ("Email Marketing Manager", False),   # 'ai' must not match inside 'email'
    ("Sales Associate", False),
])
def test_filter(title, ok):
    assert m.passes(m.Job("1", title, "u"), F) is ok


def test_no_filter_means_everything():
    assert m.passes(m.Job("1", "Anything", "u"), None)


# ── state / diffing ─────────────────────────────────────────────
def J(i):
    return m.Job(str(i), f"Job {i}", "u")


def test_first_run_seeds_silently_then_detects_new():
    cs = {}
    assert m.diff_company(cs, [J(1), J(2)], "2026-11-02T00:00:00+00:00") == []
    new = m.diff_company(cs, [J(1), J(2), J(3)], "2026-11-02T00:15:00+00:00")
    assert [j.id for j in new] == ["3"]
    assert m.diff_company(cs, [J(1), J(3)], "2026-11-02T00:30:00+00:00") == []


def test_reposted_job_is_not_new_again():
    cs = {}
    m.diff_company(cs, [J(1)], "2026-11-02T00:00:00+00:00")
    m.diff_company(cs, [], "2026-11-02T00:15:00+00:00")          # temporarily gone
    assert m.diff_company(cs, [J(1)], "2026-11-02T00:30:00+00:00") == []


# ── end-to-end run with fake companies ──────────────────────────
def test_full_run_sends_one_digest(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
filters: {f: {exclude: [senior]}}
companies:
  - {name: GoodCo, source: greenhouse, slug: good, filter: f}
  - {name: BrokenCo, source: links, mode: http, url: "https://broken/", pattern: 'x/(?P<id>\\d+)'}
settings: {failure_alert_after: 2}
""")
    monkeypatch.setattr(m, "CONFIG_PATH", cfg)
    monkeypatch.setattr(m, "STATE_PATH", tmp_path / "state.json")
    board = {"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "u1"}]}

    def get(url, **kw):
        if "broken" in url:
            raise ConnectionError("boom")
        return FakeResp(board)
    monkeypatch.setattr(m.HTTP, "get", get)
    sent = []
    monkeypatch.setattr(m, "send_email", lambda s, h, t: sent.append(s) or True)
    args = type("A", (), {"only": None, "dry_run": False, "check": False, "verbose": False})()

    m.run(args)                       # seeds GoodCo, BrokenCo fails (1x)
    assert sent == []
    board["jobs"] += [{"id": 2, "title": "Neural Decoding Engineer", "absolute_url": "u2"},
                      {"id": 3, "title": "Senior Engineer", "absolute_url": "u3"}]
    m.run(args)                       # 1 new matching job + BrokenCo hits 2 failures
    assert len(sent) == 2
    assert sent[0].startswith("🆕 1 new job: GoodCo")
    assert "stopped working" in sent[1]
    m.run(args)                       # nothing new, failure alert not repeated
    assert len(sent) == 2


# ── location filtering (strings copied from real job boards) ────
WANT = ["us", "hong kong", "germany", "uk", "new zealand"]


@pytest.mark.parametrize("loc,ok", [
    ("Mountain View, CA, USA", True),                       # Waymo
    ("Fremont, California, United States", True),           # Neuralink
    ("San Francisco, CA | New York City, NY", True),        # Anthropic
    ("Austin, Texas, United States; South San Francisco, California, United States", True),
    ("San Francisco", True),                                # OpenAI (city only)
    ("Remote worker located in the USA", True),             # Synchron
    ("Remote - US", True),
    ("Seattle, WA", True),
    ("Hong Kong", True),
    ("Kowloon, HK", True),
    ("Berlin, Germany", True),
    ("Munich, DE", True),
    ("London", True),                                       # Spotify
    ("London, UK", True),
    ("Auckland, New Zealand", True),
    ("Remote", True),                                       # include_remote
    ("", True),                                             # unknown → don't drop it
    ("Stockholm", False),                                   # Spotify
    ("Dubai", False),
    ("Melbourne, Victoria, Australia", False),              # Synchron AU
    ("Bangalore, IN", False),                               # India, not Indiana
    ("Toronto, Ontario, Canada", False),
    ("Remote - Canada", False),
    ("Singapore", False),
])
def test_location(loc, ok):
    assert m.location_ok(loc, WANT) is ok


def test_location_unknown_country_plain_match():
    assert m.location_ok("Zurich, Switzerland", ["zurich"])
    assert not m.location_ok("Paris, France", ["zurich"])


TOP = ["us", "uk", "japan", "singapore", "germany", "ireland", "australia", "france",
       "canada", "south korea", "hong kong"]


@pytest.mark.parametrize("loc", [
    "Tokyo, Japan", "Shibuya, JP", "Singapore", "Dublin, IE", "Sydney, NSW, Australia",
    "Paris, France", "Toronto, ON, Canada", "Seoul, KR", "Pangyo, South Korea"])
def test_top_countries_match(loc):
    assert m.location_ok(loc, TOP)


@pytest.mark.parametrize("loc", ["Bengaluru, India", "Hyderabad, IN", "Sao Paulo, Brazil"])
def test_india_and_others_still_filtered_out(loc):
    assert not m.location_ok(loc, TOP)


def test_remote_can_be_turned_off():
    assert not m.location_ok("Remote", WANT, include_remote=False)


def test_passes_combines_title_and_location():
    f = {"include": ["engineer"]}
    assert m.passes(m.Job("1", "Software Engineer", "u", "Hong Kong"), f, WANT)
    assert not m.passes(m.Job("1", "Software Engineer", "u", "Tokyo, Japan"), f, WANT)
    assert not m.passes(m.Job("1", "Designer", "u", "Hong Kong"), f, WANT)


# ── fixes from the first real --check run ──────────────────────
def test_card_splits_title_and_location():          # Meta / Microsoft cards
    page = ('<a href="/profile/job_details/998357492128826/"><div>Software Engineer, Machine Learning</div>'
            '<div><span>Sunnyvale, CA</span><span>⋅</span><span>Remote, US</span></div></a>')
    [j] = m.extract_links(page, "https://www.metacareers.com/jobsearch/",
                          r"metacareers\.com/(?:profile/job_details|jobs)/(?P<id>\d{6,})")
    assert j.title == "Software Engineer, Machine Learning"
    assert "Sunnyvale, CA" in j.location
    assert m.location_ok(j.location, ["us"])


def test_google_learn_more_prefix_removed():
    page = ('<a href="jobs/results/121423334280700614-corporate-performance-lead">'
            '<span>Learn more about Corporate Performance Lead, Google Cloud</span></a>')
    [j] = m.extract_links(page, "https://www.google.com/about/careers/applications/",
                          r"jobs/results/(?P<id>\d+)-(?P<slug>[a-z0-9-]+)")
    assert j.title == "Corporate Performance Lead, Google Cloud"


def test_badges_are_ignored():
    page = '<a href="https://jobs.uber.com/en/jobs/303487/"><b>New</b><h3>Staff Software Engineer</h3></a>'
    [j] = m.extract_links(page, "https://jobs.uber.com/en/jobs/",
                          r"jobs\.uber\.com/[a-z-]+/jobs/(?P<id>\d{4,})")
    assert (j.id, j.title) == ("303487", "Staff Software Engineer")


def test_uber_pattern_ignores_search_page_links():
    page = '<a href="/en/jobs/?radius=50&team=People">Search</a><a href="/en/jobs/300750/">SWE</a>'
    jobs = m.extract_links(page, "https://jobs.uber.com/en/teams/",
                           r"jobs\.uber\.com/[a-z-]+/jobs/(?P<id>\d{4,})")
    assert [j.id for j in jobs] == ["300750"]


def test_synchron_adp_links():
    url = ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?"
           "cid=d290c04e&ccId=19000101_000003&lang=en_US&jobId=9201684966510_1")
    page = f'<a href="{url}">Catheter Engineering Technician</a>'
    [j] = m.extract_links(page, "https://synchron.com/careers",
                          r"workforcenow\.adp\.com/.*[?&]jobId=(?P<id>[\d_]+)")
    assert (j.id, j.title) == ("9201684966510_1", "Catheter Engineering Technician")


def test_titles_from_embedded_next_data():           # Precision ("Job 25805")
    data = {"props": {"pageProps": {"jobs": [{"id": 25805, "title": "Clinical Data Scientist",
                                              "location": "New York, New York, United States"}]}}}
    page = (f'<a href="/precision-neuroscience/25805"></a>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>')
    [j] = m.extract_links(page, "https://careers.kula.ai/precision-neuroscience",
                          r"careers\.kula\.ai/precision-neuroscience/(?P<id>\d+)")
    assert j.title == "Clinical Data Scientist" and "New York" in j.location


def test_detail_page_title_fallback(monkeypatch):
    monkeypatch.setattr(m.HTTP, "get", fake_get(text=(
        '<html><head><meta property="og:title" content="Staff ML Engineer/Scientist">'
        '<title>Precision</title></head><body><h1>x</h1></body></html>')))
    j = m.Job("28520", "Job 28520", "https://careers.kula.ai/precision-neuroscience/28520")
    m.fetch_detail_title(j)
    assert j.title == "Staff ML Engineer/Scientist"


def test_same_job_under_two_companies_listed_once(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
companies:
  - {name: Meta, source: greenhouse, slug: meta}
  - {name: Meta Reality Labs, source: greenhouse, slug: rl}
""")
    monkeypatch.setattr(m, "CONFIG_PATH", cfg)
    monkeypatch.setattr(m, "STATE_PATH", tmp_path / "s.json")
    jobs = {"meta": [{"id": 1, "title": "A", "absolute_url": "u1"}],
            "rl": [{"id": 1, "title": "A", "absolute_url": "u1"}]}
    monkeypatch.setattr(m.HTTP, "get",
                        lambda url, **kw: FakeResp({"jobs": jobs[url.split("/")[-2]]}))
    sent = []
    monkeypatch.setattr(m, "send_email", lambda s, h, t: sent.append(t) or True)
    args = type("A", (), {"only": None, "dry_run": False, "check": False, "verbose": False})()
    m.run(args)
    jobs["meta"].append({"id": 2, "title": "Research Intern", "absolute_url": "u2"})
    jobs["rl"].append({"id": 2, "title": "Research Intern", "absolute_url": "u2"})
    m.run(args)
    assert "== Meta Reality Labs (1) ==" in sent[0] and "== Meta (" not in sent[0]


def test_location_report(monkeypatch, capsys):
    monkeypatch.setattr(m, "FETCHERS", {"greenhouse": lambda c: [
        m.Job("1", "a", "u", "Mountain View, CA, USA"), m.Job("2", "b", "u", "Zurich, Switzerland"),
        m.Job("3", "c", "u", "Bengaluru, India"), m.Job("4", "d", "u", ""),
        m.Job("5", "e", "u", "Lagos, Nigeria")]})
    m.location_report({"companies": [{"name": "X", "source": "greenhouse"}]})
    out = capsys.readouterr().out
    for word in ("us", "switzerland", "india", "(location not listed)", "Lagos, Nigeria"):
        assert word in out


# ── fixes from the first --locations survey ─────────────────────
@pytest.mark.parametrize("loc", [
    "(opens in new tab) San Diego, CA →",            # Synchron link text
    "Menlo Park, CA ⋅ Redmond, WA",                  # Meta separator
    "Sunnyvale, CA · Remote, US",
])
def test_noisy_us_locations_now_match(loc):
    assert m.location_ok(loc, ["us"])


def test_normalize_location():
    assert m.normalize_location("(opens in new tab) San Diego, CA →") == "San Diego, CA"
    assert m.normalize_location("Menlo Park, CA ⋅ Redmond, WA") == "Menlo Park, CA | Redmond, WA"


def test_link_segments_drop_screen_reader_text():
    page = ('<a href="https://workforcenow.adp.com/x?jobId=92016_1"><span>Pre-Clinical Coordinator</span>'
            '<span>(opens in new tab)</span><span>San Diego, CA</span><span>→</span></a>')
    [j] = m.extract_links(page, "https://synchron.com/careers",
                          r"workforcenow\.adp\.com/.*[?&]jobId=(?P<id>[\d_]+)")
    assert (j.title, j.location) == ("Pre-Clinical Coordinator", "San Diego, CA")
