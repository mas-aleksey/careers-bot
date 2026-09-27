#!/usr/bin/env python3
"""Сборщик вакансий: реестр компаний -> публичные API девяти ATS -> SQLite.

Компании добавляются через бота и живут в таблице companies. У кого нет
читаемой доски — следим за изменением карьерной страницы по хешу.
"""
import hashlib, json, os, re, sqlite3, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


# BOT_DATA прокинут в контейнер бота как /data. Без него сборщик внутри
# контейнера создавал вторую базу по хостовому пути — она жила в слое
# контейнера и умирала с ним.
DB = Path(os.environ.get("BOT_DATA", "/projects/localhome/services/careers_bot/data")) / "bot.db"
UA = {"User-Agent": "Mozilla/5.0 (careers-bot; +local)"}
TIMEOUT = 15


def get(url, want_json=True):
    """404 — честный ответ «доски нет». 429 и 5xx — временные, один повтор:
    при восьми потоках ATS отдают 429, и компания молча теряется до следующего дня."""
    raw = None
    for attempt in (0, 1):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT).read()
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(3)
                continue
            return None
        except (urllib.error.URLError, OSError):
            if attempt == 0:
                time.sleep(2)
                continue
            return None
    if raw is None:
        return None
    if not want_json:
        return raw.decode("utf-8", "ignore")
    try:
        return json.loads(raw)
    except ValueError:
        return None


# --- реестр -----------------------------------------------------------------

def registry_db(conn):
    """Рабочий реестр. Компании добавляются через бота, а не правкой файла."""
    return [(n, u) for n, u in conn.execute(
        "SELECT name, COALESCE(page_url, '') FROM companies ORDER BY name")]


ATS_IN_URL = [
    ("ashby", r"jobs\.ashbyhq\.com/([^/?#]+)"),
    ("greenhouse", r"(?:job-)?boards\.greenhouse\.io/([^/?#]+)"),
    ("lever", r"jobs\.lever\.co/([^/?#]+)"),
    ("teamtailor", r"([a-z0-9-]+)\.teamtailor\.com"),
    ("recruitee", r"([a-z0-9-]+)\.recruitee\.com"),
    ("workable", r"apply\.workable\.com/([^/?#]+)"),
    ("pinpoint", r"([a-z0-9-]+)\.pinpointhq\.com"),
]


def from_url(url):
    for ats, pat in ATS_IN_URL:
        m = re.search(pat, url)
        if m:
            return ats, m.group(1)
    return None


def slug_variants(name):
    """Salmon Group сидит под 'salmon-group', Plata — под 'platacard': одной
    склейки мало, пробуем и дефис, и первое слово."""
    low = re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split()
    if not low:
        return []
    out = ["".join(low), "-".join(low)]
    if len(low) > 1:
        out.append(low[0])
    return list(dict.fromkeys(out))


def slugify(name):
    v = slug_variants(name)
    return v[0] if v else None


