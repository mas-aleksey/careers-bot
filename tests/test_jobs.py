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
