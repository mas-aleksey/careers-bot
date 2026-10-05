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
    assert "Remote US" in bot.gate_reason(SENIOR, "Senior Backend Engineer", "Remote US")
    assert "Junior" in bot.gate_reason(SENIOR, "Junior Backend Engineer", "Porto")


def test_score_prompt_allows_middle():
    """Senior-кандидату middle подойти может: блокер только junior и стажировки."""
    assert "junior-позиция, стажировка или trainee" in bot.SCORE_SYSTEM
    assert "уровень ниже, чем у кандидата" not in bot.SCORE_SYSTEM
    assert "Middle и middle+" in bot.SCORE_SYSTEM
