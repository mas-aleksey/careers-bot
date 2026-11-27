import re
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


def test_embedded_payload_parsed(monkeypatch):
    """top.co держит вакансии в payload Next.js, а не в API. Место работы — поле
    location, а не имя продукта из company: у Walt и Tribute «Remote» терялся."""
    html = (r'\"id\":\"9699\",\"position\":\"Director of Risk\",\"location\":\"Remote\",'
            r'\"company\":{\"name\":\"Wallet\"},'
            r'{\"id\":\"9700\",\"position\":\"Fullstack Engineer\",\"location\":\"$undefined\",'
            r'\"company\":{\"name\":\"Walt\"}')
    assert jobs.JOB_IN_PAYLOAD.findall(html) == [("9699", "Director of Risk", "Remote"),
                                                 ("9700", "Fullstack Engineer", "$undefined")]
    monkeypatch.setattr(jobs, "get", lambda u, want_json=True, **k: html)
    assert jobs.embedded("https://top.co/careers") == [
        ("https://top.co/careers/9699", "Director of Risk", "Remote", None),
        ("https://top.co/careers/9700", "Fullstack Engineer", "", None)]


def test_every_adapter_is_registered():
    for name in ("ashby", "greenhouse", "lever", "smartrecruiters", "workable",
                 "recruitee", "teamtailor", "pinpoint", "breezy"):
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


def test_discover_skips_bamboohr_trial(monkeypatch):
    """Пробный аккаунт BambooHR отдаёт шаблонные вакансии самого BambooHR под
    именем компании: имя совпадает, вакансии чужие."""
    import jobs
    monkeypatch.setattr(jobs, "from_url", lambda u: None)
    monkeypatch.setattr(jobs, "slug_variants", lambda n: ["zeta"])
    for name in jobs.ADAPTERS:
        monkeypatch.setitem(jobs.ADAPTERS, name, lambda s: [])
    monkeypatch.setitem(jobs.ADAPTERS, "bamboohr",
                        lambda s: [("u", "Marketing Manager", "Marseille", None)])
    info = {"name": "ZETA", "inTrial": True}
    monkeypatch.setattr(jobs, "get", lambda url, **kw:
                        {"meta": [], "result": info} if "company-info" in url else None)
    assert jobs.discover("Zeta", "") is None
    info["inTrial"] = False
    assert jobs.discover("Zeta", "") == ("bamboohr", "zeta")
    info["name"] = "Other Corp"
    assert jobs.discover("Zeta", "") is None

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


AVIASALES_HTML = (
    '<a href="/about/vacancies/4219517"><div class="title-x">Product Analyst</div></a>'
    '<a href="/about/vacancies/4330401"><div class="title-x">Editor</div></a>'
    '<script>{"id":4219517,"position":"Product Analyst","tags":["SQL"],'
    '"team":{"name":"Product","icon":"https:\\u002F\\u002Fx.ru\\u002Fa.png"},"workPlace":"Удалённо"},'
    '{"id":4330401,"position":"Editor","tags":[],"team":{"name":"Commerce"},"workPlace":null}</script>')


def test_aviasales_takes_place_from_state_json(monkeypatch):
    """Место работы лежит в JSON состояния страницы, в карточке его нет.
    Работодатель заполняет поле не всегда — пустое остаётся пустым."""
    import jobs
    monkeypatch.setattr(jobs, "get", lambda u, want_json=True, **k: AVIASALES_HTML)
    monkeypatch.setattr(jobs, "page_text", lambda rows, listing: rows)
    got = jobs.aviasales("https://www.aviasales.ru/about/vacancies")
    by_title = {r[1]: r[2] for r in got}
    assert by_title == {"Product Analyst": "Удалённо", "Editor": ""}


def test_bamboohr_reads_remote_from_location_type():
    """Города у StellarTech пустые во всех полях, но locationType=1 — удалёнка."""
    import jobs
    assert jobs.bb_place({"locationType": "1"}) == "Remote"
    assert jobs.bb_place({"isRemote": True}) == "Remote"
    assert jobs.bb_place({"locationType": "2"}) == ""
    # Город важнее типа: он точнее
    assert jobs.bb_place({"locationType": "1",
                          "atsLocation": {"city": "Limassol", "country": "Cyprus"}}) == "Limassol, Cyprus"


def test_manatal_reads_api_with_location_and_text(monkeypatch):
    """careers-page.com — Manatal: локация и текст приходят API, а не вёрсткой.
    Пагинация идёт по next, вакансия без hash пропускается."""
    import jobs
    pages = {
        "https://www.careers-page.com/api/v1.0/c/emcd/jobs/": {
            "results": [
                {"hash": "AB12", "position_name": "Go Developer",
                 "location_display": "Limassol, Cyprus",
                 "description": "<p>" + "Нужен Go и PostgreSQL. " * 20 + "</p>"},
                {"position_name": "Без hash — пропускаем"}],
            "next": "https://www.careers-page.com/api/v1.0/c/emcd/jobs/?page=2"},
        "https://www.careers-page.com/api/v1.0/c/emcd/jobs/?page=2": {
            "results": [{"hash": "CD34", "position_name": "Analyst",
                         "location_display": "", "description": None}],
            "next": None},
    }
    monkeypatch.setattr(jobs, "get", lambda u, **k: pages[u])
    got = jobs.peopleforce("www.careers-page.com/emcd")
    assert [r[0] for r in got] == ["https://www.careers-page.com/emcd/job/AB12",
                                   "https://www.careers-page.com/emcd/job/CD34"]
    assert got[0][1:3] == ("Go Developer", "Limassol, Cyprus")
    assert "PostgreSQL" in got[0][6]
    # Пустая локация остаётся пустой строкой, а не None: так её видит гейт
    assert got[1][2] == "" and got[1][6] is None


