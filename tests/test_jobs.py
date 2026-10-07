import jobs


def test_slug_from_url_beats_guessing():
    """Где есть ссылка, угадывать нечего — и ошибиться нельзя."""
    assert jobs.from_url("https://jobs.ashbyhq.com/constructor") == ("ashby", "constructor")
    assert jobs.from_url("https://job-boards.greenhouse.io/nebius") == ("greenhouse", "nebius")
    assert jobs.from_url("https://jobs.lever.co/appfollow") == ("lever", "appfollow")
    assert jobs.from_url("https://praktika.teamtailor.com/jobs") == ("teamtailor", "praktika")
    assert jobs.from_url("https://vivid.jobs.personio.de/?language=en") == ("personio", "vivid")
    assert jobs.from_url("https://job-boards.eu.greenhouse.io/growe") == ("greenhouse", "growe")
    assert jobs.from_url("https://jobs.smartrecruiters.com/CDPROJEKTRED") == ("smartrecruiters", "CDPROJEKTRED")
    assert jobs.from_url("https://elixi.com/careers") is None


def test_slug_variants_skip_short_first_word():
    """«ABC Fitness» под slug abc вёл на чужую доску ThoughtWorks."""
    assert jobs.slug_variants("Salmon Group") == ["salmongroup", "salmon-group", "salmon"]
    assert jobs.slug_variants("ABC Fitness") == ["abcfitness", "abc-fitness"]
    assert jobs.slug_variants("Plata") == ["plata"]
    assert jobs.slug_variants("---") == []


def test_same_company_tolerates_punctuation_and_suffix():
    assert jobs.same_company("ASOS.com", "ASOS")
    assert jobs.same_company("Salmon", "Salmon Group")
    assert not jobs.same_company("ABC Fitness", "ThoughtWorks_new")
    assert not jobs.same_company("", "Acme")


def test_posted_date_normalised_from_every_shape():
    assert jobs.posted("2026-09-21T08:00:59.084+00:00") == "2026-09-21"
    assert jobs.posted(1788965250140) == "2026-09-09"      # Lever, миллисекунды
    assert jobs.posted("2026-09-15 16:47:19 UTC") == "2026-09-15"   # Recruitee
    assert jobs.posted(None) is None and jobs.posted("") is None


def test_embedded_payload_parsed():
    """top.co держит вакансии в payload Next.js, а не в API."""
    html = (r'\"id\":\"9699\",\"position\":\"Director of Risk\",\"location\":\"x\",'
            r'\"company\":{\"name\":\"Wallet\"}')
    assert jobs.JOB_IN_PAYLOAD.findall(html) == [("9699", "Director of Risk", "Wallet")]


def test_every_adapter_is_registered():
    for name in ("ashby", "greenhouse", "lever", "smartrecruiters", "workable",
                 "recruitee", "teamtailor", "pinpoint"):
        assert callable(jobs.ADAPTERS[name])


def test_failed_board_is_not_an_empty_board(monkeypatch):
    """Доска не ответила — пишем настоящую причину и НЕ закрываем вакансии.
    Раньше None схлопывался в [], и 429 попадал в отчёт как «пустой список»."""
    import jobs, storage
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO companies(name,page_url,ats,slug) "
                 "VALUES('Zeta','https://zeta.test','workable','zeta')")
    conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,source,first_seen) "
                 "VALUES('https://zeta.test/j1','Zeta','Backend','workable','2026-09-01')")
    conn.commit()

    monkeypatch.setitem(jobs.ADAPTERS, "workable", lambda s: None)
    monkeypatch.setattr(jobs, "diagnose", lambda *a: "HTTP 429")
    jobs.run(only="Zeta")

    conn = storage.connect()
    assert conn.execute("SELECT last_error FROM companies WHERE name='Zeta'").fetchone()[0] == "HTTP 429"
    assert conn.execute("SELECT closed_at FROM jobs WHERE url='https://zeta.test/j1'").fetchone()[0] is None


def test_workable_stops_probing_after_429(monkeypatch):
    """Бан от Cloudflare — прекращаем перебор суффиксов. Каждый лишний запрос
    после 429 только продлевает его, а компания всё равно потеряна."""
    import jobs
    seen = []

    def fake_get(url, want_json=True, with_code=False):
        seen.append(url)
        return (None, 429) if with_code else None

    monkeypatch.setattr(jobs, "get", fake_get)
    jobs._wk_suffix.clear()
    assert jobs.workable("zeta") is None
    assert len(seen) == 1, seen


def test_workable_remembers_winning_suffix(monkeypatch):
    """Суффикс найден один раз — дальше один запрос за цикл, а не три."""
    import jobs
    seen = []

    def fake_get(url, want_json=True, with_code=False):
        seen.append(url)
        ok = "zeta-1?" in url
        body = {"name": "Zeta", "jobs": [{"url": "u", "title": "t"}]} if ok else {"name": "Zeta"}
        return (body, 200) if with_code else body

    monkeypatch.setattr(jobs, "get", fake_get)
    jobs._wk_suffix.clear()
    assert len(jobs.workable("zeta")) == 1
    assert len(seen) == 2          # zeta пустой, zeta-1 сработал
    seen.clear()
    assert len(jobs.workable("zeta")) == 1
    assert len(seen) == 1 and "zeta-1?" in seen[0]


