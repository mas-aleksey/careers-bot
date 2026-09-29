import sqlite3
import storage


def test_schema_has_every_table():
    c = storage.connect()
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "invites", "sent", "profiles", "answers", "profile_edits",
            "matches", "companies", "jobs", "pages"} <= tables


def test_connect_is_idempotent():
    """Схема создаётся при каждом старте бота и сборщика — падать нельзя."""
    storage.connect()
    c = storage.connect()
    assert c.execute("SELECT 1").fetchone() == (1,)


def test_late_columns_added_to_old_base():
    """База у владельца старше кода: колонки доезжают миграцией."""
    c = storage.connect()
    cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    assert {"posted", "salary", "contact", "closed_at"} <= cols
    cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
    assert {"step", "awaiting", "last_notified"} <= cols


def test_now_is_utc_iso():
    assert storage.now().endswith("+00:00")


def test_dedup_key_ignores_case_punctuation_and_location():
    """Одна вакансия под десятью url отличается только локацией — она в ключ
    не входит, иначе Mozilla Add-Ons уедет десятью карточками."""
    import storage
    k = storage.dedup_key("Mozilla", "Senior Software Engineer, Add-Ons")
    assert k == storage.dedup_key("mozilla ", "senior software engineer add-ons")
    assert k != storage.dedup_key("Mozilla", "Senior Software Engineer, Crypto")
    assert storage.dedup_key("", "") == "|"