def posted(value):
    """Дата публикации к виду YYYY-MM-DD. Форматы разные: ISO, миллисекунды
    (Lever), «2026-09-15 16:47:19 UTC» (Recruitee)."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(value))
    return m.group(1) if m else None


# --- адаптеры ---------------------------------------------------------------
# Ashby, Workable и SmartRecruiters отдают 200 на любой slug — проверяем тело.

def ashby(s):
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{s}")
    if not d:
        return None
    return [(j["jobUrl"], j["title"], j.get("location", ""), posted(j.get("publishedAt")))
            for j in d.get("jobs", [])]


def greenhouse(s):
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs")
    if not isinstance(d, dict) or "jobs" not in d:
        return None
    return [(j["absolute_url"], j["title"], (j.get("location") or {}).get("name", ""),
             posted(j.get("first_published") or j.get("updated_at"))) for j in d["jobs"]]


def lever(s):
    """У европейских аккаунтов свой хост: CoinsPaid живёт на api.eu.lever.co,
    на api.lever.co его нет вовсе."""
    for host in ("api.lever.co", "api.eu.lever.co"):
        d = get(f"https://{host}/v0/postings/{s}?mode=json")
        if isinstance(d, list) and d:
            return [(j["hostedUrl"], j["text"], j.get("categories", {}).get("location", ""),
                     posted(j.get("createdAt"))) for j in d]
    return None


def smartrecruiters(s):
    d = get(f"https://api.smartrecruiters.com/v1/companies/{s}/postings")
    if not isinstance(d, dict) or not d.get("totalFound"):
        return None
    out = []
    for j in d.get("content", []):
        loc = j.get("location", {})
        out.append((f"https://jobs.smartrecruiters.com/{s}/{j['id']}", j["name"],
                    f"{loc.get('city','')} {loc.get('country','')}".strip(),
                    posted(j.get("releasedDate"))))
    return out


def workable(s):
    # аккаунты часто заведены с суффиксом: cloudlinux пустой, cloudlinux-1 — 84 вакансии
    for cand in (s, f"{s}-1", f"{s}-2"):
        d = get(f"https://apply.workable.com/api/v1/widget/accounts/{cand}")
        if isinstance(d, dict) and d.get("name") and d.get("jobs"):
            return [(j["url"], j["title"], j.get("location", ""),
                     posted(j.get("published_on") or j.get("created_at"))) for j in d["jobs"]]
    return None


def recruitee(s):
    d = get(f"https://{s}.recruitee.com/api/offers/")
    if not isinstance(d, dict) or "offers" not in d:
        return None
    return [(j["careers_url"], j["title"], f"{j.get('city','')} {j.get('country','')}".strip(),
             posted(j.get("published_at") or j.get("created_at"))) for j in d["offers"]]


def teamtailor(s):
    """Отдаёт JSON Feed: вакансии лежат в items, а не в jobs."""
    d = get(f"https://{s}.teamtailor.com/jobs.json")
    if not isinstance(d, dict):
        return None
    items = d.get("items") or d.get("jobs") or []
    return [(j.get("url", ""), j.get("title", ""), j.get("summary", "")[:60],
             posted(j.get("date_published"))) for j in items] or None


def pinpoint(s):
    """Даты публикации не отдаёт ни один эндпоинт Pinpoint, зато отдаёт вилку —
    у Tabby она видима в 28 вакансиях из 61, втрое чаще среднего по рынку."""
    d = get(f"https://{s}.pinpointhq.com/postings.json")
    if not isinstance(d, dict) or not d.get("data"):
        return None
    out = []
    for j in d["data"]:
        loc = j.get("location") or {}
        place = loc.get("name", "") if isinstance(loc, dict) else str(loc)
        if j.get("workplace_type_text"):
            place = f"{place}, {j['workplace_type_text']}".strip(", ")
        pay = None
        if j.get("compensation_visible") and j.get("compensation_minimum"):
            lo, hi = j.get("compensation_minimum"), j.get("compensation_maximum")
            cur = j.get("compensation_currency") or ""
            per = j.get("compensation_frequency") or ""
            pay = f"{lo:.0f}–{hi:.0f} {cur} {per}".strip() if hi else f"от {lo:.0f} {cur} {per}".strip()
        # reporting_to у Tabby заполнен в 52 вакансиях из 61 и часто содержит
        # почту нанимающего менеджера — отклик мимо общей формы.
        out.append((j.get("url") or f"https://{s}.pinpointhq.com/postings/{j['id']}",
                    j.get("title", ""), place, None, pay, str(j.get("reporting_to") or "")[:80]))
    return out


ADAPTERS = {"ashby": ashby, "greenhouse": greenhouse, "lever": lever,
            "smartrecruiters": smartrecruiters, "workable": workable,
            "recruitee": recruitee, "teamtailor": teamtailor, "pinpoint": pinpoint}


def discover(name, url):
    """(ats, slug) по ссылке, иначе перебором по имени. None — своя страница."""
    hit = from_url(url)
    if hit:
        return hit
    for s in slug_variants(name):
        for ats in ("ashby", "greenhouse", "lever", "smartrecruiters", "workable",
                    "recruitee", "teamtailor", "pinpoint"):
            if ADAPTERS[ats](s) is not None:
                return ats, s
    return None


JOB_IN_PAYLOAD = re.compile(
    r'\\"id\\":\\"(\d+)\\",\\"position\\":\\"([^"\\]{3,80})\\".{0,400}?\\"company\\":\{\\"name\\":\\"([^"\\]{1,40})\\"')


def next_data(html):
    """__NEXT_DATA__ целиком: у BETBY вакансии лежат там списком с готовыми
    ссылками на внешний ATS."""
    m = re.search(r'id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', html, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    found = []

    def walk(node):
        if isinstance(node, dict):
            title = node.get("title") or node.get("position") or node.get("name")
            link = node.get("link") or node.get("url") or node.get("applyUrl")
            if isinstance(title, str) and isinstance(link, str) and link.startswith("http"):
                found.append((link, title.strip(), str(node.get("location") or
                                                       node.get("city") or "")[:60], None))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return found or None


def embedded(url):
    """Ступень 3: вакансии лежат в HTML, а не в API. Так устроены top.co
    (свой формат) и betby.com (__NEXT_DATA__)."""
    html = get(url, want_json=False)
    if not html:
        return None
    base = url.rstrip("/")
    out = [(f"{base}/{jid}", title.strip(), company.strip(), None)
           for jid, title, company in JOB_IN_PAYLOAD.findall(html)]
    return out or next_data(html)


def page_hash(url):
    """Ступень 4: хеш текста без тегов. Не 'новая вакансия', а 'страница изменилась'."""
    html = get(url, want_json=False)
    if not html:
        return None
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", txt))
    return hashlib.md5(txt.encode()).hexdigest()


# --- база -------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies(
  name TEXT PRIMARY KEY, page_url TEXT, ats TEXT, slug TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  url TEXT PRIMARY KEY, company TEXT, title TEXT, location TEXT,
  source TEXT, first_seen TEXT, posted TEXT, salary TEXT, contact TEXT, closed_at TEXT);
CREATE TABLE IF NOT EXISTS pages(
  url TEXT PRIMARY KEY, hash TEXT, checked_at TEXT);
"""