COMEET_HTML = """
<ul class="comeet-positions-list">
<li class="comeet-position"><div class="comeet-position-name"><a href="//medvidi.com/careers/co/remote-eu/48.769/engineering-manager/all">Engineering Manager</a></div><div class="comeet-position-meta">Remote EU  &middot;  Full-time  &middot;  Management</div></li>
<li class="comeet-position"><div class="comeet-position-name"><a href="//medvidi.com/careers/co/remote-eu/DC.17A/senior-frontend/all">Senior Frontend Developer</a></div><div class="comeet-position-meta">Remote EU  &middot;  Full-time  &middot;  Senior</div></li>
</ul>
"""


COMEET_JOB_HTML = (
    '<nav>Меню сайта: ADHD Adderall Vyvanse Ritalin</nav>'
    '<div class="comeet-outer-wrapper"><div class="comeet-position-info">'
    '<div class="comeet-position-description">About The Position Нужен dbt и Airflow.</div>'
    '<div class="comeet-position-requirements">Требования: SQL, Python.'
    '</div></div></div>'
    '<footer>Подвал сайта на тысячу слов</footer>')


def test_comeet_takes_only_job_blocks_and_skips_known(monkeypatch):
    """Страница вакансии — страница всего сайта. Берём только блоки вакансии,
    иначе в описание уедет меню. Известное не перекачиваем."""
    import jobs
    seen = []

    listing = "https://medvidi.com/careers/co/remote-eu/all"

    def fake_get(url, want_json=True, **k):
        seen.append(url)
        # Адрес вакансии тоже кончается на /all — различаем по полному совпадению
        return COMEET_HTML if url == listing else COMEET_JOB_HTML

    monkeypatch.setattr(jobs, "get", fake_get)
    known = {"https://medvidi.com/careers/co/remote-eu/48.769/engineering-manager/all"}
    got = jobs.comeet("medvidi.com/careers/co/remote-eu/all", have_text=known)
    assert got[0][6] is None                       # известную не трогали
    assert "dbt и Airflow" in got[1][6] and "SQL, Python" in got[1][6]
    assert "Меню сайта" not in got[1][6] and "Подвал" not in got[1][6]
    assert len([u for u in seen if u.endswith("senior-frontend/all")]) == 1


def test_comeet_reads_positions_from_page(monkeypatch):
    """Comeet-плагин вшивает вакансии прямо в страницу. Ссылки
    протокол-относительные — без urljoin в базу уехал бы битый url."""
    import jobs
    monkeypatch.setattr(jobs, "get", lambda u, want_json=True, **k: COMEET_HTML)
    got = jobs.comeet("medvidi.com/careers/co/remote-eu/all")
    assert len(got) == 2
    assert got[0][0] == "https://medvidi.com/careers/co/remote-eu/48.769/engineering-manager/all"
    assert got[0][1] == "Engineering Manager"
    assert got[0][2] == "Remote EU"
    # Чужая страница без разметки Comeet — не доска
    monkeypatch.setattr(jobs, "get", lambda u, want_json=True, **k: "<html>нет вакансий</html>")
    assert jobs.comeet("example.com/careers") is None


COMEET_HOSTED = """<script>
COMPANY_DATA = {"name": "Acme"};
COMPANY_POSITIONS_DATA = [{"name": "Data Engineer ", "uid": "AC.F50", "is_internal": false,
 "url_comeet_hosted_page": "https://www.comeet.com/jobs/acme/98.008/data-engineer/AC.F50",
 "location": {"name": "Poland", "country": "PL", "city": "Warszawa"}, "workplace_type": "Remote",
 "custom_fields": {"details": [{"name": "Requirements", "value": "<p>Нужен <b>dbt</b></p>"}]}},
 {"name": "Внутренняя", "is_internal": true, "url_comeet_hosted_page": "https://x/y"}];
</script>"""


def test_comeet_hosted_page(monkeypatch):
    """Своя витрина Comeet: вакансии одним JSON в странице, текст рядом,
    разметки comeet-position нет. Слаг берётся из ссылки."""
    import jobs
    assert jobs.from_url("https://www.comeet.com/jobs/acme/98.008") == \
        ("comeet", "www.comeet.com/jobs/acme/98.008")
    seen = []
    monkeypatch.setattr(jobs, "get", lambda u, want_json=True, **k: seen.append(u) or COMEET_HOSTED)
    got = jobs.comeet("www.comeet.com/jobs/acme/98.008")
    assert seen == ["https://www.comeet.com/jobs/acme/98.008"]   # текст без доп. запросов
    assert len(got) == 1                                          # внутренняя отброшена
    url, title, where = got[0][:3]
    assert url.endswith("/data-engineer/AC.F50") and title == "Data Engineer"
    assert where == "Remote: Warszawa, Poland"
    assert "dbt" in got[0][6] and "<b>" not in got[0][6]


def test_oracle_fetches_text_only_for_new(monkeypatch):
    """Описание лежит в отдельном ресурсе. Дочитываем только то, чего нет в
    базе, и не больше ORACLE_TEXT_MAX за проход."""
    import jobs
    seen = []

    def fake_get(url, want_json=True, **k):
        seen.append(url)
        if "JobRequisitionDetails" in url:
            return {"items": [{"ExternalDescriptionStr": "<p>" + "Нужен Go. " * 40 + "</p>",
                               "ExternalQualificationsStr": "Erfaring med Kubernetes"}]}
        return {"items": [{"TotalJobsCount": 2, "requisitionList": [
            {"Id": 1, "Title": "A", "PrimaryLocation": "OSLO", "PostedDate": "2026-10-09"},
            {"Id": 2, "Title": "B", "PrimaryLocation": "OSLO", "PostedDate": "2026-10-09"}]}]}

    monkeypatch.setattr(jobs, "get", fake_get)
    monkeypatch.setattr(jobs, "ORACLE_TEXT_MAX", 1)
    known = {"https://fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1/"}
    got = jobs.oracle("fa.oraclecloud.com/CX_1", have_text=known)
    details = [u for u in seen if "JobRequisitionDetails" in u]
    # Известную не трогаем, на вторую уходит единственный остаток лимита
    assert len(details) == 1 and 'Id=%222%22' in details[0]
    assert got[0][6] is None
    assert "Kubernetes" in got[1][6]


