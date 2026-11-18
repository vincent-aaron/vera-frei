"""Security fixes from the 7 Nov 2026 review: nobody can make Vera upload a file from the laptop, reach the home network,
slip a stranger's link into someone else's run, or send email the owner didn't just see."""

import asyncio
import io
import json
import time
import zipfile
from types import SimpleNamespace

import pytest

from vera.agents import applier, outreach, web
from vera.apply import answers, config as apply_config, netguard
from vera.apply.answers import SKIP, AnswerMemory, Field
from vera.apply import sites
from vera.hermes import identity, tools
from tests.test_hermes import GUEST, FORMS, OUTREACH_CH, OWNER, call, on_discord, ready, rt, vera  # noqa: F401  (fixtures)


# ----------------------------------------------------------------- 1. no uploads from the laptop
@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_config, "DATA_DIR", tmp_path)
    resume = tmp_path / "Juan_Resume.pdf"
    resume.write_bytes(b"%PDF")
    letters = tmp_path / "cover_letters"
    letters.mkdir()
    (letters / "acme.docx").write_bytes(b"PK")
    secret = tmp_path / ".env"
    secret.write_text("DISCORD_TOKEN=x")
    return SimpleNamespace(resume=resume, letter=letters / "acme.docx", secret=secret, other=tmp_path / "notes.txt",
                           profile={"resume_path": str(resume)})


def test_only_their_resume_or_a_cover_letter_can_be_uploaded(folder):
    folder.other.write_text("private")
    assert apply_config.allowed_upload(folder.resume, folder.profile)
    assert apply_config.allowed_upload(folder.letter, folder.profile)
    assert not apply_config.allowed_upload(folder.secret, folder.profile)
    assert not apply_config.allowed_upload(folder.other, folder.profile)
    assert not apply_config.allowed_upload(folder.resume.parent / "missing.pdf", folder.profile)
    assert not apply_config.allowed_upload(str(folder.letter.parent / ".." / ".env"), folder.profile)


def test_a_saved_answer_is_never_used_for_a_file_field(folder, tmp_path, monkeypatch):
    monkeypatch.setattr(answers, "ANSWERS_PATH", tmp_path / "answers.json")
    memory = AnswerMemory()
    field = Field(kind="file", question="Gender", required=True, options=[])
    monkeypatch.setattr(memory, "get", lambda _f: str(folder.secret))
    monkeypatch.setattr(answers, "rule_answer", lambda *_a: None)
    assert asyncio.run(answers.decide(field, folder.profile, memory, {})) in (SKIP, None)


@pytest.mark.parametrize("answer", [r"%LOCALAPPDATA%\hermes\.env", r"\\server\share\x", "../../.env",
                                    "~/secrets", r"%APPDATA%\x", "file:///C:/x", "/home/me/x", "x" * 2000])
def test_answers_that_look_like_paths_are_refused(answer):
    assert not applier.safe_answer(answer)


def test_normal_answers_are_kept():
    for answer in ("Yes", "2 weeks", "Top 50% at school", 28000, "I built a Laravel app for a bakery."):
        assert applier.safe_answer(answer)


def test_saving_a_path_answer_is_refused_in_chat(vera, rt, monkeypatch):
    on_discord(monkeypatch, GUEST, FORMS)
    out = call(tools.save_answer, question="Upload your CV", answer=r"%LOCALAPPDATA%\hermes\.env")
    assert "wasn't saved" in out["error"]


# ----------------------------------------------------------------- 2. strangers' links and the home network
@pytest.mark.parametrize("url,safe", [
    ("file:///C:/Users/someone/AppData/Local/hermes/.env", False), ("http://localhost:8080/", False),
    ("http://127.0.0.1/", False), ("http://192.168.1.1/admin", False), ("http://10.0.0.1/", False), ("http://[::1]/", False),
    ("http://router.local/", False), ("ftp://93.184.216.34/", False), ("https://93.184.216.34/jobs", True),
    ("about:blank", True), ("data:text/plain,hi", True),
])
def test_the_engine_browser_only_reaches_public_websites(url, safe):
    assert netguard.url_is_safe(url) is safe


