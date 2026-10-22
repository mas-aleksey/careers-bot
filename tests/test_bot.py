import json, sqlite3
import bot, storage


def fresh_db():
    c = sqlite3.connect(":memory:")
    c.executescript(storage.SCHEMA)
    return c


def test_job_card_shows_age_and_contact():
    c = fresh_db()
    c.execute("INSERT INTO jobs(url,company,title,location,posted,contact) "
              "VALUES('http://x','Alpaca','Senior Go','Remote','2024-12-01','Ann Lee')")
    card = bot.job_card_db(c, "http://x", 91, "почему берём")
    assert '<a href="http://x">Senior Go — Alpaca</a>' in card
    assert "<b>Совпадение 91%</b>" in card
    assert "висит" in card and "Ann Lee" in card
    assert "стек" not in card      # разбивка баллов остаётся вне чата


def test_profile_rendered_without_service_fields():
    c = fresh_db()
    c.execute("INSERT INTO profiles(tg_id,data,min_match,notify,updated_at) "
              "VALUES(1,?,80,'daily','now')",
              (json.dumps({"name": "Тест", "role": "Senior Backend",
                           "stack": ["Go", "Python"]}),))
    r = bot.render_db_profile(c, 1)
    assert "<b>Роль:</b> Senior Backend" in r and "Go, Python" in r
    assert "от 80%" in r and "tg_id" not in r


def test_access_admin_first_then_invite_only():
    c = fresh_db()
    ok, reply = bot.check_access(c, 111, "owner", "/start")
    assert ok and c.execute("SELECT is_admin FROM users WHERE tg_id=111").fetchone()[0] == 1
    assert bot.check_access(c, 222, "stranger", "/start")[0] is False
    assert bot.check_access(c, 222, "stranger", "/start wrongcode")[0] is False


def test_invite_code_works_once():
    c = fresh_db()
    bot.check_access(c, 111, "owner", "/start")
    exp = "2999-01-01T00:00:00+00:00"
    c.execute("INSERT INTO invites(code,created_by,created_at,expires_at) VALUES('good',111,'now',?)", (exp,))
    assert bot.check_access(c, 222, "guest", "/start good")[0] is True
    assert bot.check_access(c, 333, "third", "/start good")[0] is False


def test_expired_invite_rejected():
    c = fresh_db()
    bot.check_access(c, 111, "owner", "/start")
    c.execute("INSERT INTO invites(code,created_by,created_at,expires_at) "
              "VALUES('stale',111,'now','2000-01-01T00:00:00+00:00')")
    assert bot.check_access(c, 444, "late", "/start stale")[0] is False


def test_hiring_contact_expands_email():
    assert bot.who_hires("roberts.bendins@tabby.ai") == "Roberts Bendins · roberts.bendins@tabby.ai"
    assert bot.who_hires("Shahida Sayes") == "Shahida Sayes"
    assert bot.who_hires("") is None


def test_menu_and_handlers_match():
    """Команда в меню без обработчика = молчание в ответ."""
    named = {c for c, _ in bot.COMMANDS} | {c for c, _ in bot.ADMIN_COMMANDS}
    have = {c.lstrip("/") for c in list(bot.HANDLERS) + list(bot.ADMIN_HANDLERS)} | {"cancel"}
    assert named <= have, named - have


def test_admin_commands_are_separate():
    assert not set(bot.HANDLERS) & set(bot.ADMIN_HANDLERS)


def test_html_escaped_in_cards():
    assert bot.esc("A & B <c>") == "A &amp; B &lt;c&gt;"


def test_spread_scores_whole_duplicate_group():
    """Оценили одну из группы — получили все. Иначе девять братьев остаются
    неоценёнными и уходят в следующий платный проход."""
    import bot, storage
    conn = storage.connect()
    for n, loc in enumerate(("Remote Spain", "Remote Germany", "Remote UK")):
        conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,location,dedup) VALUES(?,?,?,?,?)",
                     (f"https://t/{n}", "Mozilla", "SWE, Add-Ons", loc,
                      storage.dedup_key("Mozilla", "SWE, Add-Ons")))
    conn.commit()

    bot.spread(conn, 777, "https://t/0", 84, "подходит")
    conn.commit()

    got = conn.execute("SELECT job_url, pct FROM matches WHERE tg_id=777 ORDER BY job_url").fetchall()
    assert got == [("https://t/0", 84), ("https://t/1", 84), ("https://t/2", 84)], got


SENIOR = {"level": "senior", "location": "Порту, Португалия", "timezone": "Europe/Lisbon",
          "relocation": "нет, Португалию не покидает", "hybrid": "да, 2 дня в неделю в Порту"}


def gated(title, loc, profile=None):
    return bot.gate_reason(profile or SENIOR, title, loc) is not None


def test_gate_keeps_unknown_geography():
    """Пустая локация и голый Remote — не блокер: у BNP страна только в тексте."""
    assert not gated("Senior Backend Engineer", "")
    assert not gated("Senior Backend Engineer", None)
    assert not gated("Senior Backend Engineer", "Remote")
    assert not gated("Senior Backend Engineer", "Anywhere")


def test_gate_cuts_foreign_only_locations():
    assert gated("Senior Backend Engineer", "Remote US")
    assert gated("Senior Backend Engineer", "New York, NY. Remote (US only)")
    assert gated("Senior Backend Engineer", "Bengaluru - India, Remote")
    assert gated("Senior Backend Engineer", "GMT-6")
    # Tabby пишет Саудовскую Аравию аббревиатурой, полное имя в строке не стоит
    assert gated("Senior DevOps Engineer", "KSA, Onsite")
    # код страны отдельным словом, без слова Remote рядом
    assert gated("Senior Backend Engineer", "Kansas City, US")
    assert gated("Senior Backend Engineer", "US - California")
    # границы слова: страна внутри другого слова — не Штаты
    assert not gated("Senior Backend Engineer", "Belarus")
    assert not gated("Senior Backend Engineer", "Aarhus, Denmark")