def test_oracle_needs_expand_and_pages_by_total(monkeypatch):
    """Oracle Recruiting Cloud: limit и offset едут внутри finder, а без
    expand список приходит пустым при честном TotalJobsCount."""
    import jobs
    seen = []

    def fake_get(url, want_json=True, **k):
        seen.append(url)
        off = int(re.search(r"offset=(\d+)", url).group(1))
        n = 50 if off == 0 else 2
        return {"items": [{"TotalJobsCount": 52, "requisitionList": [
            {"Id": 100 + off + i, "Title": "Postbud", "PrimaryLocation": "OSLO, Norge",
             "PostedDate": "2026-10-09"} for i in range(n)]}]}

    monkeypatch.setattr(jobs, "get", fake_get)
    # have_text со всеми адресами: здесь проверяется только разбор списка
    got = jobs.oracle("fa-evem.oraclecloud.com/CX_1", have_text=ALL_SF_URLS)
    assert len(got) == 52 and len(seen) == 2
    assert "expand=requisitionList" in seen[0] and "offset=50" in seen[1]
    assert got[0][0].endswith("/sites/CX_1/job/100/")
    assert got[0][1:4] == ("Postbud", "OSLO, Norge", "2026-10-09")
    # Слаг без сайта — не доска Oracle, в сеть не ходим
    seen.clear()
    assert jobs.oracle("fa-evem.oraclecloud.com") is None and not seen


SF_PAGE_HTML = """
<p>Results 1 to 10 of 12</p>
<tr class="data-row">
  <td><span class="jobTitle"><a href="/job/Aarhus-Data-Engineer/8200/" class="jobTitle-link">Data Engineer</a></span></td>
  <td><span class="jobLocation">Aarhus N, DK, 8200</span> 25 Sept 2026</td>
</tr>
<tr class="data-row">
  <td><span class="jobTitle"><a href="/job/Tokyo-Sales/107/" class="jobTitle-link">Sales Director</a></span></td>
  <td><span class="jobLocation">Tokyo, JP</span> 8 Oct 2026</td>
</tr>
"""


def test_successfactors_parses_rows_and_stops_on_total(monkeypatch):
    """Выдача SuccessFactors — HTML-таблица по десять строк. Проверяем разбор
    строки, абсолютный url, «Sept» в дате и остановку по счётчику Results."""
    import jobs
    seen = []

    def fake_get(url, want_json=True, **k):
        seen.append(url)
        return SF_PAGE_HTML if len(seen) <= 6 else ""

    monkeypatch.setattr(jobs, "get", fake_get)
    # have_text со всеми адресами: здесь проверяем только разбор списка
    got = jobs.successfactors("careers.vestas.com", have_text=ALL_SF_URLS)
    assert got[0][:4] == ("https://careers.vestas.com/job/Aarhus-Data-Engineer/8200/",
                          "Data Engineer", "Aarhus N, DK, 8200", "2026-09-25")
    assert got[1][3] == "2026-10-08"
    # 12 вакансий по счётчику, по 2 в нашей заглушке — шесть страниц и стоп
    assert len(got) == 12 and len(seen) == 6
    assert "startrow=10" in seen[1] and "locale=en_GB" in seen[0]


def test_successfactors_broken_paging_is_not_a_full_list(monkeypatch):
    """Страница посреди выдачи не ответила. Неполный список сборщик принял бы
    за честный и закрыл бы непролистанные вакансии — возвращаем None."""
    import jobs
    pages = SF_PAGE_HTML.replace("of 12", "of 40")
    seen = []

    def fake_get(url, want_json=True, **k):
        seen.append(url)
        return pages if len(seen) <= 3 else None

    monkeypatch.setattr(jobs, "get", fake_get)
    assert jobs.successfactors("careers.vestas.com", have_text=ALL_SF_URLS) is None
    # Страница пришла, но без строк, — тоже обрыв, а не конец выдачи
    seen.clear()

    def no_rows(url, want_json=True, **k):
        seen.append(url)
        return pages if len(seen) <= 3 else "<p>Results 1 to 10 of 40</p>"

    monkeypatch.setattr(jobs, "get", no_rows)
    assert jobs.successfactors("careers.vestas.com", have_text=ALL_SF_URLS) is None


def test_successfactors_unified_search_json(monkeypatch):
    """Сайт перевели на новую выдачу: в HTML ни таблицы, ни счётчика, список
    приходит POST-ом из /services/recruiting/v1/jobs. Листаем до totalJobs,
    адрес — /job/<urlTitle>/<id>/, как у карточки со старой таблицы."""
    import json as js, jobs
    seen = []

    def card(i):
        return {"response": {"id": str(i), "urlTitle": f"Aarhus-N-R&amp;D-Engineer-{i}",
                             "unifiedStandardTitle": f"Engineer {i}",
                             "jobLocationShort": ["Aarhus N, Regi, DK"]}}

    def fake_get(url, want_json=True, data=None, **k):
        if "/search/" in url:
            return "<html><div id='job-results-list'></div></html>"
        page = js.loads(data)["pageNumber"]
        seen.append(page)
        rows = [card(page * 2), card(page * 2 + 1)] if page < 3 else []
        return {"totalJobs": 5, "jobSearchResult": rows[:5 - page * 2]}

    monkeypatch.setattr(jobs, "get", fake_get)
    got = jobs.successfactors("careers.vestas.com", have_text=ALL_SF_URLS)
    assert got[0][:4] == ("https://careers.vestas.com/job/Aarhus-N-R&D-Engineer-0/0/",
                          "Engineer 0", "Aarhus N, Regi, DK", None)
    assert len(got) == 5 and seen == [0, 1, 2]

    # Ответ оборвался на второй странице — не полный список, обход пропускаем
    seen.clear()

    def broken(url, want_json=True, data=None, **k):
        if "/search/" in url:
            return "<html></html>"
        page = js.loads(data)["pageNumber"]
        return {"totalJobs": 40, "jobSearchResult": [card(1)]} if page == 0 else None

    monkeypatch.setattr(jobs, "get", broken)
    assert jobs.successfactors("careers.vestas.com", have_text=ALL_SF_URLS) is None


