import jobs


def test_slug_from_url_beats_guessing():
    """Где есть ссылка, угадывать нечего — и ошибиться нельзя."""
    assert jobs.from_url("https://jobs.ashbyhq.com/constructor") == ("ashby", "constructor")
    assert jobs.from_url("https://job-boards.greenhouse.io/nebius") == ("greenhouse", "nebius")
    assert jobs.from_url("https://jobs.lever.co/appfollow") == ("lever", "appfollow")
    assert jobs.from_url("https://praktika.teamtailor.com/jobs") == ("teamtailor", "praktika")
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
