#!/usr/bin/env python3
"""Хранилище: одно подключение, одна схема, один журнал.

Раньше bot.py и jobs.py заводили соединение каждый по-своему — с разными
таймаутами и своей половиной схемы. Теперь это одно место.
"""
import os, re, sqlite3
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
  name TEXT PRIMARY KEY, page_url TEXT, ats TEXT, slug TEXT, checked_at TEXT,
  last_ok TEXT, last_count INTEGER, last_error TEXT,
  added_by TEXT, added_at TEXT);
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
    # здоровье источника: молчит доска или отвечает пустым — это разные беды
    ("companies", "last_ok TEXT"), ("companies", "last_count INTEGER"),
    ("companies", "last_error TEXT"),
    # откуда компания взялась: tg_id человека, "telegram" из добора, "collector"
    ("companies", "added_by TEXT"), ("companies", "added_at TEXT"),
    # одна вакансия лежит на доске отдельной строкой под каждую страну: у Mozilla
    # «Senior Software Engineer, Add-Ons» — десять url. Ключ схлопывает их в одну
    ("jobs", "dedup TEXT"), ("sent", "dedup TEXT"),
]
INDEXES = [
    "CREATE INDEX IF NOT EXISTS jobs_dedup ON jobs(dedup)",
    "CREATE INDEX IF NOT EXISTS sent_dedup ON sent(tg_id, dedup)",
]


def dedup_key(company, title):
    """Компания плюс должность без знаков и регистра. Локация не входит намеренно:
    именно она и различает десять копий одной вакансии."""
    norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())
    return f"{norm(company)}|{norm(title)}"


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
    for sql in INDEXES:
        c.execute(sql)
    c.commit()
    return c


def log(*parts):
    """Append-only журнал: кто, когда, что. Один на бота и сборщик."""
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "audit.log", "a") as f:
        f.write(now() + "\t" + "\t".join(str(p) for p in parts) + "\n")


if __name__ == "__main__":
    print(DB)
