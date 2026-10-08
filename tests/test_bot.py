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


TRIAGE_ROWS = [("u1", "Co", "Senior Backend Engineer", "Porto", "Python, Kafka"),
               ("u2", "Co", "Retail Account Manager", "Porto", "продажи"),
               ("u3", "Co", "Staff Platform Engineer", "Porto", "Go, k8s")]


def test_triage_keeps_by_score_and_passes_silence(monkeypatch):
    """Невернувшаяся вакансия идёт дальше: молчание модели — «не знаю», не ноль."""
    monkeypatch.setattr(bot, "TRIAGE_FLOOR", 15)
    import llm
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: {"scores": [
        {"url": "u1", "pct": 80}, {"url": "u2", "pct": 0}]})
    keep, scores = bot.triage({"role": "backend"}, TRIAGE_ROWS)
    assert [r[0] for r in keep] == ["u1", "u3"]   # u3 модель не вернула — пропущен
    assert scores == {"u1": 80, "u2": 0}


def test_triage_sends_description_slice(monkeypatch):
    """Стек в тексте впервые встречается около 1286-го символа — заголовка мало."""
    seen = {}
    import llm

    def fake(system, user, **kw):
        seen["user"] = user
        return {"scores": []}

    monkeypatch.setattr(llm, "ask_json", fake)
    bot.triage({"role": "backend"}, TRIAGE_ROWS)
    assert "Python, Kafka" in seen["user"]
    assert "Porto" not in seen["user"]            # локация триажу не нужна


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
    """Верхушка идёт к дорогой модели, остальным сразу ноль с баллом триажа —
    иначе обычный цикл подберёт их как неоценённые и заплатит за каждую."""
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
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: {"scores": [
        {"url": "https://j/0", "pct": 90}, {"url": "https://j/1", "pct": 70},
        {"url": "https://j/2", "pct": 40}, {"url": "https://j/3", "pct": 10}]})
    known = bot.backlog_pass(conn, 8)
    assert known == {"https://j/0": 90, "https://j/1": 70}
    # в базе лежат и чужие вакансии от соседних тестов — смотрим только свои
    got = dict(conn.execute("SELECT job_url, pct FROM matches WHERE tg_id=8 "
                            "AND job_url IN ('https://j/0','https://j/1',"
                            "'https://j/2','https://j/3')"))
    assert got == {"https://j/2": 0, "https://j/3": 0}    # верхушка пока без оценки
    assert conn.execute("SELECT triage_pct FROM matches WHERE job_url='https://j/2'"
                        ).fetchone()[0] == 40


def test_score_pending_trusts_backlog_scores(monkeypatch):
    """Верхушка доборки не проходит триаж второй раз: лишняя пачка плюс риск,
    что модель отсеет то, что сама же отобрала."""
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
        seen.append("триаж" if system is bot.TRIAGE_SYSTEM else "sonnet")
        return {"scores": [{"url": "https://k/1", "pct": 91, "why": "подходит"}]}

    monkeypatch.setattr(llm, "ask_json", fake_ask)
    monkeypatch.setattr(bot, "SCORE_SLICE", 50)
    bot.score_pending(conn, 11, known_triage={"https://k/1": 85})
    assert seen == ["sonnet"]                  # дешёвую модель не звали вовсе
    row = conn.execute("SELECT pct, triage_pct FROM matches "
                       "WHERE job_url='https://k/1'").fetchone()
    assert row == (91, 85)


def test_score_pending_stores_triage_score_for_both_sides(monkeypatch):
    """Балл дешёвой модели нужен и у прошедших: без пары «триаж, Sonnet»
    порог для переоценки истории не на чем калибровать."""
    import storage, llm
    conn = storage.connect()
    conn.execute("INSERT OR REPLACE INTO profiles(tg_id,data,updated_at) "
                 "VALUES(9,'{\"level\":\"senior\",\"relocation\":\"да\"}','2026-10-06')")
    for url, title in (("https://j/keep", "Senior Backend Engineer"),
                       ("https://j/drop", "Retail Account Manager")):
        conn.execute("INSERT OR REPLACE INTO jobs(url,company,title,location,source,"
                     "first_seen,dedup,description) VALUES(?,'Acme',?,'Porto','lever',"
                     "'2026-10-01',?,'Python, Kafka')", (url, title, url))
    conn.commit()

    def fake_ask(system, user, **kw):
        if system is bot.TRIAGE_SYSTEM:
            return {"scores": [{"url": "https://j/keep", "pct": 62},
                               {"url": "https://j/drop", "pct": 5}]}
        return {"scores": [{"url": "https://j/keep", "pct": 88, "why": "подходит"}]}

    monkeypatch.setattr(llm, "ask_json", fake_ask)
    bot.score_pending(conn, 9)
    got = dict(conn.execute("SELECT job_url, triage_pct FROM matches WHERE tg_id=9"))
    assert got == {"https://j/keep": 62, "https://j/drop": 5}
    assert conn.execute("SELECT pct FROM matches WHERE job_url='https://j/keep'"
                        ).fetchone()[0] == 88


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