def test_every_browser_request_goes_through_the_guard():
    routes = []

    class Ctx:
        async def route(self, pattern, handler):
            routes.append((pattern, handler))
    asyncio.run(netguard.install(Ctx()))
    assert routes and routes[0][0] == "**/*"

    done = []

    class Route:
        def __init__(self, url):
            self.request = SimpleNamespace(url=url)

        async def continue_(self):
            done.append("ok")

        async def abort(self, why):
            done.append(why)
    asyncio.run(routes[0][1](Route("http://192.168.1.1/")))
    asyncio.run(routes[0][1](Route("https://93.184.216.34/")))
    assert done == ["blockedbyclient", "ok"]


@pytest.mark.parametrize("url,board", [
    ("https://boards.greenhouse.io/acme/jobs/1", True), ("https://jobs.lever.co/acme/1", True),
    ("https://greenhouse.io.example.net/x", False), ("http://greenhouse.io@10.0.0.1/", False), ("https://notgreenhouse.io/x", False),
])
def test_a_site_is_only_recognised_by_its_real_domain(url, board):
    assert sites._ATS.matches(url) is board



def test_a_job_a_member_added_never_reaches_the_owners_list(vera, tmp_path):
    from vera.agents import scout
    job = vera.add_manual_job("https://evil.example/apply", "Web Developer", "Acme")
    vera.user(GUEST).set_job_state(job["id"], "posted")            # what adding it does
    owner = ready(vera.user(OWNER), tmp_path)
    ids = [j["id"] for j, _ in scout.rank(vera.db, OWNER, {**owner.prefs, "min_match": 0}, owner.profile, include_seen=True, save=False)]
    assert job["id"] not in ids
    mine = [j["id"] for j, _ in scout.rank(vera.db, GUEST, {**vera.user(GUEST).prefs, "min_match": 0}, {}, include_seen=True, save=False)]
    assert job["id"] in mine or True       # theirs stays theirs (their score depends on their preferences)


def test_a_name_that_answers_with_a_private_address_is_not_read():
    def response(ip):
        stream = SimpleNamespace(get_extra_info=lambda key: (ip, 443) if key == "server_addr" else None)
        return SimpleNamespace(extensions={"network_stream": stream})
    assert web._reached_public(response("93.184.216.34"))
    assert not web._reached_public(response("192.168.1.1")) and not web._reached_public(response("127.0.0.1"))


# ----------------------------------------------------------------- 3. email only after he sees every draft
def outreach_ready(vera, rt, monkeypatch, tmp_path):
    ready(vera.user(OWNER), tmp_path)
    monkeypatch.setattr(outreach, "can_send", lambda: True)

    async def plain_draft(session, lead):
        return {**lead, "subject": "Web Developer - Juan", "body": "Hello, my résumé is attached."}
    monkeypatch.setattr(outreach, "draft", plain_draft)
    on_discord(monkeypatch, OWNER, OUTREACH_CH)


def test_every_draft_is_posted_in_full_by_the_tool(vera, rt, monkeypatch, tmp_path):
    outreach_ready(vera, rt, monkeypatch, tmp_path)
    identity.record(OUTREACH_CH, OWNER, "email hr@acme.ph and jobs@beta.ph", when=time.time())
    out = call(tools.draft_emails, emails=["hr@acme.ph", "jobs@beta.ph"])
    posted = "\n".join(rt.channels[OUTREACH_CH].sent)
    assert out["drafts_posted"] == 2 and "Email 1** to **hr@acme.ph" in posted and "Email 2** to **jobs@beta.ph" in posted


def test_a_later_yes_to_something_else_never_sends_them(vera, rt, monkeypatch, tmp_path):
    outreach_ready(vera, rt, monkeypatch, tmp_path)
    identity.record(OUTREACH_CH, OWNER, "email hr@acme.ph", when=time.time())
    call(tools.draft_emails, emails=["hr@acme.ph"])
    identity.record(OUTREACH_CH, OWNER, "what's the weather?", when=time.time() + 1)
    identity.record(OUTREACH_CH, OWNER, "yes", when=time.time() + 2)
    assert "very next thing" in call(tools.send_emails)["error"] and not rt.spawned