def test_gate_keeps_multicountry_with_one_match():
    """Строка с десятком стран проходит, если среди них есть подходящая."""
    assert not gated("Senior Backend Engineer", "Remote - EMEA; United States")
    assert not gated("Senior Backend Engineer", "Remote: Portugal, Poland, Spain")
    assert not gated("Senior Backend Engineer", "Amsterdam, Netherlands; Remote")
    assert not gated("Senior Backend Engineer", "Porto")


def test_gate_grade_only_cuts_the_bottom():
    """Middle senior-кандидату подойти может, стажировка — нет."""
    assert gated("Junior Backend Engineer", "Porto")
    assert gated("Backend Engineer Intern", "Porto")
    assert not gated("Middle Backend Engineer", "Porto")
    assert not gated("Middle/Senior Java Developer", "Porto")


def test_gate_grade_off_for_non_senior_profile():
    """У junior и middle опасное направление обратное — ворота молчат."""
    assert not gated("Junior Backend Engineer", "Porto", dict(SENIOR, level="middle"))
    assert not gated("Junior Backend Engineer", "Porto", dict(SENIOR, level=""))


def test_gate_off_when_relocation_allowed():
    movable = dict(SENIOR, relocation="да, готов к переезду")
    assert not gated("Senior Backend Engineer", "Remote US", movable)


def test_gate_reason_names_the_match():
    """Молчаливый отказ не отследить при ручном разборе."""
    # в причине стоит найденное слово, а не вся строка локации
    assert "«US»" in bot.gate_reason(SENIOR, "Senior Backend Engineer", "Remote US")
    assert "Junior" in bot.gate_reason(SENIOR, "Junior Backend Engineer", "Porto")


def test_score_prompt_allows_middle():
    """Senior-кандидату middle подойти может: блокер только junior и стажировки."""
    assert "junior-позиция, стажировка или trainee" in bot.SCORE_SYSTEM
    assert "уровень ниже, чем у кандидата" not in bot.SCORE_SYSTEM
    assert "Middle и middle+" in bot.SCORE_SYSTEM


SCORE_ROWS = [("u1", "BNP", "Senior Python Developer", "", "Python, SQL, Portugal, hybrid")]


def test_score_batch_sends_text_and_keeps_where_stack(monkeypatch):
    """Текст идёт в запрос, а where и stack попадают в обоснование карточки."""
    seen = {}
    import llm

    def fake(system, user, **kw):
        seen["user"] = user
        return {"scores": [{"url": "u1", "pct": 85, "where": "Portugal, hybrid",
                            "why": "Совпали Python и SQL, домен банковский."}]}

    monkeypatch.setattr(llm, "ask_json", fake)
    (url, pct, why), = bot.score_batch({"role": "backend"}, SCORE_ROWS)
    assert "текст вакансии" in seen["user"] and "Python, SQL, Portugal" in seen["user"]
    assert (url, pct) == ("u1", 85)
    assert "Совпали Python и SQL" in why and "Portugal, hybrid" in why


def test_score_prompt_caps_why():
    """Выходные токены впятеро дороже входных — длинное why стоит денег."""
    assert "НЕ БОЛЬШЕ 400 символов" in bot.SCORE_SYSTEM
    assert "страну найма\nищи в тексте" in bot.SCORE_SYSTEM


def test_spread_does_not_drag_office_rows_through():
    """Ключ дедупликации без локации: «remote» и «San Francisco» в одной группе."""
    import storage
    conn = storage.connect()
    try:
        for u, loc in (("sp-remote", "remote"), ("sp-sf", "San Francisco")):
            conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,location,dedup) "
                         "VALUES(?,?,?,?,?)", (u, "Clera", "Senior Software Engineer",
                                               loc, "clera|seniorsoftwareengineer"))
        bot.spread(conn, 1, "sp-remote", 72, "подходит", profile=SENIOR)
        got = dict(conn.execute("SELECT job_url, pct FROM matches WHERE tg_id=1"))
        assert got["sp-remote"] == 72
        assert got["sp-sf"] == 0               # офис не наследует балл удалённой
        why = conn.execute("SELECT why FROM matches WHERE job_url='sp-sf'").fetchone()[0]
        assert "San Francisco" in why

        # Отказ по-прежнему разносится на всю группу: платить за дубли незачем.
        bot.spread(conn, 1, "sp-remote", 0, "другая профессия", profile=SENIOR)
        after = dict(conn.execute("SELECT job_url, pct FROM matches WHERE tg_id=1"))
        assert after == {"sp-remote": 0, "sp-sf": 0}
    finally:
        conn.execute("DELETE FROM matches WHERE tg_id=1")
        conn.execute("DELETE FROM jobs WHERE url LIKE 'sp-%'")
        conn.commit()
        conn.close()


def test_score_batch_truncates_long_why(monkeypatch):
    """Модель может не уложиться в лимит — обрезаем на своей стороне."""
    import llm
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: {"scores": [
        {"url": "u1", "pct": 50, "why": "я" * 900, "where": "П" * 200}]})
    (_, _, why), = bot.score_batch({"role": "backend"}, SCORE_ROWS)
    head, where = why.split("\n")
    assert len(head) == bot.WHY_CHARS
    assert len(where) == bot.WHERE_CHARS