class ALL_SF_URLS:
    """Множество, в котором «лежит» любой адрес: имитирует базу, где описание
    уже есть у всех вакансий."""

    def __contains__(self, item):
        return True


ALL_SF_URLS = ALL_SF_URLS()


def test_successfactors_fetches_text_only_for_new(monkeypatch):
    """Текста в выдаче нет, он на странице вакансии. Дочитываем только то,
    чего нет в базе, и не больше SF_TEXT_MAX за проход."""
    import jobs
    seen = []

    def fake_get(url, want_json=True, **k):
        seen.append(url)
        if "/search/" in url:
            return SF_PAGE_HTML
        return "<p>" + "Требуется dbt и Airflow. " * 20 + "</p>"

    monkeypatch.setattr(jobs, "get", fake_get)
    monkeypatch.setattr(jobs, "SF_TEXT_MAX", 1)
    known = {"https://careers.vestas.com/job/Aarhus-Data-Engineer/8200/"}
    got = jobs.successfactors("careers.vestas.com", have_text=known)
    jobs_fetched = [u for u in seen if "/job/" in u]
    # Известную не трогаем, на вторую тратим единственный лимит
    assert jobs_fetched == ["https://careers.vestas.com/job/Tokyo-Sales/107/"]
    assert got[0][6] is None and "dbt" in got[1][6]


def test_phenom_posts_widget_and_pages_until_total(monkeypatch):
    """Phenom отдаёт список только POST-ом и страницами по 50. Проверяем, что
    тело уходит, вторая страница запрашивается со сдвигом и на totalHits мы
    останавливаемся, а не крутим двадцать запросов."""
    import json as js, jobs
    calls = []

    def fake_get(url, want_json=True, with_code=False, data=None):
        body = js.loads(data)
        calls.append((url, body["from"]))
        n = 50 if body["from"] == 0 else 1
        return {"refineSearch": {"totalHits": 51, "data": {"jobs": [
            {"applyUrl": f"https://ats/{body['from']}-{i}", "title": "Data Engineer",
             "cityStateCountry": "Copenhagen, Capital, Denmark",
             "postedDate": "2026-10-07T00:00:00.000+0000",
             "descriptionTeaser": "Build pipelines", "ml_skills": ["dbt", "Airflow"]}
            for i in range(n)]}}}

    monkeypatch.setattr(jobs, "get", fake_get)
    got = jobs.phenom("careers.genmab.com")
    assert [c[1] for c in calls] == [0, 50]
    assert calls[0][0] == "https://careers.genmab.com/widgets"
    assert len(got) == 51
    assert got[0][:4] == ("https://ats/0-0", "Data Engineer",
                          "Copenhagen, Capital, Denmark", "2026-10-07")
    assert got[0][6] == "Build pipelines dbt, Airflow"
    # Пустая выдача — не доска Phenom, а чужой сайт: None, а не []
    monkeypatch.setattr(jobs, "get", lambda *a, **k: {"refineSearch": {}})
    assert jobs.phenom("careers.nobody.com") is None


def test_phenom_broken_paging_is_not_a_full_list(monkeypatch):
    """Вторая страница виджета не ответила при totalHits 140: отдать первые 50
    значило бы закрыть 90 живых вакансий. Возвращаем None."""
    import json as js, jobs

    def fake_get(url, want_json=True, with_code=False, data=None):
        if js.loads(data)["from"]:
            return None
        return {"refineSearch": {"totalHits": 140, "data": {"jobs": [
            {"applyUrl": f"https://ats/{i}", "title": "Data Engineer"} for i in range(50)]}}}

    monkeypatch.setattr(jobs, "get", fake_get)
    assert jobs.phenom("careers.genmab.com") is None


def test_phenom_repeats_pass_when_pages_overlap(monkeypatch):
    """Порядок выдачи Phenom плавает, страницы идут внахлёст: проход видит
    дубль и теряет вакансию. Потерянную сборщик закрыл бы, поэтому добираем
    вторым проходом, пока уникальных не станет totalHits, и дубли выкидываем."""
    import json as js, jobs
    calls = []

    def fake_get(url, want_json=True, with_code=False, data=None):
        body = js.loads(data)
        calls.append(body["from"])
        first_pass = len(calls) <= 2
        if body["from"] == 0:
            ids = range(50)
        else:      # первый проход: вместо 50-й снова 49-я
            ids = [49] if first_pass else [50]
        return {"refineSearch": {"totalHits": 51, "data": {"jobs": [
            {"applyUrl": f"https://ats/{i}", "title": "Data Engineer"} for i in ids]}}}

    monkeypatch.setattr(jobs, "get", fake_get)
    got = jobs.phenom("careers.genmab.com")
    assert calls == [0, 50, 0, 50]
    assert sorted(r[0] for r in got) == sorted(f"https://ats/{i}" for i in range(51))


def test_teamtailor_slug_with_dot_is_own_domain(monkeypatch):
    """Свой домен вместо поддомена Teamtailor: у Trackunit фид на
    careers.trackunit.com, trackunit.teamtailor.com отдаёт 404."""
    import jobs
    seen = []
    monkeypatch.setattr(jobs, "get", lambda u, **k: seen.append(u) or {"items": []})
    jobs.teamtailor("praktika")
    jobs.teamtailor("careers.trackunit.com")
    assert seen == ["https://praktika.teamtailor.com/jobs.json",
                    "https://careers.trackunit.com/jobs.json"]


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