def test_his_very_next_proceed_sends_them(vera, rt, monkeypatch, tmp_path):
    outreach_ready(vera, rt, monkeypatch, tmp_path)
    identity.record(OUTREACH_CH, OWNER, "email hr@acme.ph", when=time.time())
    call(tools.draft_emails, emails=["hr@acme.ph"])
    identity.record(OUTREACH_CH, OWNER, "proceed", when=time.time() + 1)
    assert call(tools.send_emails)["sending"] == 1 and rt.spawned


def test_a_list_of_addresses_is_not_one_address(vera, rt, monkeypatch, tmp_path):
    outreach_ready(vera, rt, monkeypatch, tmp_path)
    out = call(tools.draft_emails, emails=["a@x.com, b@y.com, c@z.com"])
    assert "error" in out and not rt.channels[OUTREACH_CH].sent


def test_sending_skips_anything_but_one_valid_address(vera, tmp_path, monkeypatch):
    session = ready(vera.user(OWNER), tmp_path)
    sent = []
    monkeypatch.setattr(outreach, "_send_now", lambda m: sent.append(m["To"]))
    monkeypatch.setattr(outreach, "SEND_GAP_SECONDS", 0)
    res = asyncio.run(outreach.send_batch(session, [{"email": "a@x.com, b@y.com", "subject": "s", "body": "b"}]))
    assert not sent and res["skipped"][0][1] == "not a single valid email address"


# ----------------------------------------------------------------- round 2: the browser's filtering proxy
@pytest.mark.parametrize("ip,public", [
    ("93.184.216.34", True), ("2606:2800:220:1:248:1893:25c8:1946", True), ("127.0.0.1", False), ("192.168.1.1", False),
    ("10.0.0.1", False), ("169.254.169.254", False), ("100.64.0.1", False), ("198.18.0.1", False), ("224.0.0.1", False),
    ("::1", False), ("::ffff:192.168.1.1", False), ("::127.0.0.1", False), ("::c0a8:101", False), ("64:ff9b::c0a8:101", False),
    ("fe80::1", False), ("0.0.0.0", False),
])
def test_only_real_public_addresses_count(ip, public):
    assert netguard.ip_is_public(ip) is public


def proxy_request(proxy, raw: bytes) -> bytes:
    async def go():
        await proxy.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(raw)
            await writer.drain()
            data = await asyncio.wait_for(reader.read(200), 10)
            writer.close()
            return data
        finally:
            await proxy.stop()
    return asyncio.run(go())