def test_score_prompt_asks_stack_inside_why():
    """Отдельного поля stack нет: технологии называются внутри why."""
    assert '"stack"' not in bot.SCORE_SYSTEM
    assert "технологии из обязательных требований" in bot.SCORE_SYSTEM


def test_rescore_spares_what_was_already_sent():
    """Отправленное человек видел — второй раз не придёт, переоценивать нечего.
    Остальное снимаем и помечаем как доборку истории."""
    import storage
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO profiles(tg_id,data,updated_at) "
                 "VALUES(7,'{}','2026-10-06')")
    for url in ("https://j/sent", "https://j/fresh"):
        conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,source,first_seen,dedup) "
                     "VALUES(?,'Acme','Backend','lever','2026-10-01',?)", (url, url))
        conn.execute("INSERT OR REPLACE INTO matches(tg_id,job_url,pct,why,scored_at) "
                     "VALUES(7,?,80,'старая оценка','2026-10-01')", (url,))
    conn.execute("INSERT OR REPLACE INTO sent(tg_id,job_url,sent_at,dedup) "
                 "VALUES(7,'https://j/sent','2026-10-02','https://j/sent')")
    conn.commit()
    bot.SCORE_WAKE.clear()
    bot.rescore(conn, 7)
    left = {u for (u,) in conn.execute("SELECT job_url FROM matches WHERE tg_id=7")}
    assert left == {"https://j/sent"}
    assert conn.execute("SELECT 1 FROM meta WHERE key=?",
                        (bot.backlog_key(7),)).fetchone()        # доборка назначена
    assert bot.SCORE_WAKE.is_set()             # ждать таймера не из чего


def test_backlog_pass_pays_only_for_the_top(monkeypatch):
    """Верхушка идёт к дорогой модели, остальным сразу ноль с числом совпавших
    тегов — иначе обычный цикл подберёт их как неоценённые и заплатит за каждую."""
    import storage, llm
    monkeypatch.setattr(bot, "RESCORE_TOP", 2)
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO profiles(tg_id,data,updated_at) VALUES"
                 "(8,'{\"level\":\"senior\",\"relocation\":\"да\"}','2026-10-06')")
    for i, title in enumerate(("Senior Go Engineer", "Senior Python Engineer",
                               "Senior Java Engineer", "Senior Ruby Engineer")):
        conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,location,source,"
                     "first_seen,dedup,description) VALUES(?,'Acme',?,'Porto','lever',"
                     "'2026-10-01',?,'текст')", (f"https://j/{i}", title, f"d{i}"))
    conn.commit()
    # соседние тесты пишут в ту же базу: закрываем им очередь, чтобы верхушка
    # считалась только по четырём вакансиям этого теста
    conn.execute("INSERT OR REPLACE INTO matches(tg_id,job_url,pct,why,scored_at) "
                 "SELECT 8, url, 0, 'чужая', '2026-10-06' FROM jobs "
                 "WHERE url NOT IN ('https://j/0','https://j/1','https://j/2','https://j/3')")
    # Профиль на Go и Python: верхушку определяют теги, модель не зовут вовсе
    conn.execute("UPDATE profiles SET data=? WHERE tg_id=8",
                 ('{"level":"senior","relocation":"да","role":"Senior Backend",'
                  '"stack":["Go","Python"]}',))
    conn.commit()
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: 1/0)   # вызов = падение
    known = bot.backlog_pass(conn, 8)
    assert known == {"https://j/0", "https://j/1"}
    # в базе лежат и чужие вакансии от соседних тестов — смотрим только свои
    got = dict(conn.execute("SELECT job_url, pct FROM matches WHERE tg_id=8 "
                            "AND job_url IN ('https://j/0','https://j/1',"
                            "'https://j/2','https://j/3')"))
    assert got == {"https://j/2": 0, "https://j/3": 0}    # верхушка пока без оценки
    assert "совпадение тегов" in conn.execute(
        "SELECT why FROM matches WHERE job_url='https://j/2'").fetchone()[0]


def test_score_pending_trusts_backlog_scores(monkeypatch):
    """Верхушка доборки идёт к дорогой модели первой, не проходя отбор заново."""
    import storage, llm
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO profiles(tg_id,data,updated_at) VALUES"
                 "(11,'{\"level\":\"senior\",\"relocation\":\"да\"}','2026-10-06')")
    conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,location,source,"
                 "first_seen,dedup,description) VALUES('https://k/1','Acme',"
                 "'Senior Go Engineer','Porto','lever','2026-10-01','k1','текст')")
    # соседние тесты пишут в ту же базу: закрываем им очередь, чтобы остаться
    # с одной вакансией и видеть ровно те вызовы, которые делает доборка
    conn.execute("INSERT OR REPLACE INTO matches(tg_id,job_url,pct,why,scored_at) "
                 "SELECT 11, url, 0, 'чужая', '2026-10-06' FROM jobs "
                 "WHERE url != 'https://k/1'")
    conn.commit()
    seen = []

    def fake_ask(system, user, **kw):
        seen.append("паспорт" if system is bot.PASSPORT_SYSTEM else "sonnet")
        return {"scores": [{"url": "https://k/1", "pct": 91, "why": "подходит"}],
                "items": [{"url": "https://k/1", "work_mode": "remote",
                           "role": "backend"}]}

    monkeypatch.setattr(llm, "ask_json", fake_ask)
    monkeypatch.setattr(bot, "SCORE_SLICE", 50)
    bot.score_pending(conn, 11, prefer={"https://k/1"})
    # дешёвая модель зовётся один раз и только за паспортом, триажа больше нет
    assert seen == ["паспорт", "sonnet"]
    assert conn.execute("SELECT pct FROM matches WHERE job_url='https://k/1'"
                        ).fetchone()[0] == 91


