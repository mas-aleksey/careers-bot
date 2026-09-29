#!/usr/bin/env python3
"""Сборщик вакансий: реестр компаний -> публичные API девяти ATS -> SQLite.

Компании добавляются через бота и живут в таблице companies. У кого нет
читаемой доски — следим за изменением карьерной страницы по хешу.
"""
import ast, hashlib, json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import storage


UA = {"User-Agent": "Mozilla/5.0 (careers-bot; +local)"}
TIMEOUT = 15
# Минимальный зазор между запросами к одному хосту. Пять потоков попадают на
# один ATS одновременно, и он отдаёт 429: Workable ловил его регулярно, потому
# что пробует ещё и три суффикса подряд. Подкручивать здесь, если снова полезет.
GAP = 0.6
# Cloudflare у Workable даёт 1015 на всплеск, а не на средний темп: три пробы
# подряд его уже злят. Таблица для тех, кому общего зазора мало.
GAPS = {"apply.workable.com": 3.0}
_last, _gate = {}, threading.Lock()


def space_out(url):
    """Держит зазор между запросами к одному хосту. Спим вне замка, иначе
    потоки к разным ATS ждут друг друга без причины."""
    host = urllib.parse.urlsplit(url).netloc
    with _gate:
        start = max(_last.get(host, 0.0), time.monotonic())
        _last[host] = start + GAPS.get(host, GAP)
    delay = start - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def get(url, want_json=True, with_code=False):
    """404 — честный ответ «доски нет». 429 и 5xx — временные, один повтор:
    при восьми потоках ATS отдают 429, и компания молча теряется до следующего дня.

    with_code — вернуть (тело, код). Без кода 404 «такого слага нет» и 429 «нас
    притормозили» неразличимы, и бот после бана дозванивается дальше, углубляя его."""
    raw, code = None, 0
    for attempt in (0, 1):
        try:
            space_out(url)
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT)
            raw, code = r.read(), r.status
            break
        except urllib.error.HTTPError as e:
            code = e.code
            if e.code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(3)
                continue
            break
        except (urllib.error.URLError, OSError):
            if attempt == 0:
                time.sleep(2)
                continue
            break
    body = None
    if raw is not None:
        if want_json:
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
        else:
            body = raw.decode("utf-8", "ignore")
    return (body, code) if with_code else body


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


def same_company(a, b):
    """Сравнение названий без знаков и регистра. «ABC Fitness» против
    «ThoughtWorks_new» — разные, и это ловится до того, как чужие вакансии
    попадут в базу."""
    norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())
    x, y = norm(a), norm(b)
    return bool(x) and bool(y) and (x == y or x.startswith(y) or y.startswith(x))


def board_owner(ats, slug):
    """Чьё это на самом деле. None — провайдер имени не отдаёт, проверить нечем."""
    if ats == "greenhouse":
        d = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        return (d or {}).get("name")
    if ats == "smartrecruiters":
        d = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings")
        c = ((d or {}).get("content") or [{}])[0].get("company") or {}
        return c.get("name")
    if ats == "teamtailor":
        d = get(f"https://{slug}.teamtailor.com/jobs.json")
        return (d or {}).get("title")
    if ats == "recruitee":
        d = get(f"https://{slug}.recruitee.com/api/offers/")
        return ((d or {}).get("offers") or [{}])[0].get("company_name")
    return None


def slug_variants(name):
    """Salmon Group сидит под 'salmon-group', Plata — под 'platacard': одной
    склейки мало, пробуем и дефис, и первое слово."""
    low = re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split()
    if not low:
        return []
    out = ["".join(low), "-".join(low)]
    # первое слово — только если оно само по себе похоже на имя: «abc» от
    # «ABC Fitness» поймал чужую доску с вакансиями в Пекине
    if len(low) > 1 and len(low[0]) >= 6:
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


def wk_rows(js):
    """Workable отдаёт строку на каждую пару «вакансия × город»: у CloudLinux
    14 вакансий разложены в 77 записей с одним url. Схлопываем по shortcode.

    Страны собираем в локацию: ключа `location` в ответе нет, есть `country` и
    `city`, и без них вакансия выглядит безадресной — гео-отсев такую пропускает
    и зря платит дорогой моделью."""
    out = {}
    for j in js:
        row = out.setdefault(j.get("shortcode") or j.get("url"), {"job": j, "where": []})
        place = j.get("country") or j.get("city") or ""
        if place and place not in row["where"]:
            row["where"].append(place)
    res = []
    for r in out.values():
        j = r["job"]
        where = ", ".join(r["where"][:6])
        remote = str(j.get("telecommuting", "")).lower() == "true"
        loc = f"Remote: {where}" if remote and where else where or ("Remote" if remote else "")
        res.append((j["url"], j["title"], loc,
                    posted(j.get("published_on") or j.get("created_at"))))
    return res


_wk_suffix = {}


