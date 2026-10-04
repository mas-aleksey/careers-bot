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
        ok = url.endswith("zeta-1")
        body = {"name": "Zeta", "jobs": [{"url": "u", "title": "t"}]} if ok else {"name": "Zeta"}
        return (body, 200) if with_code else body

    monkeypatch.setattr(jobs, "get", fake_get)
    jobs._wk_suffix.clear()
    assert len(jobs.workable("zeta")) == 1
    assert len(seen) == 2          # zeta пустой, zeta-1 сработал
    seen.clear()
    assert len(jobs.workable("zeta")) == 1
    assert len(seen) == 1 and seen[0].endswith("zeta-1")


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
        body = {"name": "Vivid Money", "jobs": []} if url.endswith("vivid") else None
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
    new, changed, stats, skipped = jobs.run()

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
     "published_on": "2026-09-04"},
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
    by_url = {u: (t, loc) for u, t, loc, _ in got}
    assert by_url["https://w/j/AAA"] == ("Platform Engineer", "Remote: United States, Canada")
    assert by_url["https://w/j/BBB"] == ("Kernel Developer", "Poland")


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
    assert jobs.personio("vivid") == [
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
    assert jobs.wordpress("x.test/jobs") == [
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
    monkeypatch.setattr(jobs, "get", lambda url, **kw: RP_POSTINGS)
    (url, title, where, _), = jobs.revolutpeople("elixi")
    assert url == ("https://revolutpeople.com/elixi/public/careers/position/"
                   "senior-backend-engineer-python-f8523682-4cbf-4c3c-a7c3-70333c4836c9")
    assert title == "Senior Backend Engineer (Python)"
    assert where == "Europe (remote), Dubai (office)"
    assert "United Kingdom" not in where


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
    '\\"title\\":\\"Senior Backend (GO) Engineer\\",\\"timezone\\":\\"GMT-6\\"},'
    '{\\"id\\":\\"799d1cae-de94-4580-b877-f50b73d8c436\\",'
    '\\"title\\":\\"Senior Backend (GO) Engineer\\",\\"timezone\\":\\"GMT-6\\"}]}}"])'
)


def test_flight_jobs_reads_next_router_stream():
    """App Router держит данные в self.__next_f, а не в __NEXT_DATA__."""
    rows = jobs.flight_jobs(FLIGHT_HTML, "https://kake.co/jobs")
    assert len(rows) == 1                     # id повторяется в потоке
    url, title, tz, when = rows[0]
    assert url == "https://kake.co/jobs/799d1cae-de94-4580-b877-f50b73d8c436"
    assert (title, tz, when) == ("Senior Backend (GO) Engineer", "GMT-6", None)


AR_PAGE = {"data": {"vacancies": {
    "pageInfo": {"hasNextPage": False, "endCursor": "x"},
    "nodes": [
        {"title": "Senior Go Developer", "slug": "senior-go-developer",
         "date": "2026-05-07T10:00:00",
         "vacancyPageCustomFields": {"vacancyStatus": True, "location": ["Remote"]}},
        {"title": "Закрытая с 2022", "slug": "old-one", "date": "2022-02-14T10:00:00",
         "vacancyPageCustomFields": {"vacancyStatus": False, "location": ["Remote"]}},
    ]}}}


def test_aristek_keeps_only_flagged_open_and_fixes_host(monkeypatch):
    """В архиве 108 вакансий с 2022 года, открыты четыре — остальные publish."""
    monkeypatch.setattr(jobs, "post_json", lambda url, payload, ua=None: AR_PAGE)
    (url, title, where, when), = jobs.aristek("stage.aristeksystems.com")
    assert url == "https://aristeksystems.com/career/senior-go-developer/"
    assert "stage." not in url                 # ссылка из API ведёт на stage
    assert (title, where, when) == ("Senior Go Developer", "Remote", "2026-05-07")


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
    (url, title, where, when), = jobs.atlassian("www.atlassian.com")
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
    assert len(seen) == 2                       # и дальше не ходим
    url, title, where, when = rows[0]
    assert url == "https://careers.taxdome.com/v/238101-senior-devops-engineer"
    assert title == "Senior Devops & Engineer"