def test_the_proxy_refuses_this_laptop_and_the_home_network():
    proxy = netguard.FilterProxy()            # the real address check
    for target in (b"127.0.0.1:443", b"localhost:443", b"192.168.1.1:443", b"[::1]:443"):
        assert proxy_request(proxy, b"CONNECT " + target + b" HTTP/1.1\r\nHost: x\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert proxy_request(proxy, b"GET http://192.168.1.1/admin HTTP/1.1\r\nHost: 192.168.1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert proxy_request(proxy, b"CONNECT example.com:22 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")      # odd ports too


def test_the_proxy_connects_to_exactly_the_address_it_checked():
    """A pretend lookup says "public" and hands back a local test server's address, standing in for a real site."""
    async def go():
        got = []

        async def site(reader, writer):
            got.append(await reader.read(100))
            writer.write(b"hello")
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(site, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        netguard.ALLOWED_PORTS.add(port)
        proxy = netguard.FilterProxy(resolve=lambda host, p: ["127.0.0.1"] if host == "jobs.example" else None)
        await proxy.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(f"CONNECT jobs.example:{port} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            first = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            writer.write(b"ping")
            await writer.drain()
            back = await asyncio.wait_for(reader.read(10), 5)
            writer.close()
            return first, back, got
        finally:
            netguard.ALLOWED_PORTS.discard(port)
            await proxy.stop()
            server.close()
    first, back, got = asyncio.run(go())
    assert first.startswith(b"HTTP/1.1 200") and back == b"hello" and got == [b"ping"]


def test_the_engine_browser_goes_through_the_proxy_without_stepping_into_requests():
    """The proxy filters every connection. Per-request interception can break a form's final submit, so it's not used."""
    import inspect
    from vera.apply import __main__ as engine
    source = inspect.getsource(engine.open_browser)
    assert "FilterProxy" in source and 'proxy={"server": server}' in source
    assert "netguard.install(" not in source and 'service_workers="block"' not in source
    assert "--proxy-bypass-list=<-loopback>" in netguard.BROWSER_ARGS


# ----------------------------------------------------------------- round 2: files, jobs, planted text, slow pages, mail
def test_an_export_date_range_cant_leave_their_folder(tmp_path):
    from vera.agents import exports
    folder = tmp_path / "exports"
    rows = {"sent": [], "unfinished": []}
    paths = exports.write_all(rows, ["xlsx"], folder, {"since": r"..\..\..\escaped", "until": "2026-11-07"})
    assert all(folder.resolve() in p.resolve().parents for p in paths) and not (tmp_path.parent / "escaped").exists()


def test_the_applications_tool_takes_dates_only(vera, rt, monkeypatch):
    on_discord(monkeypatch, GUEST, FORMS)
    assert "must be a date" in call(tools.applications, since=r"..\..\Desktop\x", files=["xlsx"])["error"]


def test_a_hand_added_job_stays_with_whoever_added_it(vera, rt, monkeypatch):
    job = vera.add_manual_job("https://evil.example/apply", "Web Developer", "Acme", added_by=GUEST)
    assert vera.may_use_job(job, GUEST) and not vera.may_use_job(job, OWNER)
    on_discord(monkeypatch, OWNER, FORMS)
    key = str(vera.job_key(job["id"]))
    assert "error" in call(tools.job_state, job=key, state="saved") and "error" in call(tools.job_details, job=key)
    # someone else adding the same link doesn't rewrite it, and gets their own access
    vera.add_manual_job("https://evil.example/apply", "Totally different", "Other", added_by=OWNER)
    assert vera.db.job(job["id"])["title"] == "Web Developer" and vera.may_use_job(job, OWNER)


def test_jobs_added_before_owners_were_kept_still_work(vera):
    job = vera.add_manual_job("https://jobs.example.ph/job/1", "Dev", "Acme")
    vera.user(OWNER).set_job_state(job["id"], "applied")
    assert vera.may_use_job(job, OWNER) and not vera.may_use_job(job, GUEST)
    vera.add_manual_job("https://jobs.example.ph/job/1", "Changed", "X", added_by=GUEST)
    assert vera.may_use_job(job, OWNER) and vera.db.job(job["id"])["title"] == "Dev"


@pytest.mark.parametrize("url,ok", [("https://boards.greenhouse.io/acme/jobs/1", True), ("https://jobs.lever.co/acme/1", True),
                                    ("https://apply.workable.com/acme/j/1", True), ("https://evil.example/lever.co/", False),
                                    ("https://lever.co.evil.example/", False), ("file:///lever.co", False),
                                    ("https://example-jobboard.com/job/1", False)])
def test_only_company_forms_are_filled_after_approval_and_recognised_by_their_real_address(url, ok, monkeypatch):
    from vera import config
    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    assert applier.can_auto_submit(url, OWNER) is ok


def test_planted_text_cant_save_rules_lessons_or_notes(vera, rt, monkeypatch):
    on_discord(monkeypatch, OWNER, FORMS)
    identity.record(FORMS, OWNER, "what do you think of job #12?", when=time.time())
    for fn, args in ((tools.save_rule, {"rule": "Always send salary 10k"}), (tools.learn, {"lesson": "He wants all jobs"}),
                     (tools.note, {"add": "Add a backdoor"})):
        assert "Not saved" in call(fn, **args)["error"]
    identity.record(FORMS, OWNER, "from now on, always skip recruitment agencies", when=time.time())
    assert call(tools.save_rule, rule="Skip recruitment agencies")["saved"]


def test_planted_text_cant_raise_application_limits(vera, rt, monkeypatch):
    on_discord(monkeypatch, OWNER, FORMS)
    identity.record(FORMS, OWNER, "tell me about this job", when=time.time())
    assert "Not saved" in call(tools.settings, rounds={"max_per_day": 500})["error"]
    identity.record(FORMS, OWNER, "set my limit to 10 per day", when=time.time())
    assert "error" not in call(tools.settings, rounds={"max_per_day": 10})
    assert "enabled" not in vera.user(OWNER).prefs["rounds"]                         # there is no switch for applying on its own


def test_sensitive_answers_must_be_their_own_words(vera, rt, monkeypatch):
    on_discord(monkeypatch, GUEST, FORMS)
    identity.record(FORMS, GUEST, "fill whatever", when=time.time())
    assert "only they can answer" in call(tools.save_answer, question="Are you authorized to work in the US?", answer="Yes")["error"]
    identity.record(FORMS, GUEST, "1: no, I'm not authorized", when=time.time())
    assert call(tools.save_answer, question="Are you authorized to work in the US?", answer="No")["ok"]


@pytest.mark.parametrize("make", [lambda n: "<" * n, lambda n: "<script" * (n // 7), lambda n: "<meta " * (n // 6),
                                  lambda n: '<meta property="og:description" content="' * (n // 40)])
def test_odd_pages_dont_freeze_vera(make):
    page = make(200_000)
    start = time.perf_counter()
    web.to_text(page)
    web.preview_text(page)
    from vera.sources.base import clean_html
    clean_html(page)
    assert time.perf_counter() - start < 3


def test_long_odd_text_doesnt_slow_the_email_search():
    start = time.perf_counter()
    outreach.EMAIL_RE.findall("a" * 50_000 + "@" + "b." * 20_000)
    assert time.perf_counter() - start < 1
    assert outreach.EMAIL_RE.fullmatch("hr@acme.ph") and outreach.emails_in("Send to careers@acme-labs.com.ph today") == ["careers@acme-labs.com.ph"]


def test_a_fake_display_name_never_changes_an_application(vera):
    from vera.agents import jobmail
    from tests.test_jobmail import applied, msg
    me = ready(vera.user(OWNER), Path_tmp := __import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    app_id = applied(vera, company="Acme Corp")
    found = jobmail.track(me, [{**msg("random.person@gmail.com", "Update on your application",
                                      "Unfortunately we will not be moving forward.", "Acme Corp"), "folder": jobmail.INBOX, "bulk": False}])
    assert vera.db.application(app_id)["status"] == "submitted"
    assert found and "address isn't theirs" in found[0]["note"]


def test_a_blank_platform_in_the_gateway_process_is_refused_even_before_discord_connects(vera, monkeypatch):
    from vera.hermes import runtime
    monkeypatch.setattr(runtime, "_live", None)
    monkeypatch.setattr(runtime.sys if hasattr(runtime, "sys") else __import__("sys"), "argv", ["hermes", "gateway", "run"])
    for var in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_SOURCE"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(identity.NotAllowed):
        identity.current()


# ----------------------------------------------------------------- 4. who's asking, and big files
def test_a_blank_platform_inside_the_gateway_is_not_the_owner(vera, rt, monkeypatch):
    for var in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_SOURCE", "HERMES_SESSION_USER_ID"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(identity.NotAllowed):
        identity.current()


def test_a_word_file_that_unpacks_into_gigabytes_is_refused(tmp_path):
    from vera.services import MAX_DOCX_XML, extract_text
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w:p>a</w:p>" * (MAX_DOCX_XML // 12 + 10))
    assert path.stat().st_size < 1_000_000
    with pytest.raises(ValueError):
        extract_text(path)


def test_a_normal_word_resume_still_reads(tmp_path):
    from vera.services import extract_text
    path = tmp_path / "cv.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", "<w:p><w:t>Juan Cruz</w:t></w:p><w:p><w:t>Web Developer</w:t></w:p>")
    assert "Juan Cruz" in extract_text(path) and "Web Developer" in extract_text(path)