def workable(s):
    # аккаунты часто заведены с суффиксом: cloudlinux пустой, cloudlinux-1 — 84
    # вакансии. Найденный запоминаем: три пробы каждый цикл — это и есть тот
    # всплеск, на котором Cloudflare отвечает 1015 и компания пропадает.
    empty, throttled = False, False
    for cand in ([_wk_suffix[s]] if s in _wk_suffix else (s, f"{s}-1", f"{s}-2")):
        d, code = get(f"https://apply.workable.com/api/v1/widget/accounts/{cand}", with_code=True)
        if isinstance(d, dict) and d.get("name"):
            if d.get("jobs"):
                _wk_suffix[s] = cand
                return wk_rows(d["jobs"])
            empty = True   # аккаунт есть, вакансий нет — это не сбой, а пустая доска
        if code == 429:
            throttled = True
            break          # уже притормозили: остальные суффиксы только углубят бан
    _wk_suffix.pop(s, None)       # запомненный перестал отвечать — пробуем все заново
    # [] и None читаются по-разному: пустая доска против «не дозвонились»
    return None if throttled or not empty else []


def recruitee(s):
    d = get(f"https://{s}.recruitee.com/api/offers/")
    if not isinstance(d, dict) or "offers" not in d:
        return None
    return [(j["careers_url"], j["title"], f"{j.get('city','')} {j.get('country','')}".strip(),
             posted(j.get("published_at") or j.get("created_at"))) for j in d["offers"]]


def tt_place(item):
    """Локация Teamtailor лежит не в самом фиде, а в приложенном к нему
    JobPosting по schema.org. Ключа `summary`, который читался раньше, в фиде
    нет вовсе — 126 вакансий числились безадресными и шли в дорогую оценку."""
    jp = item.get("_jobposting")
    if isinstance(jp, str):
        try:
            jp = ast.literal_eval(jp)
        except (ValueError, SyntaxError):
            return ""
    if not isinstance(jp, dict):
        return ""
    places = jp.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    out = []
    for pl in places:
        a = (pl or {}).get("address") or {}
        where = ", ".join(x for x in (a.get("addressLocality"), a.get("addressCountry")) if x)
        if where and where not in out:
            out.append(where)
    return " · ".join(out[:4])