def test_collector_does_not_overwrite_who_added(monkeypatch):
    """Кто завёл компанию — пишется один раз. Обход ходит по ней дважды в сутки
    и не должен переписывать авторство на себя."""
    import jobs, storage
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO companies"
                 "(name,page_url,ats,slug,added_by,added_at) "
                 "VALUES('Omega','https://omega.test','lever','omega','703432434','2026-09-01')")
    conn.commit()

    monkeypatch.setitem(jobs.ADAPTERS, "lever", lambda s: [("u1", "Backend", "Remote", None)])
    jobs.run(only="Omega")

    who, when = storage.connect().execute(
        "SELECT added_by, added_at FROM companies WHERE name='Omega'").fetchone()
    assert (who, when) == ("703432434", "2026-09-01")


def test_workable_empty_account_is_not_a_failure(monkeypatch):
    """Аккаунт есть, вакансий ноль — это пустая доска, а не сбой. Vivid Money
    именно такой: путать его с 429 значит врать в отчёте о здоровье."""
    import jobs

    def fake_get(url, want_json=True, with_code=False):
        body = {"name": "Vivid Money", "jobs": []} if "vivid?" in url else None
        code = 200 if body else 404
        return (body, code) if with_code else body

    monkeypatch.setattr(jobs, "get", fake_get)
    jobs._wk_suffix.clear()
    assert jobs.workable("vivid") == []          # пусто, но доска жива
    assert jobs.workable("неттакого") is None    # вообще не дозвонились


XATA_HTML = '''<h2>Open positions</h2><div class="flex flex-col gap-4">
<a class="group flex" href="/careers/7952382"><span class="text-foreground">Forward Deployed Engineer</span><span class="sr-only"> — </span><span class="text-muted">Remote</span></a>
<a class="group flex" href="/careers/7928089"><span class="text-foreground">Senior Backend Engineer (Go/Rust)</span><span class="sr-only"> — </span><span class="text-muted">Remote</span></a>
</div><a href="/careers">Все вакансии</a><a href="/blog/2024">Блог</a>'''


def test_linked_jobs_reads_own_site_listing():
    """Сайт сам перечисляет вакансии ссылками — так устроена Xata: доска на
    Teamtailor, но публичного фида нет. Ссылки без номера вакансии не берём."""
    import jobs
    got = jobs.linked_jobs(XATA_HTML, "https://xata.io/careers")
    assert [(u, t, loc) for u, t, loc, _ in got] == [
        ("https://xata.io/careers/7952382", "Forward Deployed Engineer", "Remote"),
        ("https://xata.io/careers/7928089", "Senior Backend Engineer (Go/Rust)", "Remote"),
    ], got


def test_linked_jobs_skips_non_job_links():
    """/careers и /blog/2024 — не вакансии: нужен числовой id в пути."""
    import jobs
    assert jobs.linked_jobs('<a href="/careers">Вакансии</a><a href="/blog/2024">Блог</a>',
                            "https://xata.io/careers") == []


def test_fresh_company_is_skipped_but_stale_one_is_not(monkeypatch):
    """Бот перезапускается чаще, чем обновляются доски. Проверенную час назад
    компанию не трогаем, проверенную вчера — трогаем."""
    from datetime import datetime, timedelta, timezone
    import jobs, storage
    conn = storage.connect()
    hour = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    day = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    conn.execute("INSERT OR REPLACE INTO companies(name,page_url,ats,slug,checked_at) "
                 "VALUES('Fresh','https://f.test','lever','fresh',?)", (hour,))
    conn.execute("INSERT OR REPLACE INTO companies(name,page_url,ats,slug,checked_at) "
                 "VALUES('Stale','https://s.test','lever','stale',?)", (day,))
    conn.commit()

    hit = []
    monkeypatch.setitem(jobs.ADAPTERS, "lever",
                        lambda s: hit.append(s) or [(f"u/{s}", "Backend", "Remote", None)])
    new, changed, stats, skipped, _ = jobs.run()

    assert "stale" in hit and "fresh" not in hit, hit
    assert skipped >= 1