def test_collect_due_counts_from_last_check_not_from_start():
    """Расписание обхода переживает пересборку образа: отсчёт от последней
    проверки компаний, а не от старта процесса."""
    import storage
    from datetime import datetime, timedelta, timezone
    conn = storage.connect()
    conn.execute("DELETE FROM companies")
    assert bot.collect_due_in(conn) == 0          # пустой реестр — идём сразу

    def put(hours_ago):
        when = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
        conn.execute("INSERT OR REPLACE INTO companies(name,page_url,checked_at) "
                     "VALUES('Acme','https://acme.test',?)", (when,))
        conn.commit()

    put(1)                                        # час назад — ждём около трёх
    assert 2.9 * 3600 < bot.collect_due_in(conn) <= 3 * 3600
    put(5)                                        # просрочено — идём сразу
    assert bot.collect_due_in(conn) == 0
    conn.execute("UPDATE companies SET checked_at='не дата'")
    conn.commit()
    assert bot.collect_due_in(conn) == 0           # мусор в поле не вешает цикл


def test_deliver_commits_before_the_next_send(monkeypatch):
    """Сеть внутри открытой транзакции держит базу: коммит до следующей
    отправки, а не один в конце рассылки."""
    import storage
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO users(tg_id,paused) VALUES(21,0)")
    conn.execute("INSERT OR REPLACE INTO profiles(tg_id,data,min_match,notify,updated_at) "
                 "VALUES(21,'{}',70,'instant','2026-10-06')")
    for i in (1, 2):
        conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,source,first_seen,dedup) "
                     "VALUES(?,'Acme','Backend','lever','2026-10-01',?)",
                     (f"https://d/{i}", f"dd{i}"))
        conn.execute("INSERT OR REPLACE INTO matches(tg_id,job_url,pct,why,scored_at) "
                     "VALUES(21,?,90,'подходит','2026-10-06')", (f"https://d/{i}",))
    conn.commit()
    in_txn = []
    monkeypatch.setattr(bot, "send",
                        lambda *a, **k: in_txn.append(conn.in_transaction) or True)
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    assert bot.deliver(conn) == 2
    assert in_txn == [False, False]            # обе отправки вне транзакции


def test_inherit_scores_reuses_the_verdict_for_a_new_url(monkeypatch):
    """Clera перевыпускает id в Ashby: та же вакансия возвращается под новым
    адресом. Платить за неё второй раз незачем — берём прежнюю оценку."""
    import storage, llm
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO profiles(tg_id,data,updated_at) VALUES"
                 "(31,'{\"level\":\"senior\",\"relocation\":\"да\"}','2026-10-07')")
    conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,location,source,"
                 "first_seen,dedup,closed_at) VALUES('https://a/old','Clera',"
                 "'Founding Engineer','Remote','ashby','2026-10-01','clera|fe','2026-10-07')")
    conn.execute("INSERT OR REPLACE INTO matches(tg_id,job_url,pct,why,scored_at,triage_pct) "
                 "VALUES(31,'https://a/old',77,'подходит','2026-10-01',60)")
    conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,location,source,"
                 "first_seen,dedup) VALUES('https://a/new','Clera','Founding Engineer',"
                 "'Remote','ashby','2026-10-07','clera|fe')")
    conn.commit()
    called = []
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: called.append(1) or {"scores": []})
    assert bot.inherit_scores(conn, 31, {"level": "senior", "relocation": "да"}) == 1
    assert conn.execute("SELECT pct, why FROM matches WHERE tg_id=31 AND "
                        "job_url='https://a/new'").fetchone() == (77, "подходит")
    assert called == []                        # к модели не ходили


def test_health_counts_vacancies_not_rows():
    """Одна вакансия лежит на доске под несколькими адресами, а Ashby у Clera
    ещё и перевыпускает id. По строкам отчёт завышал и приход, и уход."""
    import storage
    conn = storage.connect()
    conn.execute("DELETE FROM jobs")
    conn.execute("DELETE FROM companies")
    # одна вакансия двумя строками — приход считается один раз
    for u in ("https://h/1", "https://h/2"):
        conn.execute("INSERT INTO jobs(url,company,title,source,first_seen,dedup) "
                     "VALUES(?,'Acme','Backend','ashby',datetime('now'),'acme|backend')", (u,))
    # id перевыпустили: старая строка закрыта, живая копия осталась
    conn.execute("INSERT INTO jobs(url,company,title,source,first_seen,dedup,closed_at) "
                 "VALUES('https://h/old','Acme','Backend','ashby',datetime('now'),"
                 "'acme|backend',datetime('now'))")
    # а эта ушла с доски совсем
    conn.execute("INSERT INTO jobs(url,company,title,source,first_seen,dedup,closed_at) "
                 "VALUES('https://h/gone','Acme','Designer','ashby',datetime('now'),"
                 "'acme|designer',datetime('now'))")
    conn.commit()
    # id перевыпустили у вакансии, которая была известна и раньше: не новая
    conn.execute("INSERT INTO jobs(url,company,title,source,first_seen,dedup,closed_at) "
                 "VALUES('https://h/was','Acme','Analyst','ashby','2026-09-01',"
                 "'acme|analyst',datetime('now'))")
    conn.execute("INSERT INTO jobs(url,company,title,source,first_seen,dedup) "
                 "VALUES('https://h/again','Acme','Analyst','ashby',datetime('now'),"
                 "'acme|analyst')")
    conn.commit()
    # вернувшаяся из закрытых: в приход не попадает, из ухода выбывает
    conn.execute("INSERT INTO jobs(url,company,title,source,first_seen,dedup,reopened_at) "
                 "VALUES('https://h/back','Acme','SRE','ashby','2026-09-01',"
                 "'acme|sre',datetime('now'))")
    conn.commit()
    text = bot.health_report(conn)
    assert "+2 вакансий · закрылось 1 · вернулось 1" in text
    # разбор по компаниям считает теми же правилами, что и шапка
    assert "· Acme: +2 / −1" in text