def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.executescript(SCHEMA)
    for col in ("posted TEXT", "salary TEXT", "contact TEXT", "closed_at TEXT"):   # база могла быть создана раньше
        try:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
            c.commit()
        except sqlite3.OperationalError:
            pass
    return c


def collect(name, url, cache):
    """Один поток на компанию. Соединение SQLite между потоками не живёт, поэтому
    известные ats/slug читаются заранее в словарь. Discovery сюда не входит:
    Workable режет параллельные запросы, и компания молча теряется."""
    ats, slug = cache.get(name, (None, None))
    if ats == "embedded":
        return name, ats, slug, embedded(url) or [], None
    if ats:
        jobs = ADAPTERS[ats](slug)
        return name, ats, slug, jobs or [], None
    jobs = embedded(url)          # вакансии внутри страницы — ступень 3
    if jobs:
        return name, "embedded", url, jobs, None
    return name, None, None, None, page_hash(url)


def run(only=None):
    """only — подстрока имени: гонять весь реестр ради одной компании незачем."""
    conn = db()
    companies = registry_db(conn)
    if only:
        companies = [c for c in companies if only.lower() in c[0].lower()]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cache = {n: (a, s) for n, a, s in conn.execute(
        "SELECT name, ats, slug FROM companies WHERE ats IS NOT NULL")}
    # discovery последовательно и один раз на компанию: параллельно ATS отдают 429
    for name, url in companies:
        if name not in cache:
            found = discover(name, url)
            if found:
                cache[name] = found
            time.sleep(0.3)
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(lambda c: collect(c[0], c[1], cache), companies))

    new, changed, stats, closed = [], [], [], []
    for (name, url), (_, ats, slug, jobs, phash) in zip(companies, results):
        conn.execute("INSERT INTO companies(name,page_url,ats,slug,checked_at) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(name) DO UPDATE SET ats=excluded.ats, slug=excluded.slug, "
                     "checked_at=excluded.checked_at", (name, url, ats, slug, now))
        if jobs is None:
            old = conn.execute("SELECT hash FROM pages WHERE url=?", (url,)).fetchone()
            if phash and (not old or old[0] != phash):
                if old:
                    changed.append((name, url))
                conn.execute("INSERT INTO pages(url,hash,checked_at) VALUES(?,?,?) "
                             "ON CONFLICT(url) DO UPDATE SET hash=excluded.hash, checked_at=excluded.checked_at",
                             (url, phash, now))
            stats.append((name, "страница", 0))
            continue
        fresh = 0
        for row in jobs:
            j_url, title, loc = row[0], row[1], row[2]
            pub = row[3] if len(row) > 3 else None
            pay = row[4] if len(row) > 4 else None
            who = row[5] if len(row) > 5 else None
            if not j_url:
                continue
            cur = conn.execute("INSERT OR IGNORE INTO jobs(url,company,title,location,source,first_seen,posted,salary,contact) "
                               "VALUES(?,?,?,?,?,?,?,?,?)", (j_url, name, title, loc, ats, now, pub, pay, who))
            if not cur.rowcount and who:
                conn.execute("UPDATE jobs SET contact=? WHERE url=? AND contact IS NULL", (who, j_url))
            if not cur.rowcount and pay:
                conn.execute("UPDATE jobs SET salary=? WHERE url=? AND salary IS NULL", (pay, j_url))
            if not cur.rowcount and pub:      # вакансия известна, дату узнали позже
                conn.execute("UPDATE jobs SET posted=? WHERE url=? AND posted IS NULL", (pub, j_url))
            if cur.rowcount:
                fresh += 1
                new.append((name, title, f"{loc} · опубликована {pub}" if pub else loc, j_url))
        # Вакансия пропала с доски — помечаем закрытой. Только при непустом
        # ответе: пустой список чаще означает сбой API, а не «всех наняли».
        if jobs:
            live = {j[0] for j in jobs if j and j[0]}
            marks = conn.execute(
                "UPDATE jobs SET closed_at=? WHERE company=? AND closed_at IS NULL "
                f"AND url NOT IN ({','.join('?' * len(live))})",
                [now, name, *live]).rowcount
            if marks:
                closed.append((name, marks))
        stats.append((name, ats, fresh))

    conn.commit()
    return new, changed, stats