def test_wordpress_finds_wp_job_openings_plugin(monkeypatch):
    """Плагин WP Job Openings регистрирует тип awsm_job_openings, других нет."""
    def fake_get(url, **kw):
        if "/awsm_job_openings?" in url:
            return WP_PAGE if url.endswith("&page=1") else []
        return {"code": "rest_no_route"}

    monkeypatch.setattr(jobs, "get", fake_get)
    rows = jobs.wordpress("x.test")
    assert [r[1] for r in rows] == ["Backend – Senior"]

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
            '<a class="tw-stretched-link tw-text-black" data-turbo-frame="_top"'
            ' href="/v/245310-ai-operations-lead">AI Operations Lead</a>'
            '<a class="other" href="/v/1-not-a-card">Чужая ссылка</a>')


def test_peopleforce_pages_until_nothing_new(monkeypatch):
    """Страницы по десять; вторая повторяет первую — значит список кончился."""
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        return PF_PAGE1

    monkeypatch.setattr(jobs, "get", fake_get)
    rows = jobs.peopleforce("careers.taxdome.com")
    # класс с префиксом Tailwind — та же карточка: на нём TaxDome с 2026-10-31
    assert [r[1] for r in rows] == ["Senior Devops & Engineer", "AI Operations Lead"]
    # страница списка, две страницы вакансий, вторая страница списка
    assert len(seen) == 4                       # и дальше не ходим
    url, title, where, when = rows[0][:4]
    assert url == "https://careers.taxdome.com/v/238101-senior-devops-engineer"
    assert title == "Senior Devops & Engineer"




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


def test_peopleforce_on_own_subdomain(monkeypatch):
    """Витрина на поддомене PeopleForce: адрес <клиент>.peopleforce.io/careers,
    ссылки на вакансии с префиксом /careers/v/."""
    assert jobs.from_url("https://acme.peopleforce.io/careers") == \
        ("peopleforce", "acme.peopleforce.io/careers")
    listing = ('<a class="tw-stretched-link tw-text-black" data-turbo-frame="_top" '
               'href="/careers/v/42-backend-engineer">Backend Engineer\n</a>')
    pages = {"https://acme.peopleforce.io/careers?page=1": listing,
             "https://acme.peopleforce.io/careers/v/42-backend-engineer":
                 "<h1>Backend Engineer</h1><p>Go</p>"}
    monkeypatch.setattr(jobs, "get", lambda url, **kw: pages.get(url))
    rows = jobs.peopleforce("acme.peopleforce.io/careers")
    assert [(r[0], r[1]) for r in rows] == [
        ("https://acme.peopleforce.io/careers/v/42-backend-engineer", "Backend Engineer")]
    assert rows[0][6] == "Backend Engineer Go"


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


def test_long_empty_board_closes_leftover_jobs(monkeypatch):
    """Пустой список не закрывал ничего, и последняя вакансия AppFollow висела
    открытой три недели после снятия. Пустая дольше двух суток — закрываем;
    свежая пустота — по-прежнему сбой API, вакансию не трогаем."""
    import storage
    conn = storage.connect()
    for name, ok in (("Давно пусто", "2026-10-30T08:00:00+00:00"),
                     ("Только что", storage.now())):
        conn.execute("INSERT OR REPLACE INTO companies(name,page_url,ats,slug,last_ok) "
                     "VALUES(?,?,'lever','x',?)", (name, f"https://{len(name)}.test", ok))
        conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,source,first_seen) "
                     "VALUES(?,?,'Backend','lever','2026-10-01')", (f"https://j.test/{name}", name))
    conn.commit()
    monkeypatch.setattr(jobs, "db", lambda: conn)
    monkeypatch.setitem(jobs.ADAPTERS, "lever", lambda s: [])
    jobs.run(only="Давно пусто")
    jobs.run(only="Только что")
    shut = dict(conn.execute("SELECT company, closed_at FROM jobs WHERE url LIKE 'https://j.test/%'"))
    assert shut["Давно пусто"] is not None
    assert shut["Только что"] is None


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
    row = conn.execute("SELECT closed_at, reopened_at FROM jobs "
                       "WHERE url='https://back.test/j1'").fetchone()
    assert row[0] is None and row[1]           # отметка нужна суточной сводке
    assert new == []                           # не новая: человек её уже видел


def test_wp_places_reads_the_city_only_for_new_jobs(monkeypatch):
    """Город у BNP есть только в шапке страницы. Известной вакансии он уже
    записан, и лишний запрос к сайту ничего не добавит."""
    seen = []
    card = ('<p class="crd__reference">Reference111</p><ul class="crd__specs">'
            '<li><span>Permanent</span></li>'
            '<li><svg class="icon icon-location"><use href="#icon-location">'
            '</use></svg><span>PT-Porto-Porto</span></li></ul>')
    monkeypatch.setattr(jobs, "get", lambda url, **kw: seen.append(url) or card)
    rows = jobs.wp_places(
        [("https://x.test/jobs/a/", "PO", "", None, None, "", "текст"),
         ("https://x.test/jobs/b/", "BA", "", None, None, "", "текст"),
         ("https://x.test/jobs/c/", "QA", "Lisboa", None, None, "", "текст")],
        {"https://x.test/jobs/b/"})
    assert seen == ["https://x.test/jobs/a/"]
    assert [r[2] for r in rows] == ["PT-Porto-Porto", "", "Lisboa"]


BB_LIST = {"meta": {"totalCount": 2}, "result": [
    {"id": "205", "jobOpeningName": " Backend Lead ",
     "atsLocation": {"city": None, "country": None}, "location": {"city": None},
     "isRemote": True},
    {"id": "274", "jobOpeningName": "Customer Care",
     "atsLocation": {"city": "Warsaw", "country": "Poland"}, "isRemote": None}]}
BB_DETAIL = {"meta": {}, "result": {"jobOpening": {
    "datePosted": "2026-06-01", "description": "<p>Python и <b>Go</b></p>"}}}