# --- паспорт вакансии: блокеры из текста ------------------------------------

def psp(**kw):
    base = {"hire": [], "work_mode": "not-stated", "office_city": "",
            "levels": [], "uncertain": []}
    return dict(base, **kw)


def test_passport_cuts_foreign_hiring_when_field_is_empty():
    """Дыра, из-за которой 2026-10-04 выпали Mozilla и BNP: страна только в тексте."""
    assert not bot.gate_reason(SENIOR, "Senior Backend Engineer", "")
    assert bot.gate_reason(SENIOR, "Senior Backend Engineer", "", psp(hire=["US"]))
    assert bot.gate_reason(SENIOR, "Senior Backend Engineer", "Remote",
                           psp(hire=["AMERICAS"]))


def test_passport_keeps_europe_and_supranational():
    for h in (["PT"], ["EUROPE"], ["EMEA"], ["WORLDWIDE"], ["US", "DE"]):
        assert not bot.gate_reason(SENIOR, "Senior Backend Engineer", "", psp(hire=h))


def test_passport_silent_when_unsure_or_empty():
    """«Не знаю» — не блокер: ложный отказ дороже лишней оценки."""
    assert not bot.gate_reason(SENIOR, "Senior Backend Engineer", "", psp(hire=[]))
    assert not bot.gate_reason(SENIOR, "Senior Backend Engineer", "",
                               psp(hire=["US"], uncertain=["hire"]))
    assert not bot.gate_reason(SENIOR, "Senior Backend Engineer", "",
                               psp(work_mode="onsite", uncertain=["work_mode"]))


def test_passport_work_mode():
    assert bot.gate_reason(SENIOR, "Senior Backend Engineer", "",
                           psp(work_mode="onsite", office_city="berlin"))
    # гибрид в Порту профилю подходит, в Копенгагене — нет
    assert not bot.gate_reason(SENIOR, "Senior Backend Engineer", "",
                               psp(work_mode="hybrid", office_city="porto"))
    assert bot.gate_reason(SENIOR, "Senior Backend Engineer", "",
                           psp(work_mode="hybrid", office_city="copenhagen"))


def test_passport_levels_only_cut_the_bottom():
    assert bot.gate_reason(SENIOR, "Software Engineer", "", psp(levels=["junior"]))
    assert not bot.gate_reason(SENIOR, "Software Engineer", "",
                               psp(levels=["middle", "senior"]))
    assert not bot.gate_reason(SENIOR, "Software Engineer", "", psp(levels=[]))


def test_passport_off_when_relocation_allowed():
    movable = dict(SENIOR, relocation="да, готов к переезду")
    assert not bot.gate_reason(movable, "Senior Backend Engineer", "", psp(hire=["US"]))


def test_passport_parse_drops_alien_urls_and_normalises():
    out = {"items": [{"url": "http://a", "hire": ["pt", " eu"], "work_mode": "Remote",
                      "levels": ["Senior"], "uncertain": ["hire"],
                      "role": "Backend", "must": ["Go", "PostgreSQL"],
                      "domains": ["Fintech"]},
                     {"url": "http://чужой", "hire": ["US"]}]}
    rows = bot.parse_passports(out, {"http://a"})
    assert rows == [("http://a", "PT,EU", "remote", "", "senior", "hire",
                     "backend", "go,postgresql", "", "fintech")]


def test_passport_prompt_keeps_url_and_cuts_text():
    rows = [("http://a", "Senior Go", "", "x" * 9000)]
    text = bot.passport_prompt(rows)
    assert "url=http://a" in text and len(text) < bot.PASSPORT_CHARS + 200


def test_gate_rejections_are_not_stored_and_do_not_block_the_slice(monkeypatch):
    """Отказ ворот строки не оставляет, но и очередь собой не затыкает."""
    import storage, llm
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO profiles(tg_id,data,updated_at) VALUES"
                 "(77,'{\"level\":\"senior\",\"location\":\"Порту\",\"relocation\":\"нет\"}','2026-10-12')")
    for url, title, loc, dd in (("https://g/us", "Senior Go Engineer", "Remote US", "g-us"),
                                ("https://g/pt", "Senior Go Engineer", "Porto", "g-pt")):
        conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,location,source,"
                     "first_seen,dedup,description) VALUES(?,'Acme',?,?,'lever',"
                     "'2026-10-12',?,'текст вакансии')", (url, title, loc, dd))
    # очередь соседних тестов закрываем, чтобы видеть ровно эти две вакансии
    conn.execute("INSERT OR REPLACE INTO matches(tg_id,job_url,pct,why,scored_at) "
                 "SELECT 77, url, 0, 'чужая', '2026-10-12' FROM jobs "
                 "WHERE url NOT IN ('https://g/us','https://g/pt')")
    conn.commit()

    def fake_ask(system, user, **kw):
        return {"items": [{"url": "https://g/pt", "work_mode": "remote"}],
                "scores": [{"url": "https://g/pt", "pct": 88, "why": "подходит"}]}

    monkeypatch.setattr(llm, "ask_json", fake_ask)
    bot.score_pending(conn, 77)
    got = dict(conn.execute("SELECT job_url, pct FROM matches WHERE tg_id=77 "
                            "AND job_url LIKE 'https://g/%'").fetchall())
    assert "https://g/us" not in got          # отказ ворот в базу не попал
    assert got["https://g/pt"] == 88          # а подходящая оценена, срез не забит
    # паспорт строится только на том, что прошло бесплатные ворота
    assert conn.execute("SELECT count(*) FROM passports WHERE url='https://g/us'"
                        ).fetchone()[0] == 0