def selftest():
    assert from_url("https://jobs.ashbyhq.com/constructor") == ("ashby", "constructor")
    assert from_url("https://job-boards.greenhouse.io/nebius") == ("greenhouse", "nebius")
    assert from_url("https://jobs.lever.co/appfollow") == ("lever", "appfollow")
    assert from_url("https://praktika.teamtailor.com/jobs") == ("teamtailor", "praktika")
    assert from_url("https://elixi.com/careers") is None
    assert slugify("Grid Dynamics") == "griddynamics"
    assert slug_variants("Salmon Group") == ["salmongroup", "salmon-group", "salmon"]
    assert slug_variants("Plata") == ["plata"]
    sample = (r'\"id\":\"9699\",\"position\":\"Director of Risk\",\"location\":\"x\",'
              r'\"company\":{\"name\":\"Wallet\"}')
    assert JOB_IN_PAYLOAD.findall(sample) == [("9699", "Director of Risk", "Wallet")]
    assert slugify("Xata.io") == "xataio"
    assert posted("2026-09-21T08:00:59.084+00:00") == "2026-09-21"
    assert posted(1788965250140) == "2026-09-09"
    assert posted("2026-09-15 16:47:19 UTC") == "2026-09-15"
    assert posted(None) is None and posted("") is None
    assert slugify("---") is None
    print("selftest ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        new, changed, stats = run(sys.argv[1] if len(sys.argv) > 1 else None)
        for name, ats, fresh in sorted(stats, key=lambda x: -x[2])[:15]:
            print(f"{fresh:4}  {name:24} {ats}")
        import sqlite3 as _s
        shut = _s.connect(DB).execute("select count(*) from jobs where closed_at is not null").fetchone()[0]
        print(f"\nновых вакансий {len(new)}, страниц изменилось {len(changed)}, "
              f"компаний {len(stats)}, закрытых всего {shut}")