def test_bamboohr_reads_list_and_cards(monkeypatch):
    """Список без текста, текст — отдельной карточкой. Локации в списке часто
    пустые: у StellarTech они null во всех вакансиях, кроме одной."""
    monkeypatch.setattr(jobs, "get",
                        lambda url, **kw: BB_DETAIL if "/detail" in url else BB_LIST)
    rows = jobs.bamboohr("stellartech")
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("https://stellartech.bamboohr.com/careers/205", "Backend Lead", "Remote", "2026-06-01"),
        ("https://stellartech.bamboohr.com/careers/274", "Customer Care", "Warsaw, Poland", "2026-06-01")]
    assert "Python и Go" in rows[0][6]
    monkeypatch.setattr(jobs, "get", lambda url, **kw: {"meta": {}, "result": []})
    assert jobs.bamboohr("stellartech") is None


LD_PAGE = """<html><script type="application/ld+json">[
 {"@type":"JobPosting","title":" SRE ","url":"https://x.test/j/1",
  "datePosted":"2026-05-11","jobLocationType":"TELECOMMUTE",
  "jobLocation":{"address":{"addressLocality":"Limassol","addressCountry":"CY"}},
  "applicantLocationRequirements":[{"name":"Serbia"},{"name":"Poland"}],
  "description":"<p>Go и <b>k8s</b></p>"},
 {"@type":"JobPosting","title":"Офис","url":"https://x.test/j/2",
  "jobLocation":{"address":{"addressLocality":"Warsaw","addressCountry":"PL"}},
  "description":"текст"},
 {"@type":"Organization","name":"не вакансия"}]</script></html>"""


def test_jsonld_prefers_hiring_countries_for_remote():
    """Удалённую вакансию выдаёт не адрес офиса: у SDG в jobLocation стоит
    Лимасол, а нанимают в семи странах. Офисная локацию сохраняет."""
    rows = jobs.jsonld_jobs(LD_PAGE)
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("https://x.test/j/1", "SRE", "Remote: Serbia, Poland", "2026-05-11"),
        ("https://x.test/j/2", "Офис", "Warsaw, PL", None)]
    assert "Go и k8s" in rows[0][6]
    assert jobs.jsonld_jobs("<html>без разметки</html>") == []
    assert jobs.jsonld_jobs('<script type="application/ld+json">не json</script>') == []


def test_lever_empty_board_is_empty_not_broken(monkeypatch):
    """Пустой список на обоих хостах — у компании нет вакансий: [] вместо None, иначе
    отчёт пишет «сменился формат». Молчание хостов остаётся None."""
    monkeypatch.setattr(jobs, "get", lambda u, **k: [])
    assert jobs.lever("appfollow") == []
    monkeypatch.setattr(jobs, "get", lambda u, **k: None)
    assert jobs.lever("appfollow") is None
    eu = [{"hostedUrl": "https://jobs.eu.lever.co/x/1", "text": "Dev", "descriptionPlain": "т"}]
    monkeypatch.setattr(jobs, "get", lambda u, **k: eu if "api.eu." in u else [])
    assert [r[1] for r in jobs.lever("coinspaid")] == ["Dev"]


def test_revolut_builds_url_and_fetches_only_missing_texts(monkeypatch):
    """Список без текста, карточку тянем только тем, у кого текста ещё нет."""
    asked = []

    def js(expr):
        if expr is jobs.REVOLUT_LIST:
            return [{"id": "aa57", "text": "Credit Manager (Policy)", "where": "New Zealand - Remote"},
                    {"id": "bb01", "text": "Backend Engineer (Java)", "where": "Portugal - Remote"}]
        asked.append(expr)
        return {"backend-engineer-java-bb01": "<p>Строим банк</p>"}

    monkeypatch.setattr(jobs, "in_browser", lambda url, work: work(js))
    old = "https://www.revolut.com/careers/position/credit-manager-policy-aa57/"
    rows = jobs.revolut("www.revolut.com", described={old})
    assert [r[0] for r in rows] == [old, "https://www.revolut.com/careers/position/backend-engineer-java-bb01/"]
    assert rows[1][1:3] == ("Backend Engineer (Java)", "Portugal - Remote")
    assert (rows[0][6], rows[1][6]) == (None, "Строим банк")
    assert len(asked) == 1 and "credit-manager" not in asked[0]
    assert jobs.from_url("https://www.revolut.com/en-PT/careers/") == ("revolut", "www.revolut.com")


def test_get_survives_incomplete_read(monkeypatch):
    """Обрыв ответа на середине — http.client.IncompleteRead, он не OSError.
    Раньше он пролетал мимо get() и ронял весь обход, а не одну компанию."""
    import http.client

    class Cut:
        status = 200

        def read(self):
            raise http.client.IncompleteRead(b"{\"jobs\": [", 70842)

    calls = []
    monkeypatch.setattr(jobs.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1) or Cut())
    monkeypatch.setattr(jobs, "space_out", lambda u: None)
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    assert jobs.get("https://example.com/api") is None
    assert len(calls) == 2                       # один повтор, как при сетевой ошибке
    assert jobs.post_json("https://example.com/graphql", {}) is None


def _flight_page(chunks):
    """Страница Next.js: поток flight кусками в self.__next_f.push."""
    import json
    return "".join(f'<script>self.__next_f.push([1,{json.dumps(c)}])</script>' for c in chunks)


