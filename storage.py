#!/usr/bin/env python3
"""Хранилище: одно подключение, одна схема, один журнал.

Раньше bot.py и jobs.py заводили соединение каждый по-своему — с разными
таймаутами и своей половиной схемы. Теперь это одно место.
"""
import os, sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(os.environ.get("BOT_DATA", "/data"))
DB = DATA / "bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  tg_id INTEGER PRIMARY KEY, username TEXT, profile TEXT, is_admin INTEGER DEFAULT 0,
  invited_by INTEGER, joined_at TEXT, paused INTEGER DEFAULT 0,
  step INTEGER DEFAULT 0, awaiting TEXT, last_notified TEXT);
CREATE TABLE IF NOT EXISTS invites(
  code TEXT PRIMARY KEY, created_by INTEGER, created_at TEXT, expires_at TEXT, used_by INTEGER);
CREATE TABLE IF NOT EXISTS sent(
  tg_id INTEGER, job_url TEXT, sent_at TEXT, PRIMARY KEY(tg_id, job_url));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS profiles(
  tg_id INTEGER PRIMARY KEY, data TEXT, min_match INTEGER DEFAULT 70,
  notify TEXT DEFAULT 'daily', updated_at TEXT);
CREATE TABLE IF NOT EXISTS answers(
  tg_id INTEGER, question TEXT, answer TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS profile_edits(
  tg_id INTEGER, text TEXT, changed TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS matches(
  tg_id INTEGER, job_url TEXT, pct INTEGER, why TEXT, scored_at TEXT,
  PRIMARY KEY(tg_id, job_url));
CREATE TABLE IF NOT EXISTS companies(
  name TEXT PRIMARY KEY, page_url TEXT, ats TEXT, slug TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  url TEXT PRIMARY KEY, company TEXT, title TEXT, location TEXT,
  source TEXT, first_seen TEXT, posted TEXT, salary TEXT, contact TEXT, closed_at TEXT);
CREATE TABLE IF NOT EXISTS pages(
  url TEXT PRIMARY KEY, hash TEXT, checked_at TEXT);
"""

# Колонки, добавленные после первого релиза: база у владельца старше кода.
LATE_COLUMNS = [
    ("users", "step INTEGER DEFAULT 0"), ("users", "awaiting TEXT"),
    ("users", "last_notified TEXT"),
    ("jobs", "posted TEXT"), ("jobs", "salary TEXT"),
    ("jobs", "contact TEXT"), ("jobs", "closed_at TEXT"),
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")   # бот и сборщик пишут одновременно
    c.executescript(SCHEMA)
    for table, col in LATE_COLUMNS:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    return c


def log(*parts):
    """Append-only журнал: кто, когда, что. Один на бота и сборщик."""
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "audit.log", "a") as f:
        f.write(now() + "\t" + "\t".join(str(p) for p in parts) + "\n")


def selftest():
    import tempfile
    os.environ["BOT_DATA"] = tempfile.mkdtemp()
    global DATA, DB
    DATA = Path(os.environ["BOT_DATA"]); DB = DATA / "bot.db"
    c = connect()
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "jobs", "profiles", "matches", "companies"} <= tables, tables
    assert connect() is not None, "повторное подключение не должно падать"
    cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    assert {"posted", "salary", "contact", "closed_at"} <= cols, cols
    assert now().endswith("+00:00")
    print("selftest ok")


if __name__ == "__main__":
    import sys
    selftest() if len(sys.argv) > 1 and sys.argv[1] == "selftest" else print(DB)