def teamtailor(s):
    """Отдаёт JSON Feed: вакансии лежат в items, а не в jobs."""
    d = get(f"https://{s}.teamtailor.com/jobs.json")
    if not isinstance(d, dict):
        return None
    items = d.get("items") or d.get("jobs") or []
    return [(j.get("url", ""), j.get("title", ""), tt_place(j),
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
            # Пустая доска — не доказательство, что это та самая компания:
            # Workable заводит аккаунты под кучу имён, и Atlassian с Revolut
            # получили чужие. Для discovery годится только доска с вакансиями.
            if not ADAPTERS[ats](s):
                continue
            owner = board_owner(ats, s)
            if owner and not same_company(name, owner):
                log_skip(name, ats, s, owner)
                continue
            return ats, s
    return None


def log_skip(name, ats, slug, owner):
    print(f"  пропуск: {name} -> {ats}/{slug} принадлежит «{owner}»", file=sys.stderr)


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


# Вакансия как обычная ссылка на свой же сайт: /careers/7928089, /jobs/1234.
# Внутри ссылки название и локация отдельными кусками текста.
LINKED_JOB = re.compile(
    r'<a[^>]+href="(?P<href>[^"]*/(?:careers|jobs|vacancies|positions)/\d{4,}[^"]*)"[^>]*>'
    r'(?P<body>.{0,600}?)</a>', re.S)
SEPARATORS = {"—", "-", "–", "·", "|", ",", ""}


def linked_jobs(html, url):
    """Ступень 3, общий случай: сайт сам перечисляет вакансии ссылками.
    Так устроена Xata — доска у неё на Teamtailor, но публичного фида нет,
    зато собственная страница читается без всякого API."""
    root = re.match(r"https?://[^/]+", url)
    if not root:
        return []
    out, seen = [], set()
    for m in LINKED_JOB.finditer(html):
        parts = [t.strip() for t in re.split(r"<[^>]+>", m.group("body"))]
        parts = [t for t in parts if t not in SEPARATORS]
        if not parts:
            continue
        href = m.group("href")
        full = href if href.startswith("http") else root.group(0) + href
        if full in seen:                 # одна вакансия часто и в списке, и в JSON страницы
            continue
        seen.add(full)
        out.append((full, parts[0], parts[-1] if len(parts) > 1 else "", None))
    return out


def embedded(url):
    """Ступень 3: вакансии лежат в HTML, а не в API. Так устроены top.co
    (свой формат), betby.com (__NEXT_DATA__) и xata.io (ссылки на свой сайт)."""
    html = get(url, want_json=False)
    if not html:
        return None
    base = url.rstrip("/")
    out = [(f"{base}/{jid}", title.strip(), company.strip(), None)
           for jid, title, company in JOB_IN_PAYLOAD.findall(html)]
    return out or next_data(html) or linked_jobs(html, url)


def diagnose(ats, slug, url):
    """Почему не ответило. Один запрос, только при неудаче: код говорит
    больше, чем факт молчания — 403 это Cloudflare, 404 сменившийся slug."""
    probe = {"ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
             "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
             "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json",
             "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
             "workable": f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
             "recruitee": f"https://{slug}.recruitee.com/api/offers/",
             "teamtailor": f"https://{slug}.teamtailor.com/jobs.json",
             "pinpoint": f"https://{slug}.pinpointhq.com/postings.json"}.get(ats, url)
    if not probe:
        return "нет адреса для проверки"
    try:
        urllib.request.urlopen(urllib.request.Request(probe, headers=UA), timeout=TIMEOUT).read()
        return "ответ есть, но разобрать не удалось — возможно, сменился формат"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:
        return type(e).__name__


def page_hash(url):
    """Ступень 4: хеш текста без тегов. Не 'новая вакансия', а 'страница изменилась'."""
    html = get(url, want_json=False)
    if not html:
        return None
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", txt))
    return hashlib.md5(txt.encode()).hexdigest()


# --- база -------------------------------------------------------------------

def db():
    return storage.connect()


def collect(name, url, cache):
    """Один поток на компанию. Соединение SQLite между потоками не живёт, поэтому
    известные ats/slug читаются заранее в словарь. Discovery сюда не входит:
    Workable режет параллельные запросы, и компания молча теряется."""
    ats, slug = cache.get(name, (None, None))
    if ats == "embedded":
        return name, ats, slug, embedded(url) or [], None
    if ats:
        # None (доска не ответила) не схлопывать в []: иначе 429 от Workable
        # попадает в отчёт как «доска вернула пустой список» и diagnose молчит
        return name, ats, slug, ADAPTERS[ats](slug), None
    jobs = embedded(url)          # вакансии внутри страницы — ступень 3
    if jobs:
        return name, "embedded", url, jobs, None
    return name, None, None, None, page_hash(url)


# Бот перезапускается чаще, чем доски обновляются: сборщик стартует вместе с ним
# и гонит весь реестр заново. Компанию, проверенную недавно, пропускаем.
FRESH_HOURS = 4


def run(only=None):
    """only — подстрока имени: гонять весь реестр ради одной компании незачем.
    По имени окно свежести не действует — явная просьба важнее экономии."""
    conn = db()
    companies = registry_db(conn)
    now = storage.now()
    skipped = 0
    if only:
        companies = [c for c in companies if only.lower() in c[0].lower()]
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=FRESH_HOURS)).isoformat(timespec="seconds")
        fresh = {n for (n,) in conn.execute(
            "SELECT name FROM companies WHERE checked_at > ?", (cutoff,))}
        skipped = sum(1 for c in companies if c[0] in fresh)
        companies = [c for c in companies if c[0] not in fresh]
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
        # added_by/added_at только при вставке: обход не должен переписывать,
        # кто завёл компанию. DO UPDATE их намеренно не трогает.
        conn.execute("INSERT INTO companies(name,page_url,ats,slug,checked_at,added_by,added_at) "
                     "VALUES(?,?,?,?,?,'collector',?) "
                     "ON CONFLICT(name) DO UPDATE SET ats=excluded.ats, slug=excluded.slug, "
                     "checked_at=excluded.checked_at", (name, url, ats, slug, now, now))
        if ats:
            if jobs:
                conn.execute("UPDATE companies SET last_ok=?, last_count=?, last_error=NULL "
                             "WHERE name=?", (now, len(jobs), name))
            else:
                why = ("доска вернула пустой список" if jobs == []
                       else diagnose(ats, slug, url))
                conn.execute("UPDATE companies SET last_count=?, last_error=? WHERE name=?",
                             (0, why, name))
        if ats is None:
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
        for row in jobs or []:
            j_url, title, loc = row[0], row[1], row[2]
            pub = row[3] if len(row) > 3 else None
            pay = row[4] if len(row) > 4 else None
            who = row[5] if len(row) > 5 else None
            if not j_url:
                continue
            cur = conn.execute("INSERT OR IGNORE INTO jobs(url,company,title,location,source,first_seen,posted,salary,contact,dedup) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (j_url, name, title, loc, ats, now, pub, pay, who,
                                storage.dedup_key(name, title)))
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
    return new, changed, stats, skipped


if __name__ == "__main__":
    new, changed, stats, skipped = run(sys.argv[1] if len(sys.argv) > 1 else None)
    for name, ats, fresh in sorted(stats, key=lambda x: -x[2])[:15]:
        print(f"{fresh:4}  {name:24} {ats}")
    shut = db().execute("select count(*) from jobs where closed_at is not null").fetchone()[0]
    print(f"\nновых вакансий {len(new)}, страниц изменилось {len(changed)}, "
          f"компаний {len(stats)}, пропущено свежих {skipped}, закрытых всего {shut}")