MICRO1_LIST = {"total": 3, "data": [
    {"job_id": "aaaaaaaa-0000-0000-0000-000000000001", "job_name": "Director of Engineering",
     "apply_url": "https://jobs.micro1.ai/post/aaaaaaaa-0000-0000-0000-000000000001?utm_source=micro1",
     "location_type": "remote", "date_posted": "2026-11-12 10:00:00",
     "ideal_yearly_compensation": {"min": 450000, "max": 800000},
     "ideal_hourly_rate": None, "is_micro1_account": True},
    {"job_id": "aaaaaaaa-0000-0000-0000-000000000002", "job_name": "Research Headhunter",
     "apply_url": "https://jobs.micro1.ai/post/aaaaaaaa-0000-0000-0000-000000000002",
     "location_type": None, "date_posted": "2026-11-02 23:23:35",
     "ideal_yearly_compensation": None, "ideal_hourly_rate": {"min": 80, "max": 90},
     "is_micro1_account": True},
    {"job_id": "aaaaaaaa-0000-0000-0000-000000000003", "job_name": "Client Role",
     "apply_url": "https://jobs.micro1.ai/post/aaaaaaaa-0000-0000-0000-000000000003",
     "is_micro1_account": False},
]}


def test_micro1_reads_portal_api_and_job_text(monkeypatch):
    """micro1 — свой портал: список из POST в их API, текст во flight-потоке
    страницы вакансии. Чужие (клиентские) вакансии и известный текст не берём."""
    import jobs
    posts, gets = [], []

    def fake_post(url, payload, ua=None):
        posts.append((url, payload))
        return MICRO1_LIST

    body = "<p>Own the platform. Go, Kubernetes.</p>"
    page = _flight_page(['0:{"job_description":"$1f","job_tags":["Core team"]}\n',
                         f"1f:T{len(body.encode()):x},{body}"])

    def fake_get(url, want_json=True, **k):
        gets.append(url)
        return page

    monkeypatch.setattr(jobs, "post_json", fake_post)
    monkeypatch.setattr(jobs, "get", fake_get)
    known = {"https://jobs.micro1.ai/post/aaaaaaaa-0000-0000-0000-000000000002"}
    got = jobs.micro1("micro1", have_text=known)
    assert len(posts) == 1 and posts[0][1]["action"] == "get_all_jobs"
    assert [g[1] for g in got] == ["Director of Engineering", "Research Headhunter"]
    first, second = got
    assert first[0] == "https://jobs.micro1.ai/post/aaaaaaaa-0000-0000-0000-000000000001"
    assert first[2] == "Remote" and first[3] == "2026-11-12"
    assert first[4] == "450000–800000 USD год" and second[4] == "80–90 USD час"
    assert "Go, Kubernetes" in first[6] and second[6] is None
    assert gets == [first[0]]


def test_micro1_from_url():
    import jobs
    assert jobs.from_url("https://www.micro1.ai/careers") == ("micro1", "micro1")
    assert jobs.from_url("https://jobs.micro1.ai/post/x") == ("micro1", "micro1")
    assert jobs.from_url("https://micro1.ai.example.com/") is None


BREEZY_LIST = [
    {"id": "a1", "name": "Senior Engineering Manager",
     "url": "https://acme.breezy.hr/p/a1-senior-engineering-manager",
     "published_date": "2026-11-20T19:55:30.897Z", "salary": "",
     "location": {"name": "Uzbekistan", "is_remote": True},
     "locations": [{"name": "Uzbekistan", "is_remote": True},
                   {"name": "Georgia", "is_remote": True},
                   {"name": "Armenia", "is_remote": True}],
     "company": {"name": "Acme Dispatch"}},
    {"id": "b2", "name": "Support Specialist",
     "url": "https://acme.breezy.hr/p/b2-support-specialist",
     "published_date": "2026-11-23T16:45:22.378Z", "salary": "$45,000 – $50,000 / year",
     "location": {"name": "Kansas City", "is_remote": False},
     "company": {"name": "Acme Dispatch"}},
]


def test_breezy_reads_json_and_job_text(monkeypatch):
    """Breezy: список из открытого JSON, место работы — все locations, текст —
    schema.org со страницы вакансии и только у тех, у кого его ещё нет."""
    import jobs
    page = ('<script type="application/ld+json">{"@type": "JobPosting", '
            '"url": "https://acme.breezy.hr/p/a1-senior-engineering-manager?source=GoogleJobs", '
            '"title": "Senior Engineering Manager", "datePosted": "2026-11-02", '
            '"description": "<p>Lead Platform and Integrations. Go, Kubernetes.</p>"}</script>')
    gets = []

    def fake_get(url, want_json=True, **k):
        gets.append(url)
        return BREEZY_LIST if url == "https://acme.breezy.hr/json" else page

    monkeypatch.setattr(jobs, "get", fake_get)
    got = jobs.breezy("acme", described={BREEZY_LIST[1]["url"]})
    first, second = got
    assert first[:5] == (BREEZY_LIST[0]["url"], "Senior Engineering Manager",
                         "Uzbekistan · Georgia · Armenia, Remote", "2026-11-20", None)
    assert "Go, Kubernetes" in first[6]
    assert second[2] == "Kansas City" and second[4] == "$45,000 – $50,000 / year"
    assert second[6] is None
    assert gets == ["https://acme.breezy.hr/json", BREEZY_LIST[0]["url"]]

    # разведка по имени страниц не качает
    gets.clear()
    assert len(jobs.breezy("acme")) == 2 and gets == ["https://acme.breezy.hr/json"]
    assert jobs.board_owner("breezy", "acme") == "Acme Dispatch"


def test_breezy_missing_board_and_url(monkeypatch):
    import jobs
    monkeypatch.setattr(jobs, "get", lambda url, want_json=True, **k: None)
    assert jobs.breezy("nobody") is None
    assert jobs.from_url("https://superdispatch.breezy.hr/") == ("breezy", "superdispatch")
    assert jobs.from_url("https://superdispatch.breezy.hr/p/9394767b433e-x") == ("breezy", "superdispatch")


def _deel_row(jid, title, places, shown=True, listed=True):
    import json
    return json.dumps({"id": 0, "attributes": {
        "ashby_id": jid, "title": title, "location_name": places[0],
        "all_locations": places, "is_listed": listed,
        "ashby_published_date": "2026-11-20T09:45:25.919Z",
        "compensation_tier_summary": "$$48,000 - $75,000 USD",
        "should_display_compensation_on_Job_board": shown}}, separators=(",", ":"))


