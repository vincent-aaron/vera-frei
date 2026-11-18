"""New free sources and the 'open to me' filter. No network."""

import asyncio

import httpx
import pytest

from vera.apply.filters import rejects
from vera.sources.extra import CompanyForms, HNHiring, _lists, parse_post, title_fits


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_company_lists_return_remote_jobs_that_fit():
    _lists.clear()

    def handler(request):
        if "greenhouse" in request.url.host and "gitlab" in request.url.path:
            return httpx.Response(200, json={"jobs": [
                {"id": 1, "title": "Senior Web Developer", "location": {"name": "Remote, Worldwide"}, "absolute_url": "https://job-boards.greenhouse.io/gitlab/jobs/1", "content": "x", "updated_at": "2026-11-01"},
                {"id": 2, "title": "Web Developer", "location": {"name": "Berlin"}, "absolute_url": "https://x/2", "content": "x", "updated_at": "2026-11-01"},
                {"id": 3, "title": "Accountant", "location": {"name": "Remote"}, "absolute_url": "https://x/3", "content": "x", "updated_at": "2026-11-01"}]})
        if "ashby" in request.url.host and "ramp" in request.url.path:
            return httpx.Response(200, json={"jobs": [{"id": "a", "title": "Web Developer", "isRemote": True, "location": "Philippines", "jobUrl": "https://jobs.ashbyhq.com/ramp/a", "descriptionPlain": "y", "publishedAt": "2026-11-02"}]})
        return httpx.Response(404)

    async def go():
        async with client(handler) as c:
            return await CompanyForms().fetch(c, "Web Developer", "Philippines")
    jobs = asyncio.run(go())
    assert sorted(j["url"] for j in jobs) == ["https://job-boards.greenhouse.io/gitlab/jobs/1", "https://jobs.ashbyhq.com/ramp/a"]
    assert jobs[0]["source"] == "companyforms"


def test_one_company_down_doesnt_stop_the_rest():
    _lists.clear()

    def handler(request):
        raise httpx.ConnectError("down")

    async def go():
        async with client(handler) as c:
            return await CompanyForms().fetch(c, "Web Developer")
    assert asyncio.run(go()) == []


@pytest.mark.parametrize("text,company,title", [
    ("Simple Analytics | DevOps Engineer | REMOTE (worldwide) | Full-time", "Simple Analytics", "DevOps Engineer"),
    ("Acme | https://acme.dev | Full Stack Developer | Remote", "Acme", "Full Stack Developer"),
])
def test_hiring_posts_are_understood(text, company, title):
    p = parse_post(text)
    assert p["company"] == company and p["title"] == title


def test_chat_posts_without_a_role_are_ignored():
    assert parse_post("Thanks for posting this, very helpful!") is None
    assert parse_post("Acme | Sales Manager | Remote") is None


def test_hn_links_prefer_a_form_she_can_fill():
    def handler(request):
        if "search_by_date" in str(request.url):
            return httpx.Response(200, json={"hits": [{"title": "Ask HN: Who is hiring? (November 2026)", "objectID": "9"}]})
        return httpx.Response(200, json={"children": [
            {"id": 1, "created_at": "2026-11-01", "text": 'Acme | Web Developer | REMOTE<p>Apply: <a href="https://jobs.lever.co/acme/123">here</a>'},
            {"id": 2, "created_at": "2026-11-01", "text": "Beta | Web Developer | REMOTE<p>Email hiring@beta.dev"},
            {"id": 3, "created_at": "2026-11-01", "text": "Gamma | Web Developer | Onsite NYC"}]})

    async def go():
        async with client(handler) as c:
            return await HNHiring().fetch(c, "Web Developer")
    jobs = asyncio.run(go())
    assert [j["url"] for j in jobs] == ["https://jobs.lever.co/acme/123", "https://news.ycombinator.com/item?id=2"]
    assert "hiring@beta.dev" in jobs[1]["description"]         # available for outreach


def test_title_fits_ignores_levels():
    assert title_fits("Web Developer", ["web", "developer"]) and not title_fits("Web Designer", ["web", "developer"])


SPEC = {"work_setup": ["Remote", "Hybrid", "On-site"], "home_country": "Philippines"}


@pytest.mark.parametrize("location", ["Remote, United States", "Remote - US only", "Remote (Canada)", "Remote, EMEA", "Remote - Germany", "Remote US", "REMOTE (US)", "Remote - US", "REMOTE (US, East Coast hours)"])
def test_remote_jobs_for_other_countries_are_skipped(location):
    assert "only for" in rejects({"title": "Web Developer", "location": location}, SPEC)