def test_named_run_ignores_the_freshness_window(monkeypatch):
    """Запуск по имени — явная просьба, окно её не отменяет."""
    from datetime import datetime, timezone
    import jobs, storage
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO companies(name,page_url,ats,slug,checked_at) "
                 "VALUES('Justnow','https://j.test','lever','justnow',?)",
                 (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    conn.commit()

    hit = []
    monkeypatch.setitem(jobs.ADAPTERS, "lever",
                        lambda s: hit.append(s) or [(f"u/{s}", "Backend", "Remote", None)])
    jobs.run(only="Justnow")
    assert hit == ["justnow"], hit


def test_discover_ignores_an_empty_board(monkeypatch):
    """Аккаунт на доске есть, вакансий нет — это не та компания. Так Atlassian
    и Revolut получили чужие доски на Workable."""
    import jobs
    monkeypatch.setattr(jobs, "from_url", lambda u: None)
    monkeypatch.setattr(jobs, "slug_variants", lambda n: ["zeta"])
    monkeypatch.setattr(jobs, "board_owner", lambda *a: None)
    for name in jobs.ADAPTERS:
        monkeypatch.setitem(jobs.ADAPTERS, name, lambda s: [])
    assert jobs.discover("Zeta", "") is None

    monkeypatch.setitem(jobs.ADAPTERS, "lever", lambda s: [("u", "Backend", "Remote", None)])
    assert jobs.discover("Zeta", "") == ("lever", "zeta")


WORKABLE_PAYLOAD = [
    {"shortcode": "AAA", "title": "Platform Engineer", "url": "https://w/j/AAA",
     "telecommuting": "True", "country": "United States", "city": "Austin",
     "published_on": "2026-09-04", "description": "<p>Run the platform</p>"},
    {"shortcode": "AAA", "title": "Platform Engineer", "url": "https://w/j/AAA",
     "telecommuting": "True", "country": "Canada", "city": "Ottawa",
     "published_on": "2026-09-04"},
    {"shortcode": "BBB", "title": "Kernel Developer", "url": "https://w/j/BBB",
     "telecommuting": "", "country": "Poland", "city": "Warsaw",
     "published_on": "2026-09-05"},
]


def test_workable_collapses_city_rows_and_keeps_countries():
    """Workable отдаёт строку на пару «вакансия × город»: у CloudLinux 14 вакансий
    лежат в 77 записях с одним url. Схлопываем, страны собираем в локацию —
    иначе вакансия выглядит безадресной и гео-отсев её пропускает."""
    import jobs
    got = jobs.wk_rows(WORKABLE_PAYLOAD)
    assert len(got) == 2, got
    by_url = {r[0]: (r[1], r[2]) for r in got}
    assert by_url["https://w/j/AAA"] == ("Platform Engineer", "Remote: United States, Canada")
    assert by_url["https://w/j/BBB"] == ("Kernel Developer", "Poland")
    # details=true кладёт текст в тот же ответ — отдельных запросов не делаем
    assert [r[6] for r in got if r[0].endswith("AAA")] == ["Run the platform"]


def test_teamtailor_takes_location_from_jobposting():
    """В фиде Teamtailor нет ни `location`, ни `summary` — локация лежит в
    приложенном JobPosting. 126 вакансий числились безадресными."""
    import jobs
    item = {"title": "Senior Java", "url": "https://t/1", "date_published": "2026-09-29",
            "_jobposting": {"jobLocation": [
                {"address": {"addressLocality": "Banja Luka", "addressCountry": "BA"}},
                {"address": {"addressLocality": "Beograd", "addressCountry": "RS"}},
                {"address": {"addressLocality": "Banja Luka", "addressCountry": "BA"}}]}}
    assert jobs.tt_place(item) == "Banja Luka, BA · Beograd, RS"
    assert jobs.tt_place({"_jobposting": "{'jobLocation': [{'address': {'addressCountry': 'PL'}}]}"}) == "PL"
    assert jobs.tt_place({}) == ""
    assert jobs.tt_place({"_jobposting": "не json"}) == ""


PERSONIO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
<position><id>42</id><office>Porto</office><name>Backend Engineer</name>
<createdAt>2026-09-15T10:00:00+00:00</createdAt></position>
<position><office>Berlin</office><name>Без id — пропускаем</name></position>
</workzag-jobs>"""


def test_personio_parses_xml_and_skips_idless(monkeypatch):
    """Personio отдаёт XML, а не JSON, и на чужой поддомен — тоже 200."""
    monkeypatch.setattr(jobs, "get", lambda url, **kw: PERSONIO_XML)
    assert [r[:4] for r in jobs.personio("vivid")] == [
        ("https://vivid.jobs.personio.de/job/42?language=en", "Backend Engineer",
         "Porto", "2026-09-15")]
    monkeypatch.setattr(jobs, "get", lambda url, **kw: "<html>не фид</html>")
    assert jobs.personio("vivid") is None


WP_PAGE = [
    {"link": "https://x.test/en/jobs/a/", "title": {"rendered": "Backend &#8211; Senior"},
     "lang": "en", "date": "2026-09-15T10:00:00"},
    {"link": "https://x.test/pt/jobs/a/", "title": {"rendered": "Backend"},
     "lang": "pt", "date": "2026-09-15T10:00:00"},
]


def test_wordpress_keeps_english_and_stops_on_empty_page(monkeypatch):
    """Polylang отдаёт ту же вакансию на двух языках — в выдаче нужна одна."""
    pages = iter([WP_PAGE, []])
    monkeypatch.setattr(jobs, "get", lambda url, **kw: next(pages, []))
    assert [r[:4] for r in jobs.wordpress("x.test/jobs")] == [
        ("https://x.test/en/jobs/a/", "Backend – Senior", "", "2026-09-15")]
    monkeypatch.setattr(jobs, "get", lambda url, **kw: {"code": "rest_no_route"})
    assert jobs.wordpress("x.test/jobs") is None


def test_wordpress_finds_the_post_type_when_slug_has_none(monkeypatch):
    """Тип записи у каждого сайта свой: у BNP jobs, у Akvelon vacancy."""
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        if "/vacancy?" in url and url.endswith("&page=1"):
            return WP_PAGE
        return [] if "/vacancy?" in url else {"code": "rest_no_route"}

    monkeypatch.setattr(jobs, "get", fake_get)
    rows = jobs.wordpress("x.test")
    assert len(rows) == 1 and "/en/jobs/a/" in rows[0][0]
    assert any("/wp/v2/jobs?" in u for u in seen)      # сперва пробуем jobs
    assert any("/wp/v2/vacancy?" in u for u in seen)


FINDEV_HTML = """
<a href="/career/open-positions/senior-java-developer-storm-4256535">
  <div><h3>Java developer (STORM)</h3><span>Senior</span>
  <p>%s</p><span>Spain</span></div></a>
<a href="/jobs/998877">Backend</a>
<a href="/career/open-positions/filter">Все вакансии</a>
""" % ("x" * 700)


def test_linked_jobs_reads_id_after_slug_and_long_card():
    """Findev: id в хвосте слага, а карточка длиннее прежнего окна в 600."""
    rows = jobs.linked_jobs(FINDEV_HTML, "https://fin.dev/career")
    hrefs = [r[0] for r in rows]
    assert "https://fin.dev/career/open-positions/senior-java-developer-storm-4256535" in hrefs
    assert "https://fin.dev/jobs/998877" in hrefs        # старый формат не сломан
    assert all("filter" not in h for h in hrefs)         # ссылка-фильтр не вакансия
    storm = next(r for r in rows if "storm" in r[0])
    assert storm[1] == "Java developer (STORM)" and storm[2] == "Spain"


RP_POSTINGS = [{
    "id": "f8523682-4cbf-4c3c-a7c3-70333c4836c9",
    "title": "Senior Backend Engineer (Python)",
    "locations": [{"name": "Europe", "type": "remote", "country": {"name": "United Kingdom"}},
                  {"name": "Dubai", "type": "office", "country": {"name": "United Kingdom"}}],
}]


def test_revolutpeople_builds_url_and_ignores_country(monkeypatch):
    """country у Elixi везде United Kingdom, хотя нанимают в Европе и ОАЭ."""
    card = {"description": "<p>Строим бота</p>",
            "creation_date_time": "2026-07-28T08:52:22.562206Z"}
    monkeypatch.setattr(jobs, "get",
                        lambda url, **kw: card if url.rstrip("/").endswith(RP_POSTINGS[0]["id"])
                        else RP_POSTINGS)
    row, = jobs.revolutpeople("elixi")
    url, title, where, when = row[:4]
    assert url == ("https://revolutpeople.com/elixi/public/careers/position/"
                   "senior-backend-engineer-python-f8523682-4cbf-4c3c-a7c3-70333c4836c9")
    assert title == "Senior Backend Engineer (Python)"
    assert where == "Europe (remote), Dubai (office)"
    assert "United Kingdom" not in where
    # страница вакансии закрыта Cloudflare, карточка по API открывается
    assert (row[6], when) == ("Строим бота", "2026-07-28")


def test_job_id_like_tells_id_from_section():
    """Три формата id против разделов сайта и дублей «Apply now»."""
    assert jobs.job_id_like("/jobs/123456")                       # xata
    assert jobs.job_id_like("/career/open-positions/java-dev-4256535")
    assert jobs.job_id_like("/emcd/job/3W39VW58")                 # careers-page
    assert jobs.job_id_like("/jobs/job/sap-basis-consultant/r5yeHsqh")
    assert not jobs.job_id_like("/jobs/job/apply/r5yeHsqh")       # дубль заявки
    assert not jobs.job_id_like("/career/open-positions/filter")
    assert not jobs.job_id_like("/jobs/web3-developer-2024-guide")


NOTION_HTML = """
<a href="https://app.notion.com/p/podscribe/Senior-Backend-Engineer-2b2454e64c6a8052b2b6d2ecfa74f9d2?source=copy_link">роль</a>
<a href="https://app.notion.com/p/podscribe/Senior-Backend-Engineer-2b2454e64c6a8052b2b6d2ecfa74f9d2">та же, второй раз</a>
<a href="https://www.notion.so/podscribe/Our-Handbook">не вакансия</a>
"""


def test_notion_jobs_dedups_and_needs_page_id():
    """Podscribe держит вакансии страницами Notion, ссылка повторяется дважды."""
    rows = jobs.notion_jobs(NOTION_HTML)
    assert len(rows) == 1
    url, title, where, when = rows[0]
    assert title == "Senior Backend Engineer"
    assert url.endswith("2b2454e64c6a8052b2b6d2ecfa74f9d2")   # без ?source=
    assert (where, when) == ("", None)


SURFE_HTML = """
<h3>Senior Backend Engineer</h3>
<a href="https://app.dover.com/apply/surfe/b812b5b6-42b6-417a-923c-7737ba82a06f">Apply now</a>
<h3>Our values</h3>
<a href="https://www.surfe.com/apply/newsletter">Apply now</a>
"""


def test_apply_links_take_title_from_heading():
    """У Surfe текст ссылки — «Apply now», название стоит в заголовке перед ней."""
    rows = jobs.apply_links(SURFE_HTML)
    assert len(rows) == 1                       # чужой /apply/ не считается
    url, title, where, when = rows[0]
    assert title == "Senior Backend Engineer"
    assert url.startswith("https://app.dover.com/apply/surfe/")




FLIGHT_HTML = (
    'self.__next_f.push([1,"{\\"type\\":\\"jobsList\\",\\"data\\":{\\"jobs\\":['
    '{\\"id\\":\\"799d1cae-de94-4580-b877-f50b73d8c436\\",'
    '\\"title\\":\\"Senior Backend (GO) Engineer\\",\\"timezone\\":\\"GMT-6\\",'
    '\\"requirementsHtml\\":\\"$19\\"},'
    '{\\"id\\":\\"799d1cae-de94-4580-b877-f50b73d8c436\\",'
    '\\"title\\":\\"Senior Backend (GO) Engineer\\",\\"timezone\\":\\"GMT-6\\",'
    '\\"requirementsHtml\\":\\"$19\\"}]}}"])'
    # текст — отдельная строка потока, длина 0x17 в байтах, а не в символах
    'self.__next_f.push([1,"19:T17,<p>Go и Kubernetes</p>"])'
)


def test_flight_jobs_reads_next_router_stream():
    """App Router держит данные в self.__next_f, а не в __NEXT_DATA__."""
    rows = jobs.flight_jobs(FLIGHT_HTML, "https://kake.co/jobs")
    assert len(rows) == 1                     # id повторяется в потоке
    url, title, tz, when = rows[0][:4]
    assert url == "https://kake.co/jobs/799d1cae-de94-4580-b877-f50b73d8c436"
    assert (title, tz, when) == ("Senior Backend (GO) Engineer", "GMT-6", None)
    assert rows[0][6] == "Go и Kubernetes"   # длина строки задана в байтах


AR_PAGE = {"data": {"vacancies": {
    "pageInfo": {"hasNextPage": False, "endCursor": "x"},
    "nodes": [
        {"title": "Senior Go Developer", "slug": "senior-go-developer",
         "date": "2026-05-07T10:00:00",
         "vacancyPageCustomFields": {"vacancyStatus": True, "location": ["Remote"],
                                     "content": [{"title": None, "text": "<p>Про компанию</p>"},
                                                 {"title": "Required Skills:",
                                                  "text": "<p>5+ years of Go</p>"}]}},
        {"title": "Закрытая с 2022", "slug": "old-one", "date": "2022-02-14T10:00:00",
         "vacancyPageCustomFields": {"vacancyStatus": False, "location": ["Remote"]}},
    ]}}}


def test_aristek_keeps_only_flagged_open_and_fixes_host(monkeypatch):
    """В архиве 108 вакансий с 2022 года, открыты четыре — остальные publish."""
    monkeypatch.setattr(jobs, "post_json", lambda url, payload, ua=None: AR_PAGE)
    row, = jobs.aristek("stage.aristeksystems.com")
    url, title, where, when = row[:4]
    assert url == "https://aristeksystems.com/career/senior-go-developer/"
    assert "stage." not in url                 # ссылка из API ведёт на stage
    assert (title, where, when) == ("Senior Go Developer", "Remote", "2026-05-07")
    # у самой записи content пустой, текст разложен по секциям ACF
    assert row[6] == "Про компанию Required Skills: 5+ years of Go"


ATL_LISTING = [{
    "portalJobPost": {"portalUrl": "https://globalcareers-atlassian.icims.com/jobs/1/x/job",
                      "updatedDate": "2026-09-24 03:33 PM"},
    "title": "Senior Backend Engineer",
    "applyUrl": "https://globalcareers-atlassian.icims.com/jobs/1/x/job?mode=apply",
    "locations": ["Bengaluru - India -   Bengaluru,  560071 India",
                  "Remote - Remote", "Remote - UK - Remote"],
}]


def test_atlassian_shortens_locations_and_skips_apply_url(monkeypatch):
    """Локации приходят в три колена с индексом; ссылка нужна без mode=apply."""
    monkeypatch.setattr(jobs, "get", lambda url, **kw: ATL_LISTING)
    url, title, where, when = jobs.atlassian("www.atlassian.com")[0][:4]
    assert url.endswith("/job") and "mode=apply" not in url
    assert where == "Bengaluru - India, Remote, Remote - UK"
    assert (title, when) == ("Senior Backend Engineer", "2026-09-24")


PF_PAGE1 = ('<a class="stretched-link tw-text-black" data-turbo-frame="_top"'
            ' href="/v/238101-senior-devops-engineer">Senior Devops &amp; Engineer</a>'
            '<a class="other" href="/v/1-not-a-card">Чужая ссылка</a>')


def test_peopleforce_pages_until_nothing_new(monkeypatch):
    """Страницы по десять; вторая повторяет первую — значит список кончился."""
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        return PF_PAGE1

    monkeypatch.setattr(jobs, "get", fake_get)
    rows = jobs.peopleforce("careers.taxdome.com")
    assert len(rows) == 1                       # вторая страница не добавила нового
    # страница списка, страница самой вакансии, вторая страница списка
    assert len(seen) == 3                       # и дальше не ходим
    url, title, where, when = rows[0][:4]
    assert url == "https://careers.taxdome.com/v/238101-senior-devops-engineer"
    assert title == "Senior Devops & Engineer"


HIVEX_PAGE = '<a class="d-block" href="/hivexteam/job/L54V9X93">Shopify Developer</a>'


def test_peopleforce_reads_shared_domain_layout(monkeypatch):
    """На careers-page.com вёрстка другая: класс d-block вместо stretched-link."""
    pages = iter([HIVEX_PAGE, HIVEX_PAGE])
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        return next(pages, "")

    monkeypatch.setattr(jobs, "get", fake_get)
    rows = jobs.peopleforce("www.careers-page.com/hivexteam")
    assert len(rows) == 1                       # вторая страница повторила первую
    assert rows[0][0] == "https://www.careers-page.com/hivexteam/job/L54V9X93"
    assert rows[0][1] == "Shopify Developer"
    assert seen[0] == "https://www.careers-page.com/hivexteam?page=1"


def test_triage_no_longer_judges_geography():
    """Географию решают ворота: три дешёвые модели путались в строках вида
    «Remote: Portugal, Poland, Spain» и теряли до половины подходящего."""
    import bot
    assert "Географию не оценивай" in bot.TRIAGE_SYSTEM
    assert "локацию игнорируй" in bot.TRIAGE_SYSTEM


def test_wordpress_keeps_description(monkeypatch):
    """Текст вакансии приходит в том же ответе — без него оценка слепа к стеку."""
    page = [{"link": "https://x.test/en/jobs/a/", "lang": "en",
             "title": {"rendered": "Backend"}, "date": "2026-09-15T10:00:00",
             "content": {"rendered": "<p>Python &amp; Kafka</p><script>x</script>"}}]
    pages = iter([page, []])
    monkeypatch.setattr(jobs, "get", lambda url, **kw: next(pages, []))
    (row,) = jobs.wordpress("x.test/jobs")
    assert len(row) == 7
    assert row[6] == "Python & Kafka"       # без тегов, скриптов и сущностей


def test_plain_text_trims_long_tail():
    assert jobs.plain_text("<p>" + "a" * 20000 + "</p>", limit=100) == "a" * 100
    assert jobs.plain_text("") is None


def test_adapters_carry_description(monkeypatch):
    """Текст приходит в том же ответе — адаптер обязан его донести, а не терять."""
    monkeypatch.setattr(jobs, "get", lambda url, **kw: {"jobs": [
        {"jobUrl": "u", "title": "t", "location": "l", "publishedAt": None,
         "descriptionPlain": "Python и Kafka"}]})
    (row,) = jobs.ashby("x")
    assert len(row) == 7 and row[6] == "Python и Kafka"

    monkeypatch.setattr(jobs, "get", lambda url, **kw: {"jobs": [
        {"absolute_url": "u", "title": "t", "location": {"name": "l"},
         "content": "<p>Go &amp; k8s</p>"}]})
    (row,) = jobs.greenhouse("x")
    assert row[6] == "Go & k8s"


def test_greenhouse_asks_for_content(monkeypatch):
    """Без content=true Greenhouse текст не отдаёт, а лишний запрос не нужен."""
    seen = []
    monkeypatch.setattr(jobs, "get", lambda url, **kw: seen.append(url) or {"jobs": []})
    jobs.greenhouse("x")
    assert "content=true" in seen[0]


NEXT_DATA_HTML = (
    '<script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"positions":['
    '{"title":"SENIOR PYTHON DEVELOPER","link":"https://talent.sage.hr/jobs/abc",'
    '"content":"<p>We\'re looking for a Senior Software Engineer.</p>"}]}}}'
    '</script>'
)


def test_next_data_takes_description_from_card():
    """У BETBY текст лежит в карточке: ссылка ведёт на Sage HR за Cloudflare."""
    rows = jobs.next_data(NEXT_DATA_HTML)
    assert len(rows) == 1
    assert rows[0][0] == "https://talent.sage.hr/jobs/abc"
    assert rows[0][6] == "We're looking for a Senior Software Engineer."


def test_smartrecruiters_pulls_text_from_each_posting(monkeypatch):
    """В списке текста нет: он приходит карточкой, без companyDescription."""
    answers = {
        "https://api.smartrecruiters.com/v1/companies/gcore/postings": {
            "totalFound": 1,
            "content": [{"id": "744", "name": "Go Engineer",
                         "location": {"city": "Porto", "country": "pt",
                                      "fullLocation": "Porto, Porto District, Portugal"}}]},
        "https://api.smartrecruiters.com/v1/companies/gcore/postings/744": {
            "jobAd": {"sections": {
                "companyDescription": {"text": "<p>Gcore is great</p>"},
                "jobDescription": {"text": "<p>Write Go</p>"},
                "qualifications": {"text": "<p>5 years</p>"}}}},
    }
    monkeypatch.setattr(jobs, "get", lambda url, **kw: answers.get(url))
    rows = jobs.smartrecruiters("gcore")
    assert rows[0][6] == "Write Go 5 years"


def test_sr_place_prefers_country_spelled_out():
    """country отдаёт код: «London gb» гео-ворота не читают."""
    assert jobs.sr_place({"city": "London", "country": "gb",
                          "fullLocation": "London, England, United Kingdom"}) \
        == "London, England, United Kingdom"
    # у Gcore страны разъезжаются по полям, а в fullLocation есть пустые куски
    assert jobs.sr_place({"city": "Serbia", "country": "cy",
                          "fullLocation": "Serbia, , Cyprus"}) == "Serbia, Cyprus"
    assert jobs.sr_place({"city": "Porto", "country": "pt"}) == "Porto pt"


def test_peopleforce_reads_text_from_job_page(monkeypatch):
    """Текста в списке нет: страница вакансии — обычный HTML без JS."""
    listing = ('<a class="stretched-link tw-text-black" '
               'href="/v/42-backend-engineer">Backend Engineer</a>')
    pages = {"https://careers.acme.com/?page=1": listing,
             "https://careers.acme.com/v/42-backend-engineer":
                 "<h1>Backend Engineer</h1><p>Python, 5+ years</p>"}
    monkeypatch.setattr(jobs, "get", lambda url, **kw: pages.get(url))
    rows = jobs.peopleforce("careers.acme.com")
    assert rows[0][0] == "https://careers.acme.com/v/42-backend-engineer"
    assert rows[0][6] == "Backend Engineer Python, 5+ years"


EPAM_FACETS = {"country": [{"key": "Portugal", "id": "406", "doc_count": 52},
                           {"key": "Mexico", "id": "405", "doc_count": 1055}]}
EPAM_JOB = {"name": "Senior Python Developer", "vacancy_type": "Remote",
            "country": [{"name": "Portugal"}], "created_at": "2026-09-29T08:31:25.011Z",
            "seo": {"url": "/en/vacancy/senior-python-developer-blt1_en"},
            "text": "Python, 5+ years"}


def test_epam_filters_by_country_id_not_name(monkeypatch):
    """Страна задаётся id из фасетов: по имени доска отвечает пустым списком."""
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        if "facets" not in url:
            return {"data": {"total": 5060, "jobs": [], "facets": EPAM_FACETS}}
        return {"data": {"total": 1, "jobs": [EPAM_JOB], "facets": EPAM_FACETS}}

    monkeypatch.setattr(jobs, "get", fake_get)
    row, = jobs.epam("Portugal")
    assert "facets=country%3D406" in seen[1]
    assert row[0] == "https://careers.epam.com/en/vacancy/senior-python-developer-blt1_en"
    assert (row[1], row[2], row[3]) == ("Senior Python Developer", "Remote: Portugal",
                                        "2026-09-29")
    assert row[6] == "Python, 5+ years"
    assert jobs.epam("Атлантида") == []     # страны нет в фасетах, но доска жива


def test_page_text_only_fills_what_is_missing(monkeypatch):
    """У BETBY текст уже есть, а ссылка ведёт на Sage HR за Cloudflare: лишний
    запрос туда ничего не даст. Дочитываем только пустые."""
    seen = []
    page = "<p>Python, 5+ years. " + "Опыт с Kubernetes и PostgreSQL. " * 12 + "</p>"
    monkeypatch.setattr(jobs, "get", lambda url, **kw: seen.append(url) or page)
    rows = jobs.page_text(
        [("https://acme.test/jobs/1", "Backend", "Porto", None, None, "", "уже есть"),
         ("https://acme.test/jobs/2", "Frontend", "Porto", None)],
        "https://acme.test/jobs")
    assert seen == ["https://acme.test/jobs/2"]
    assert rows[0][6] == "уже есть"
    assert rows[1][6].startswith("Python, 5+ years.")
    assert len(rows[1]) == 7                   # короткая строка дополнена до семи


def test_page_text_drops_a_javascript_stub(monkeypatch):
    """Dover у Surfe отдаёт оболочку SPA: пустое поле честнее такого «текста»."""
    monkeypatch.setattr(jobs, "get",
                        lambda url, **kw: "<p>You need to enable JavaScript to run this app.</p>")
    rows = jobs.page_text([("https://acme.test/jobs/1", "Backend", "Porto", None)],
                          "https://acme.test/jobs")
    assert rows[0][6] is None


def test_dover_job_takes_card_from_api(monkeypatch):
    """app.dover.com отдаёт оболочку SPA, карточку — свой API без ключа."""
    card = {"user_provided_description": "<h1>Who You Are</h1><p>Senior Backend</p>",
            "locations": [{"name": "Europe", "location_type": "REMOTE"},
                          {"name": "Europe", "location_type": "REMOTE"}]}
    seen = []
    monkeypatch.setattr(jobs, "get", lambda url, **kw: seen.append(url) or card)
    where, text = jobs.dover_job(
        "https://app.dover.com/apply/surfe/b812b5b6-42b6-417a-923c-7737ba82a06f")
    assert seen == ["https://app.dover.com/api/v1/inbound/application-portal-job/"
                    "b812b5b6-42b6-417a-923c-7737ba82a06f"]
    assert where == "Remote: Europe"           # повтор локации схлопнут
    assert text == "Who You Are Senior Backend"
    assert jobs.dover_job("https://acme.test/jobs/1") == (None, None)


def test_run_diagnoses_outside_the_transaction(monkeypatch, tmp_path):
    """diagnose ходит в сеть: внутри открытой транзакции он держал бы запись
    до 15 секунд на каждую молчащую доску."""
    import storage
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO companies(name,page_url,ats,slug) "
                 "VALUES('Няма','https://nyama.test','lever','nyama')")
    conn.commit()
    in_txn = []
    monkeypatch.setattr(jobs, "db", lambda: conn)
    monkeypatch.setitem(jobs.ADAPTERS, "lever", lambda s: None)
    monkeypatch.setattr(jobs, "diagnose",
                        lambda *a: in_txn.append(conn.in_transaction) or "HTTP 404")
    jobs.run(only="Няма")
    assert in_txn == [False]                   # сеть вне транзакции
    assert conn.execute("SELECT last_error FROM companies WHERE name='Няма'"
                        ).fetchone()[0] == "HTTP 404"


AVIA_HTML = (
    '<a class="card-HUSXor" data-id="4343726" href="/about/vacancies/4343726">'
    '<div class="header-N9FCCf"><img src="x.png"/>'
    '<div class="body-2-regular-zrjCZc team-pazXxA">Support: Monitoring</div></div>'
    '<div class="position-qdyp96"><svg><path d="M15"/></svg>'
    '<div class="bold-TA4Om3 title-KOQ0n_">Monitoring Specialist</div></div></a>'
    '<a class="card-HUSXor" href="/about/vacancies/4197840">'
    '<div class="team-pazXxA">Maintenance</div>'
    '<div class="title-KOQ0n_">System Administrator</div></a>'
)


def test_aviasales_takes_the_title_not_the_team(monkeypatch):
    """В карточке сначала отдел, потом должность: общий linked_jobs брал первое
    и писал в базу «Ticket» вместо «Team Lead»."""
    monkeypatch.setattr(jobs, "get", lambda url, **kw: AVIA_HTML if "vacancies" == url.rsplit("/", 1)[-1] else None)
    rows = jobs.aviasales("https://www.aviasales.ru/about/vacancies")
    assert [(r[0], r[1]) for r in rows] == [
        ("https://www.aviasales.ru/about/vacancies/4343726", "Monitoring Specialist"),
        ("https://www.aviasales.ru/about/vacancies/4197840", "System Administrator")]
    assert jobs.ADAPTERS["aviasales"] is jobs.aviasales


def test_closed_job_reopens_when_it_is_back_on_the_board(monkeypatch):
    """Разовый сбой выдачи закрывал вакансию навсегда: INSERT OR IGNORE молча
    пропускает известный url, и closed_at уже никто не снимал."""
    import storage
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO companies(name,page_url,ats,slug) "
                 "VALUES('Вернулась','https://back.test','lever','back')")
    conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,source,first_seen,closed_at) "
                 "VALUES('https://back.test/j1','Вернулась','Backend','lever',"
                 "'2026-10-01','2026-10-05')")
    conn.commit()
    monkeypatch.setattr(jobs, "db", lambda: conn)
    monkeypatch.setitem(jobs.ADAPTERS, "lever",
                        lambda s: [("https://back.test/j1", "Backend", "Porto", None)])
    new, _, _, _, reopened = jobs.run(only="Вернулась")
    assert reopened == 1
    assert conn.execute("SELECT closed_at FROM jobs WHERE url='https://back.test/j1'"
                        ).fetchone()[0] is None
    assert new == []                           # не новая: человек её уже видел