def test_gate_audit_reproduces_rejections_live():
    """Живой разбор вместо хранимых нулей: причина есть, модель не нужна."""
    import storage
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO profiles(tg_id,data,updated_at) VALUES"
                 "(78,'{\"level\":\"senior\",\"location\":\"Порту\",\"relocation\":\"нет\"}','2026-10-12')")
    conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,location,source,"
                 "first_seen,dedup) VALUES('https://g/us2','Acme','Senior Go',"
                 "'Remote US','lever','2026-10-12','g-us2')")
    conn.commit()
    rejected = {u: why for u, _, _, why in bot.gate_audit(conn, 78)}
    assert "«US»" in rejected["https://g/us2"]


# --- теги вместо триажа ------------------------------------------------------

def test_job_tags_take_role_from_title_and_passport():
    """Из одного ярлыка модель выбирает привкус, а не основу: «Senior Software
    Engineer (Backend) — AI/ML» она помечает как ml-engineer. Название спасает."""
    t = bot.job_tags("Senior Software Engineer (Backend) - AI/ML",
                     {"role": "ml-engineer", "must": ["Python"], "domains": ["Cloud"]})
    assert t["role"] == {"backend", "ml-engineer"}
    assert t["stack"] == {"python"} and t["domain"] == {"cloud"}


def test_stack_tags_fold_synonyms_without_false_hits():
    assert bot.stack_tags(["PostgreSQL", "Postgres", "MySQL"]) == {"sql"}
    assert bot.stack_tags(["Kubernetes", "Docker", "Helm"]) == {"k8s"}
    assert bot.stack_tags(["Golang"]) == {"go"}
    assert bot.stack_tags(["MongoDB"]) == {"nosql"}      # «go» внутри слова не считается
    assert bot.stack_tags(["Python 3.12"]) == {"python"}


def test_profile_tags_and_subscription_round_trip():
    import storage
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO profiles(tg_id,data,updated_at) VALUES"
                 "(21,'{\"role\":\"Senior Backend\",\"stack\":[\"Go\",\"Kafka\"],"
                 "\"domains\":[\"fintech\"]}','2026-10-12')")
    conn.commit()
    tags = bot.get_tags(conn, 21)
    # соседние роли в подписке по умолчанию: без них теряется пятая часть
    assert tags["role"] == {"backend", "platform", "devops-sre", "fullstack"}
    assert tags["stack"] == {"go", "kafka"}
    # выведенная подписка сохраняется, чтобы человек мог её поправить
    assert conn.execute("SELECT count(*) FROM profile_tags WHERE tg_id=21"
                        ).fetchone()[0] == 7
    tags["role"].discard("devops-sre")
    bot.set_tags(conn, 21, tags)
    assert bot.get_tags(conn, 21)["role"] == {"backend", "platform", "fullstack"}


def test_tags_command_edits_roles(monkeypatch):
    import storage
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO profiles(tg_id,data,updated_at) VALUES"
                 "(22,'{\"role\":\"Senior Backend\",\"stack\":[\"Go\"]}','2026-10-12')")
    conn.commit()
    said = []
    monkeypatch.setattr(bot, "send", lambda chat, text, **kw: said.append(text))
    bot.cmd_tags(bot.Ctx(conn, 22, 22, "-devops-sre -backend +ерунда", False))
    assert bot.get_tags(conn, 22)["role"] == {"platform", "fullstack"}
    assert "Не знаю таких ролей: ерунда" in said[0]
    assert "platform" in said[-1]


def test_score_pending_keeps_untagged_and_drops_foreign_role(monkeypatch):
    """Чужая профессия отсеивается бесплатно, а вакансия без тегов идёт дальше:
    пустые теги — это «не знаю», а не «не подходит». Соседняя техническая роль
    тоже идёт к модели: product manager у бэкендера не повод для отказа."""
    import storage, llm
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO profiles(tg_id,data,updated_at) VALUES"
                 "(23,'{\"level\":\"senior\",\"relocation\":\"да\",\"role\":\"Senior Backend\","
                 "\"stack\":[\"Go\"]}','2026-10-12')")
    for url, title in (("https://t/own", "Senior Backend Engineer"),
                       ("https://t/alien", "Senior Account Executive"),
                       ("https://t/near", "Senior Product Manager"),
                       ("https://t/mute", "Specialist II")):
        conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,location,source,"
                     "first_seen,dedup,description) VALUES(?,'Acme',?,'Porto','lever',"
                     "'2026-10-12',?,'текст')", (url, title, url))
    conn.execute("INSERT OR REPLACE INTO matches(tg_id,job_url,pct,why,scored_at) "
                 "SELECT 23, url, 0, 'чужая', '2026-10-12' FROM jobs "
                 "WHERE url NOT IN ('https://t/own','https://t/alien','https://t/mute',"
                 "'https://t/near')")
    conn.commit()

    def fake_ask(system, user, **kw):
        if system is bot.PASSPORT_SYSTEM:
            return {"items": [{"url": u, "work_mode": "remote"}
                              for u in ("https://t/own", "https://t/alien", "https://t/mute",
                                        "https://t/near")]}
        return {"scores": [{"url": u, "pct": 80, "why": "ок"} for u in
                           ("https://t/own", "https://t/mute", "https://t/near")]}

    monkeypatch.setattr(llm, "ask_json", fake_ask)
    bot.score_pending(conn, 23)
    got = dict(conn.execute("SELECT job_url, pct FROM matches WHERE tg_id=23 "
                            "AND job_url LIKE 'https://t/%'"))
    assert got == {"https://t/own": 80, "https://t/mute": 80, "https://t/near": 80}
    assert "https://t/alien" not in got         # отсеяна тегами, строки не оставила