@pytest.mark.parametrize("location", ["Remote", "Remote, Worldwide", "Remote - Philippines", "Remote APAC", "Anywhere (Remote)", "Remote, Asia"])
def test_remote_jobs_open_to_you_pass(location):
    assert rejects({"title": "Web Developer", "location": location}, SPEC) is None


def test_remote_with_a_city_means_that_area_on_overseas_sites():
    overseas = "https://jobs.ashbyhq.com/acme/1"
    for place in ("Remote Toronto", "Remote Foster City, CA"):
        assert "only for" in rejects({"title": "Web Developer", "location": place, "url": overseas}, SPEC), place
    for place in ("Remote", "Remote, Global", "Home based - Worldwide", "Remote - Philippines"):
        assert rejects({"title": "Web Developer", "location": place, "url": overseas}, SPEC) is None, place
    # On a Philippine site, a remote job listing a city in the Philippines is fine.
    assert rejects({"title": "Web Developer (Remote)", "location": "Makati City, Metro Manila", "url": "https://jobs.example.ph/job/1"}, SPEC) is None


NEAR = {"work_setup": ["Remote", "Hybrid", "On-site"], "home_country": "Philippines", "places": ["Manila", "Metro Manila", "Bulacan", "Pampanga", "Bataan"]}


@pytest.mark.parametrize("location", [
    "Makati City, Metro Manila", "Makati", "Mandaluyong City", "Taguig", "BGC, Taguig City", "Bonifacio Global City", "Fort Bonifacio", "Rizal Drive, BGC, Taguig",
    "Pasig", "Ortigas Center", "Quezon City", "Las Piñas", "Las Pinas City", "NCR", "Manila", "Malolos, Bulacan", "Hagonoy", "City of San Fernando, Pampanga",
    "Angeles City", "Clark Freeport Zone", "Balanga, Bataan", "Mariveles", "Rizal Avenue, Manila",
])
def test_on_site_jobs_near_you_are_kept(location):
    assert rejects({"title": "Web Developer", "location": location, "url": "https://jobs.example.ph/job/1"}, NEAR) is None, location


@pytest.mark.parametrize("location", [
    "Cebu City", "Davao City", "Dasmariñas, Cavite", "Sta. Rosa, Laguna", "Antipolo, Rizal", "Morong, Rizal", "San Fernando, La Union", "Baguio", "Tarlac City", "Iloilo",
    "Makati or Cebu", "Makati City / Cavite", "Los Angeles", "Singapore", "Clark County, USA",
])
def test_on_site_jobs_too_far_are_skipped(location):
    assert "outside your locations" in rejects({"title": "Web Developer", "location": location, "url": "https://jobs.example.ph/job/1"}, NEAR), location


@pytest.mark.parametrize("location", ["Philippines", "Nationwide", "Luzon", "Multiple locations"])
def test_a_location_that_could_be_anywhere_is_skipped(location):
    assert "outside your locations" in rejects({"title": "Web Developer", "location": location}, NEAR)


def test_no_location_at_all_is_skipped_for_on_site_jobs():
    assert "no location listed" in rejects({"title": "Web Developer", "location": ""}, NEAR)


@pytest.mark.parametrize("description", [
    "You will work with our remote team and clients.", "We support remote customers across Asia.", "This is not a remote role. Work from our Cebu office.",
    "No remote work is allowed.", "Remote access tools experience is a plus.", "This is a remote position, but you must report to the office in Cebu weekly.",
])
def test_the_word_remote_alone_doesnt_skip_the_location_check(description):
    job = {"title": "Web Developer", "location": "Cebu City", "description": description, "url": "https://jobs.example.ph/job/1"}
    assert "outside your locations" in rejects(job, NEAR)


@pytest.mark.parametrize("description", [
    "This is a fully remote position.", "100% remote, work anywhere in the Philippines.", "You will work from home.", "Work setup: Remote", "This is a remote role.", "Permanent WFH.",
])
def test_a_clearly_remote_job_can_list_any_city(description):
    job = {"title": "Web Developer", "location": "Cebu City", "description": description, "url": "https://jobs.example.ph/job/1"}
    assert rejects(job, NEAR) is None


@pytest.mark.parametrize("title,location", [("Web Developer (Remote)", "Cebu City"), ("Web Developer", "Cebu City (Work From Home)"), ("WFH Web Developer", "Davao")])
def test_remote_in_the_title_or_location_still_counts(title, location):
    assert rejects({"title": title, "location": location, "url": "https://jobs.example.ph/job/1"}, NEAR) is None