def test_deel_reads_careers_flight_and_job_text(monkeypatch):
    """Deel: список во flight-потоке www.deel.com/careers, текст — schema.org
    на jobs.deel.com. Все страны, вилка только где её разрешено показывать,
    скрытые и уже прочитанные вакансии страниц не тянут."""
    import jobs
    a, b, c = ("aaaaaaaa-0000-0000-0000-00000000000%d" % i for i in (1, 2, 3))
    blob = ('{"jobs":[' + ",".join([
        _deel_row(a, "Applied AI Engineer", ["United Kingdom", "Spain", "Portugal"]),
        _deel_row(b, "Account Executive", ["Sweden"], shown=False),
        _deel_row(c, "Hidden", ["Spain"], listed=False),
        _deel_row(a, "Applied AI Engineer", ["United Kingdom"])]) + ']}')
    careers = _flight_page([blob[:300], blob[300:]])
    job = ('<script type="application/ld+json">{"@type": "JobPosting", '
           '"url": "x", "title": "Applied AI Engineer", '
           '"description": "<p>LLM systems in Python.</p>"}</script>')
    gets = []

    def fake_get(url, want_json=True, **k):
        gets.append(url)
        return careers if url == "https://www.deel.com/careers/" else job

    monkeypatch.setattr(jobs, "get", fake_get)
    url_a, url_b = (jobs.DEEL_JOB.format(x) for x in (a, b))
    got = jobs.deel("www.deel.com", have_text={url_b})
    assert [g[1] for g in got] == ["Applied AI Engineer", "Account Executive"]
    first, second = got
    assert first[:5] == (url_a, "Applied AI Engineer", "United Kingdom · Spain · Portugal",
                         "2026-11-20", "$48,000 - $75,000 USD")
    assert "Python" in first[6] and second[4] is None and second[6] is None
    assert gets == ["https://www.deel.com/careers/", url_a]

    # разведка страниц вакансий не качает, пустая витрина — не «всё закрыто»
    gets.clear()
    assert len(jobs.deel("www.deel.com")) == 2 and len(gets) == 1
    monkeypatch.setattr(jobs, "get", lambda url, want_json=True, **k: "<html></html>")
    assert jobs.deel("www.deel.com") is None
    assert jobs.from_url("https://www.deel.com/careers/") == ("deel", "www.deel.com")


def test_embedded_unescapes_href(monkeypatch):
    """Artezio на hh.ru: в href «&amp;». Модель отвечает адресом с «&», и
    паспорт с оценкой не находили вакансию — запрос повторялся каждый проход."""
    import jobs
    html = ('<a href="https://hh.ru/vacancy/139056402?from=a&amp;hhtmFrom=employer">'
            '<span>QA-инженер</span><span>Москва</span></a>')
    monkeypatch.setattr(jobs, "get", lambda url, want_json=True, **k: html)
    got = jobs.embedded("https://hh.ru/employer/733")
    assert got and got[0][0] == "https://hh.ru/vacancy/139056402?from=a&hhtmFrom=employer", got


def test_slug_jobs_last_resort(monkeypatch):
    """Huntflow и Webflow дают вакансии слагом без id: /vacancy/qa-lead.
    Пункт меню под тем же сегментом и одиночная ссылка вакансией не считаются,
    кнопка «Apply» берёт название из заголовка карточки (CVAT)."""
    import jobs
    page = ('<a href="/careers/join-us">Join us</a>'
            '<h2>QA Lead</h2><p>Remote</p><a href="/vacancy/qa-lead">Apply</a>'
            '<a href="/vacancy/senior-product-analyst-3"><span>Senior Product Analyst</span>'
            '<span>Remote</span></a>')
    monkeypatch.setattr(jobs, "get", lambda url, want_json=True, **k: page)
    got = jobs.embedded("https://x.huntflow.io/")
    assert [(r[0], r[1]) for r in got] == [
        ("https://x.huntflow.io/vacancy/qa-lead", "QA Lead"),
        ("https://x.huntflow.io/vacancy/senior-product-analyst-3", "Senior Product Analyst")], got
    one = '<a href="/vacancy/qa-lead">QA Lead</a><a href="/careers/join-us">Join us</a>'
    monkeypatch.setattr(jobs, "get", lambda url, want_json=True, **k: one)
    assert not jobs.embedded("https://x.huntflow.io/")


def test_long_retry_after_bans_host(monkeypatch):
    """Workable 2026-11-27: 429 с Retry-After 63627. Бот повторял через 3 с и
    стучался каждый обход. Теперь хост молчит до срока, срок переживает процесс."""
    import io, json, urllib.error, jobs
    calls = []

    def boom(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 429, "x", {"Retry-After": "63627"}, io.BytesIO())
    monkeypatch.setattr(jobs.urllib.request, "urlopen", boom)
    monkeypatch.setattr(jobs, "_bans", {})
    url = "https://apply.workable.com/api/v1/widget/accounts/joom?details=true"
    assert jobs.get(url, with_code=True) == (None, 429) and len(calls) == 1
    assert jobs.workable("zyte") is None and len(calls) == 1
    assert "apply.workable.com" in json.loads(jobs.BANS.read_text())
    assert jobs.diagnose("workable", "zyte", "").startswith("HTTP 429, бан до")


def test_workable_empty_account_no_suffix_probe(monkeypatch):
    import jobs
    seen = []

    def fake(url, want_json=True, with_code=False, **k):
        seen.append(url)
        return ({"name": "Hospitable", "jobs": []}, 200)
    monkeypatch.setattr(jobs, "get", fake)
    assert jobs.workable("hospitable", probe=False) == [] and len(seen) == 1
    assert jobs.collect("Hospitable", "", {"Hospitable": ("workable", "hospitable")})[3] == []
    assert len(seen) == 2