def test_passport_from_old_schema_is_rebuilt(monkeypatch):
    """Паспорт без роли — из прошлой версии схемы: без неё вакансия остаётся
    без тегов, поэтому строим заново."""
    import storage, llm
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO passports(url,hire,work_mode,made_at) "
                 "VALUES('https://p/old','PT','remote','2026-10-12')")
    conn.execute("INSERT OR REPLACE INTO passports(url,hire,work_mode,role,made_at) "
                 "VALUES('https://p/new','PT','remote','backend','2026-10-12')")
    conn.commit()
    asked = []
    monkeypatch.setattr(llm, "ask_json", lambda system, user, **kw: (
        asked.append(user), {"items": [{"url": "https://p/old", "role": "backend"}]})[1])
    rows = [("https://p/old", "Acme", "Senior Go", "Porto", "текст"),
            ("https://p/new", "Acme", "Senior Go", "Porto", "текст")]
    bot.ensure_passports(conn, rows)
    assert len(asked) == 1 and "https://p/old" in asked[0] and "https://p/new" not in asked[0]
    assert conn.execute("SELECT role FROM passports WHERE url='https://p/old'"
                        ).fetchone()[0] == "backend"


def test_score_pending_survives_empty_backlog(monkeypatch):
    """score_once зовёт score_pending с prefer=None, когда доборки не было."""
    import storage, llm
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO profiles(tg_id,data,updated_at) VALUES"
                 "(24,'{\"level\":\"senior\",\"relocation\":\"да\",\"role\":\"Senior Backend\"}',"
                 "'2026-10-12')")
    conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,location,source,"
                 "first_seen,dedup,description) VALUES('https://n/1','Acme',"
                 "'Senior Backend Engineer','Porto','lever','2026-10-12','n1','текст')")
    conn.execute("INSERT OR REPLACE INTO matches(tg_id,job_url,pct,why,scored_at) "
                 "SELECT 24, url, 0, 'чужая', '2026-10-12' FROM jobs WHERE url <> 'https://n/1'")
    conn.commit()
    monkeypatch.setattr(llm, "ask_json", lambda system, user, **kw: {
        "items": [{"url": "https://n/1", "role": "backend", "work_mode": "remote"}],
        "scores": [{"url": "https://n/1", "pct": 77, "why": "ок"}]})
    assert bot.score_pending(conn, 24, prefer=None) == 1


def test_stack_tags_new_terms_and_tricky_boundaries():
    """Словарь пополнен по отчёту о пропущенных терминах 2026-10-13."""
    assert bot.stack_tags(["React", "Next.js"]) == {"react"}
    assert bot.stack_tags(["NumPy", "pandas"]) == {"pydata"}
    assert bot.stack_tags(["PyTorch"]) == {"ml"}
    assert bot.stack_tags(["Django", "FastAPI"]) == {"python"}
    assert bot.stack_tags(["Jira", "Confluence"]) == {"tracker"}
    assert bot.stack_tags(["Salesforce", "HubSpot"]) == {"crm"}
    assert bot.stack_tags(["SAP", "Workday"]) == {"enterprise"}
    # три разных языка, которые отличаются одним знаком
    assert bot.stack_tags(["C++"]) == {"cpp"}
    assert bot.stack_tags(["C"]) == {"cpp"}
    assert bot.stack_tags(["C#"]) == {"dotnet"}
    assert bot.stack_tags([".NET"]) == {"dotnet"}
    # общие для всех профессий слова тегов не получают: они не различают вакансии
    for word in ("Excel", "PowerPoint", "MS Office", "Git", "Linux", "Slack", "English"):
        assert bot.stack_tags([word]) == set(), word
    # старое поведение не сломано
    assert bot.stack_tags(["PostgreSQL", "Postgres"]) == {"sql"}
    assert bot.stack_tags(["MongoDB"]) == {"nosql"}


def test_role_rx_has_no_control_chars_and_short_words_match():
    """В bbf2320 вместо \\b в файл записались символы backspace: правила hr, ux,
    seo, bdr и sdr не совпадали ни с чем, и такие вакансии уходили к дорогой
    модели как «роль неизвестна». Порча невидима глазом — ловим её тестом."""
    assert not any(ch < " " for rx in bot.ROLE_RX.values() for ch in rx)
    assert bot.role_tags("Senior HR Business Partner") == {"hr"}
    assert bot.role_tags("Senior UX Researcher") == {"designer"}
    assert bot.role_tags("Senior Manager, SEO & GEO") == {"marketing"}
    # Граница слова: внутри других слов эти буквы тег не дают
    assert bot.role_tags("Linux Kernel Engineer") == set() or "designer" not in bot.role_tags("Linux Kernel Engineer")
    assert "hr" not in bot.role_tags("Throughput Engineer")