def test_hybrid_jobs_have_to_be_near_you_too():
    far = {"title": "Web Developer", "location": "Cebu City", "description": "Hybrid setup, 3 days in the office.", "url": "https://jobs.example.ph/job/1"}
    near = {**far, "location": "BGC, Taguig"}
    assert "outside your locations" in rejects(far, NEAR) and rejects(near, NEAR) is None


def test_only_remote_means_a_job_that_merely_mentions_remote_is_out():
    spec = {**NEAR, "work_setup": ["Remote"]}
    assert "work setup looks On-site" in rejects({"title": "Web Developer", "location": "Makati", "description": "Work with our remote team."}, spec)


def test_someone_elses_place_works_too():
    spec = {**NEAR, "places": ["Dagupan"]}
    assert rejects({"title": "Web Developer", "location": "Dagupan City"}, spec) is None


AVOID = {**NEAR, "avoid_onsite": ["Muntinlupa", "Alabang", "Filinvest", "Las Piñas", "Parañaque"]}


@pytest.mark.parametrize("location", ["Muntinlupa City, Metro Manila", "Alabang, Muntinlupa", "Filinvest, Alabang", "Las Piñas City, Metro Manila", "Las Pinas", "Parañaque", "Paranaque City, Metro Manila"])
def test_places_too_far_for_on_site_are_skipped_even_in_metro_manila(location):
    job = {"title": "Web Developer", "location": location, "url": "https://jobs.example.ph/job/1"}
    assert "too far" in rejects(job, AVOID)
    assert "too far" in rejects({**job, "description": "Hybrid setup, 3 days in the office."}, AVOID)


@pytest.mark.parametrize("job", [
    {"location": "Alabang, Muntinlupa (Remote)"}, {"location": "Las Piñas City", "description": "This is a fully remote position."},
    {"location": "Davao City (Remote)"}, {"location": "Remote - Philippines"}, {"location": "Remote, Worldwide"},
])
def test_remote_jobs_can_still_be_anywhere(job):
    assert rejects({"title": "Web Developer", "url": "https://jobs.example.ph/job/1", **job}, AVOID) is None


@pytest.mark.parametrize("location", ["Makati City, Metro Manila", "BGC, Taguig", "Mandaluyong City", "Quezon City, Metro Manila", "Pasay City, Metro Manila"])
def test_the_rest_of_metro_manila_is_still_fine(location):
    assert rejects({"title": "Web Developer", "location": location, "url": "https://jobs.example.ph/job/1"}, AVOID) is None


FEED = """<rss><channel><item><title><![CDATA[Web Developer at Acme & Co]]></title><link>https://jobspresso.co/job/web-dev/</link>
<pubDate>Wed, 04 Nov 2026 10:00:00 +0000</pubDate><description>Work from anywhere R&D team</description></item>
<item><title>Sales Manager</title><link>https://jobspresso.co/job/sales/</link><description>x</description></item></channel></rss>"""


def test_new_feeds_read_even_slightly_broken_xml():
    from vera.sources.feeds import Jobspresso

    async def go():
        async with client(lambda _r: httpx.Response(200, text=FEED)) as c:
            return await Jobspresso().fetch(c, "Web Developer")
    jobs = asyncio.run(go())
    assert len(jobs) == 1 and jobs[0]["title"] == "Web Developer" and jobs[0]["company"] == "Acme & Co" and jobs[0]["url"].endswith("/web-dev/") and jobs[0]["remote"] == 1 and jobs[0]["posted_at"]


def test_new_boards_are_everywhere_they_need_to_be():
    from vera import preferences
    from vera.agents.rounds import REMOTE_SOURCES
    from vera.apply.sites import RemoteBoard
    from vera.sources import LABELS, SOURCES
    for name, domain in (("jobspresso", "jobspresso.co"), ("realworkfromanywhere", "realworkfromanywhere.com"), ("larajobs", "larajobs.com")):
        assert name in SOURCES and name in LABELS and name in REMOTE_SOURCES and name in preferences.defaults()["sources"] and domain in RemoteBoard.domains


def test_a_feed_thats_down_doesnt_stop_the_others():
    from vera.sources.feeds import LaraJobs

    async def go():
        async with client(lambda _r: httpx.Response(500)) as c:
            return await LaraJobs().fetch(c, "Web Developer")
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(go())