def test_fullstack_with_space_keeps_engineer_from_false_reject():
    """«Full Stack» через пробел не распознавался. Вместе с вернувшимся \\bux\\b
    вакансия «Full Stack Engineer — Core UX» получила бы только designer и была
    бы отсечена у бэкендера до модели."""
    tags = bot.role_tags("Senior Full Stack Engineer - Core UX")
    assert "fullstack" in tags and "designer" in tags
    mine = bot.profile_tags({"role": "Senior Backend Engineer"})
    assert mine["role"] & tags                 # общий тег есть — не отсекаем


def test_says_yes_reads_onboarding_answers():
    """Согласие в свободной форме: раньше им считались только «да» и «готов»."""
    assert bot.says_yes("открыта к релокации по всей Европе")
    assert bot.says_yes("любой формат (офис/гибрид/удалёнка)")
    assert bot.says_yes("да, 2 дня в офисе")
    assert not bot.says_yes("нет, Португалию не покидает")
    assert not bot.says_yes("не рассматриваю")
    assert not bot.says_yes("")


def test_relocating_profile_is_not_blocked_by_office_city():
    """Готовой к переезду по Европе офис в Париже не блокер, страна найма —
    по-прежнему да: US-only вакансия отсекается."""
    valeria = {"level": "middle", "location": "Norway",
               "relocation": "открыта к релокации по всей Европе",
               "hybrid": "любой формат (офис/гибрид/удалёнка)"}
    assert bot.passport_block(valeria, {"work_mode": "hybrid", "office_city": "paris",
                                        "hire": ["FR"]}) is None
    assert bot.passport_block(valeria, {"work_mode": "onsite", "office_city": "london",
                                        "hire": ["GB"]}) is None
    assert bot.passport_block(valeria, {"work_mode": "remote", "hire": ["US"]})
    # Неподвижный профиль из Порту — как раньше
    porto = {"level": "senior", "location": "Porto", "relocation": "нет",
             "hybrid": "да, 2 дня в офисе"}
    assert bot.passport_block(porto, {"work_mode": "hybrid", "office_city": "paris",
                                      "hire": ["FR"]})
    assert bot.passport_block(porto, {"work_mode": "hybrid", "office_city": "porto",
                                      "hire": ["PT"]}) is None


def test_lisbon_hybrid_blocks_porto_profile():
    """Порту и Лиссабон — разные города: гибрид в Лиссабоне для того, кто ходит в
    офис только в Порту, блокер. «Porto or Lisbon» по-прежнему проходит."""
    porto = {"level": "senior", "location": "Porto", "relocation": "нет",
             "hybrid": "да, 2 дня в неделю в Порту"}
    assert bot.passport_block(porto, {"work_mode": "hybrid", "office_city": "lisbon",
                                      "hire": ["PT"]})
    assert bot.passport_block(porto, {"work_mode": "hybrid", "office_city": "porto or lisbon",
                                      "hire": ["PT"]}) is None


def test_score_prompt_treats_avoid_as_blocker():
    """Профиль хранит, чего человек не хочет, а промпт оценки поле avoid не
    упоминал: бизнес-аналитику приходил Senior Data Scientist на 62 балла."""
    assert "avoid" in bot.SCORE_SYSTEM


def test_money_line_warns_on_low_balance(monkeypatch):
    """Кончившиеся деньги выглядят как тишина — отчёт должен сказать прямо."""
    import llm
    monkeypatch.setattr(llm, "balance", lambda: 9.731)
    assert bot.money_line() == "OpenRouter: остаток $9.73"
    monkeypatch.setattr(llm, "balance", lambda: 1.2)
    assert "⚠️" in bot.money_line() and "$1.20" in bot.money_line()
    monkeypatch.setattr(llm, "balance", lambda: None)
    assert "не удалось" in bot.money_line()


def test_north_america_in_location_blocks_but_not_with_europe():
    """«Remote, North America» в поле локации ворота не узнавали: в справочнике
    мест было americas, а это другое слово. Вместе с EU — проходит."""
    porto = {"level": "senior", "location": "Porto", "relocation": "нет",
             "hybrid": "да, 2 дня в офисе"}
    assert bot.gate_reason(porto, "Senior Product Manager", "Remote, North America")
    assert bot.gate_reason(porto, "Senior Product Manager",
                           "Remote - North America - EU - UK") is None
    assert "North America" in bot.PASSPORT_SYSTEM


def test_pause_stops_scoring_and_resume_schedules_backlog(monkeypatch):
    """На паузе модель не зовём; снятие паузы назначает доборку, а не поштучную оценку."""
    import storage
    conn = storage.connect()
    for t in (31, 32):
        conn.execute("INSERT OR REPLACE INTO users(tg_id,paused) VALUES(?,0)", (t,))
        conn.execute("INSERT OR REPLACE INTO profiles(tg_id,data) VALUES(?,'{}')", (t,))
    conn.execute("DELETE FROM meta WHERE key LIKE 'backlog:3%'")
    conn.commit()
    bot.set_paused(conn, 31, True)
    scored = []
    monkeypatch.setattr(bot, "db", lambda: storage.connect())
    monkeypatch.setattr(bot, "score_pending", lambda c, t, **kw: scored.append(t))
    monkeypatch.setattr(bot, "backlog_pass", lambda c, t: scored.append(("bl", t)))
    bot.score_once()
    assert 31 not in scored and 32 in scored
    bot.set_paused(conn, 32, False)                      # не был на паузе — доборки нет
    assert not conn.execute("SELECT 1 FROM meta WHERE key=?", (bot.backlog_key(32),)).fetchone()
    bot.set_paused(conn, 31, False)
    assert conn.execute("SELECT 1 FROM meta WHERE key=?", (bot.backlog_key(31),)).fetchone()
    scored.clear()
    bot.score_once()
    assert ("bl", 31) in scored and 31 in scored
