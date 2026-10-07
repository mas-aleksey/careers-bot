#!/usr/bin/env python3
"""Сборщик вакансий: реестр компаний -> публичные API одиннадцати ATS -> SQLite.

Компании добавляются через бота и живут в таблице companies. У кого нет
читаемой доски — следим за изменением карьерной страницы по хешу.
"""
import ast, hashlib, json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request
import html as html_mod
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import storage


UA = {"User-Agent": "Mozilla/5.0 (careers-bot; +local)"}
# Отдельные бекенды рвут соединение на честном имени бота.
BROWSER_UA = "Mozilla/5.0"
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


def post_json(url, payload, ua=None):
    """GraphQL и подобное GET-ом не отдают: нужен POST с телом запроса.

    ua — подменить User-Agent. Бекенд Aristek рвёт соединение на нашем
    «careers-bot» и отвечает только браузерной строке."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"User-Agent": ua or UA["User-Agent"],
                                          "Content-Type": "application/json"})
    try:
        space_out(url)
        return json.loads(urllib.request.urlopen(req, timeout=TIMEOUT).read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


# --- реестр -----------------------------------------------------------------

def registry_db(conn):
    """Рабочий реестр. Компании добавляются через бота, а не правкой файла."""
    return [(n, u) for n, u in conn.execute(
        "SELECT name, COALESCE(page_url, '') FROM companies ORDER BY name")]


ATS_IN_URL = [
    ("ashby", r"jobs\.ashbyhq\.com/([^/?#]+)"),
    ("greenhouse", r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([^/?#]+)"),
    ("smartrecruiters", r"jobs\.smartrecruiters\.com/([^/?#]+)"),
    ("lever", r"jobs\.lever\.co/([^/?#]+)"),
    ("teamtailor", r"([a-z0-9-]+)\.teamtailor\.com"),
    ("recruitee", r"([a-z0-9-]+)\.recruitee\.com"),
    ("workable", r"apply\.workable\.com/([^/?#]+)"),
    ("personio", r"([a-z0-9-]+)\.jobs\.personio\.de"),
    ("peopleforce", r"(careers\.[a-z0-9.-]+)/v/\d+-"),
    ("revolutpeople", r"([a-z0-9-]+)\.revolutpeople\.com"),
    ("revolutpeople", r"revolutpeople\.com/([a-z0-9-]+)/"),
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
    return [(j["jobUrl"], j["title"], j.get("location", ""), posted(j.get("publishedAt")),
             None, "", plain_text(j.get("descriptionPlain") or j.get("descriptionHtml")))
            for j in d.get("jobs", [])]


def greenhouse(s):
    # content=true отдаёт текст вакансии в том же ответе — без него оценка слепа
    # к стеку и требованиям, а лишнего запроса флаг не стоит.
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs?content=true")
    if not isinstance(d, dict) or "jobs" not in d:
        return None
    return [(j["absolute_url"], j["title"], (j.get("location") or {}).get("name", ""),
             posted(j.get("first_published") or j.get("updated_at")),
             None, "", plain_text(j.get("content"))) for j in d["jobs"]]


def lever_text(j):
    """У Collectly description пустой, а весь текст разложен по lists —
    «Key Responsibilities», «Required Qualifications» и так далее. Заголовок
    блока склеиваем с телом, он несёт смысл не меньше самого списка."""
    body = j.get("descriptionPlain") or j.get("description")
    if not body:
        body = " ".join(" ".join(filter(None, (l.get("text"), l.get("content"))))
                        for l in j.get("lists") or [])
    return plain_text(" ".join(filter(None, (body, j.get("additionalPlain")))))


def lever(s):
    """У европейских аккаунтов свой хост: CoinsPaid живёт на api.eu.lever.co,
    на api.lever.co его нет вовсе."""
    for host in ("api.lever.co", "api.eu.lever.co"):
        d = get(f"https://{host}/v0/postings/{s}?mode=json")
        if isinstance(d, list) and d:
            return [(j["hostedUrl"], j["text"], j.get("categories", {}).get("location", ""),
                     posted(j.get("createdAt")), None, "", lever_text(j))
                    for j in d]
    return None


# Текста в списке SmartRecruiters нет, он лежит в карточке — отдельный запрос
# на вакансию. companyDescription пропускаем: одна и та же реклама компании во
# всех вакансиях, в оценку не входит, а лимит в 12 тысяч символов съедает.
SR_SECTIONS = ("jobDescription", "qualifications", "additionalInformation")


def sr_place(loc):
    """fullLocation — единственное поле со страной словом: country отдаёт код
    («London gb»), и гео-ворота, устроенные на названиях, его не видят. У Gcore
    страны ещё и разъезжаются по city/region/address, в fullLocation они собраны.
    Пустые куски там встречаются («Serbia, , Cyprus») — выкидываем."""
    full = loc.get("fullLocation") or ""
    parts = [p.strip() for p in full.split(",") if p.strip()]
    if parts:
        return ", ".join(dict.fromkeys(parts))[:120]
    return f"{loc.get('city','')} {loc.get('country','')}".strip()


def sr_text(s, jid):
    d = get(f"https://api.smartrecruiters.com/v1/companies/{s}/postings/{jid}")
    sections = ((d or {}).get("jobAd") or {}).get("sections") or {}
    return plain_text(" ".join(filter(None, (
        (sections.get(k) or {}).get("text") for k in SR_SECTIONS))))


def smartrecruiters(s):
    d = get(f"https://api.smartrecruiters.com/v1/companies/{s}/postings")
    if not isinstance(d, dict) or not d.get("totalFound"):
        return None
    out = []
    for j in d.get("content", []):
        out.append((f"https://jobs.smartrecruiters.com/{s}/{j['id']}", j["name"],
                    sr_place(j.get("location") or {}),
                    posted(j.get("releasedDate")), None, "", sr_text(s, j["id"])))
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
                    posted(j.get("published_on") or j.get("created_at")), None, "",
                    plain_text(j.get("description"))))
    return res


_wk_suffix = {}


def workable(s):
    # аккаунты часто заведены с суффиксом: cloudlinux пустой, cloudlinux-1 — 84
    # вакансии. Найденный запоминаем: три пробы каждый цикл — это и есть тот
    # всплеск, на котором Cloudflare отвечает 1015 и компания пропадает.
    empty, throttled = False, False
    for cand in ([_wk_suffix[s]] if s in _wk_suffix else (s, f"{s}-1", f"{s}-2")):
        # details=true отдаёт текст вакансий в том же ответе: отдельный запрос
        # на вакансию здесь стоит дорого — зазор к Workable три секунды.
        d, code = get(f"https://apply.workable.com/api/v1/widget/accounts/{cand}?details=true",
                      with_code=True)
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
    # Описание и требования лежат разными полями — модели нужны оба.
    return [(j["careers_url"], j["title"], f"{j.get('city','')} {j.get('country','')}".strip(),
             posted(j.get("published_at") or j.get("created_at")), None, "",
             plain_text(" ".join(filter(None, (j.get("description"), j.get("requirements"))))))
            for j in d["offers"]]


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
             posted(j.get("date_published")), None, "",
             plain_text(j.get("content_html") or j.get("content_text")))
            for j in items] or None


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
                    j.get("title", ""), place, None, pay,
                    str(j.get("reporting_to") or "")[:80], plain_text(j.get("description"))))
    return out

def personio(s):
    """XML-фид, а не JSON: `<office>` — единственная локация, которую Personio
    отдаёт, и она же в карточке на сайте. Пустой фид от несуществующего
    поддомена не отличить по коду — Personio на оба отвечает 200."""
    body = get(f"https://{s}.jobs.personio.de/xml?language=en", want_json=False)
    if not body:
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    if root.tag != "workzag-jobs":   # 200 с html-страницей — это не пустая доска
        return None
    return [(f"https://{s}.jobs.personio.de/job/{j.findtext('id')}?language=en",
             j.findtext("name") or "", j.findtext("office") or "",
             posted(j.findtext("createdAt")), None, "",
             plain_text(" ".join(v.text or "" for v in j.iter("value"))))
            for j in root.findall("position") if j.findtext("id")]

def plain_text(html, limit=12000):
    """HTML описания вакансии в текст: модели нужен смысл, а не разметка.
    Обрезаем на 12 тысячах символов — у BNP медиана 5.5 тысяч, длинный хвост
    обычно состоит из юридических оговорок, а не из требований."""
    if not html:
        return None
    # Комментарий убираем до тегов: внутри него бывает своя разметка
    # («<!--[if IE]><div>…»), и тогда от него остаётся хвост «-->» в тексте.
    txt = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", txt, flags=re.S)
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", txt)).strip()
    return html_mod.unescape(txt)[:limit] or None


def wordpress(s):
    """WP REST API с типом записи jobs: отдаёт список без ключа и без JS.

    Слаг — host или host/тип: имя типа записи у каждого сайта своё, у BNP это
    jobs, у Akvelon vacancy. Без типа перебираем WP_TYPES, с типом — один запрос.

    У BNP Paribas это единственный читаемый перечень: портал Avature на
    bwelcome.hr.bnpparibas отдаёт карточку по jobId, но страницы со списком
    у него наружу нет. Локации в API нет ни в одном поле — остаётся пустой,
    страну видно по домену сайта."""
    host, _, kind = s.partition("/")
    for kind in ([kind] if kind else WP_TYPES):
        out = _wp_type(host, kind)
        if out:
            # У Akvelon тип vacancy не отдаёт content через REST: поле просто
            # не зарегистрировано. Текст дочитываем со страницы вакансии.
            return page_text(out, f"https://{host}")
    return None


WP_TYPES = ("jobs", "vacancy", "vacancies", "job_listing", "careers")


def _wp_type(host, kind):
    out = []
    for page in range(1, 6):      # 100 на страницу, дальше пятой не ходим
        d = get(f"https://{host}/wp-json/wp/v2/{kind}?per_page=100&page={page}")
        if not isinstance(d, list) or not d:
            break
        for j in d:
            if j.get("lang") not in (None, "en"):   # Polylang дублирует вакансии
                continue
            out.append((j.get("link", ""),
                        html_mod.unescape((j.get("title") or {}).get("rendered", "")),
                        "", posted(j.get("date")), None, "",
                        plain_text((j.get("content") or {}).get("rendered", ""))))
    return out

def revolutpeople(s):
    """Revolut People. Ссылку API не отдаёт, она собирается из слага заголовка
    и id: revolutpeople.com/<tenant>/public/careers/position/<slug>-<id>.

    Локация — поле `name` каждой записи, а не `country`: у Elixi во всех
    вакансиях country=United Kingdom, хотя нанимают в Европе и ОАЭ. По country
    гео-отсев зарубил бы всё как британский онсайт."""
    d = get(f"https://{s}.revolutpeople.com/api/external/v2/postings")
    if not isinstance(d, list):
        return None
    out = []
    for j in d:
        title = j.get("title") or ""
        slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", title.lower())).strip("-")
        where = ", ".join(f"{l.get('name')} ({l.get('type')})"
                          for l in j.get("locations", []) if l.get("name"))
        # Карточка вакансии по API открывается, хотя та же страница на сайте
        # закрыта Cloudflare: текст и дату берём оттуда.
        card = get(f"https://{s}.revolutpeople.com/api/external/v2/postings/{j['id']}") or {}
        out.append((f"https://revolutpeople.com/{s}/public/careers/position/{slug}-{j['id']}",
                    title, where, posted(card.get("creation_date_time")), None, "",
                    plain_text(card.get("description"))))
    return out or None

def aristek(s):
    """WPGraphQL. Боевой сайт на Gatsby, список тянет с бекенда stage —
    так зашито в их же бандле, другого публичного адреса нет.

    В архиве 108 вакансий с 2022 года, все со статусом publish; открытые
    помечены флагом vacancyStatus, их четыре. Без этого фильтра в базу
    поехали бы сто мёртвых карточек."""
    out, after = [], "null"
    for _ in range(5):                    # 100 за запрос, пятой страницы хватит
        d = post_json(f"https://{s}/graphql", ua=BROWSER_UA, payload={"query": (
            "{vacancies(first:100,after:%s){pageInfo{hasNextPage endCursor}"
            "nodes{title slug date vacancyPageCustomFields"
            "{vacancyStatus location content{title text}}}}}" % after)})
        v = ((d or {}).get("data") or {}).get("vacancies")
        if not v:
            return None
        for j in v["nodes"]:
            f = j.get("vacancyPageCustomFields") or {}
            if not f.get("vacancyStatus"):
                continue
            # Ссылка из API ведёт на stage, кандидату нужен боевой адрес.
            # Поле content у самой записи пустое: текст разложен по секциям
            # ACF, заголовок секции склеиваем с телом.
            body = " ".join(" ".join(filter(None, (c.get("title"), c.get("text"))))
                            for c in f.get("content") or [])
            out.append((f"https://aristeksystems.com/career/{j['slug']}/",
                        j.get("title") or "", ", ".join(f.get("location") or []),
                        posted(j.get("date")), None, "", plain_text(body)))
        if not v["pageInfo"]["hasNextPage"]:
            break
        after = json.dumps(v["pageInfo"]["endCursor"])
    return out or None

EPAM_API = "https://careers.epam.com/api/jobs/v2/search/careers-i18n"
EPAM_SIZE = 50                        # size=100 сервер молча урезает до пятидесяти


def epam_page(s, **extra):
    q = {"lang": "en", "sortBy": "relevance;relocation=asc", "websiteLocale": "en-us",
         "size": EPAM_SIZE, **extra}
    d = get(f"{EPAM_API}?{urllib.parse.urlencode(q)}")
    return (d or {}).get("data")


def epam(s):
    """careers.epam.com: страница отдаёт первые десять вакансий из пяти тысяч,
    остальное тянет её же API. Слаг — название страны: отбирать всю EPAM смысла
    нет. Страна задаётся не именем, а id из фасетов, его берём из первого ответа.

    Раньше компания шла через `embedded`, и в базу уезжали favicon и логотипы
    со страницы — своей доски у EPAM нет, а скрейпер брал все ссылки подряд."""
    first = epam_page(s, **{"from": 0})
    if not first:
        return None
    cid = next((c["id"] for c in first["facets"]["country"]
                if str(c.get("key", "")).lower() == s.lower()), None)
    if not cid:
        return []                     # страна есть в реестре, вакансий в ней нет
    out = []
    for page in range(20):            # 50 за запрос; тысячи вакансий в одной стране нет
        d = epam_page(s, **{"from": page * EPAM_SIZE, "facets": f"country={cid}"})
        rows = (d or {}).get("jobs")
        if not rows:
            break
        for j in rows:
            where = ", ".join(c["name"] for c in j.get("country") or [] if c.get("name"))
            if str(j.get("vacancy_type") or "").lower() == "remote" and where:
                where = f"Remote: {where}"
            out.append((f"https://careers.epam.com{(j.get('seo') or {}).get('url', '')}",
                        j.get("name") or "", where, posted(j.get("created_at")),
                        None, "", plain_text(j.get("text") or j.get("description"))))
        if len(out) >= (d.get("total") or 0):
            break
    return out or None


def atlassian(s):
    """Свой эндпоинт поверх iCIMS, ключа не требует — вопреки тому, что
    отвечает соседний /endpoint/careers/listing без «s» на конце.

    Локации приходят в три колена с индексом и повтором страны
    («Bengaluru - India -   Bengaluru,  560071 India»); берём первые два
    и убираем дубль, иначе строка не лезет в карточку и мешает гео-отсеву."""
    d = get(f"https://{s}/endpoint/careers/listings")
    if not isinstance(d, list):
        return None
    out = []
    for j in d:
        post = j.get("portalJobPost") or {}
        places = []
        for loc in j.get("locations") or []:
            parts = [x.strip() for x in loc.split(" - ")[:2] if x.strip()]
            short = " - ".join(dict.fromkeys(parts))
            if short and short not in places:
                places.append(short)
        out.append((post.get("portalUrl") or j.get("applyUrl") or "",
                    j.get("title") or "", ", ".join(places[:4]),
                    str(post.get("updatedDate") or "")[:10] or None, None, "",
                    plain_text(" ".join(filter(None, (j.get("overview"),
                        j.get("responsibilities"), j.get("qualifications")))))))
    return out or None

PF_JOB = re.compile(r'<a class="stretched-link[^"]*"[^>]*href="(?P<href>/v/[^"]+)"[^>]*>'
                    r'(?P<title>[^<]{3,100})</a>')


# У Авиасейлс в карточке сначала отдел, потом должность, и общий linked_jobs
# берёт за название первое — в базу уезжало «Ticket» вместо «Team Lead».
# Разворачивать общее правило нельзя: у Findev и RED Global порядок обратный.
# Имена классов тут от CSS-модулей, хвост после дефиса меняется при пересборке
# сайта — цепляемся за стабильную часть, «team-» и «title-».
AVIASALES_CARD = re.compile(
    r'href="(?P<href>/about/vacancies/\d+)"'
    r'(?:(?!</a>)[\s\S])*?class="[^"]*\btitle-[^"]*"[^>]*>(?P<title>[^<]{3,80})<')


def aviasales(s):
    """Карточки на своей странице: отдел и должность отдельными блоками.
    Локации в списке нет вовсе — она в тексте вакансии, его дочитает page_text."""
    html = get(s, want_json=False)
    if not html:
        return None
    root = re.match(r"https?://[^/]+", s)
    out, seen = [], set()
    for m in AVIASALES_CARD.finditer(html):
        full = root.group(0) + m.group("href")
        if full in seen:
            continue
        seen.add(full)
        out.append((full, html_mod.unescape(m.group("title")).strip(), "", None))
    return page_text(out, s) if out else None


def peopleforce(s):
    """PeopleForce под своим доменом: careers.taxdome.com. Вакансии в вёрстке,
    по десять на страницу, локации на списке нет — только отдел."""
    host = s.split("/")[0]
    out, seen = [], set()
    for page in range(1, 9):                   # у Hivex 60 вакансий по десять
        url = f"https://{s}?page={page}" if "/" in s else f"https://{s}/?page={page}"
        html = get(url, want_json=False)
        if not html:
            break
        # Своя вёрстка на поддомене (TaxDome) и общая на careers-page.com
        # (Hivex) отличаются классами ссылки — вторую забирает linked_jobs.
        found = [(m.group("href"), html_mod.unescape(m.group("title")).strip())
                 for m in PF_JOB.finditer(html)]
        if not found:
            found = [(r[0], r[1]) for r in linked_jobs(html, f"https://{host}")]
        fresh = 0
        for href, title in found:
            full = href if href.startswith("http") else f"https://{host}{href}"
            if full in seen:
                continue
            seen.add(full)
            fresh += 1
            # Текста в списке нет, страница вакансии — обычный HTML без JS.
            # Шапка и подвал попадают в текст вместе с вакансией: отделять их
            # пришлось бы под каждую из двух вёрсток, а модели они не мешают.
            out.append((full, title, "", None, None, "",
                        plain_text(get(full, want_json=False))))
        if not fresh:
            break
    return out or None


ADAPTERS = {"ashby": ashby, "greenhouse": greenhouse, "lever": lever,
            "smartrecruiters": smartrecruiters, "workable": workable,
            "recruitee": recruitee, "teamtailor": teamtailor, "pinpoint": pinpoint,
            "personio": personio,
            "wordpress": wordpress,
            "revolutpeople": revolutpeople,
            "aristek": aristek,
            "atlassian": atlassian,
            "epam": epam,
            "peopleforce": peopleforce,
            "aviasales": aviasales}


def discover(name, url):
    """(ats, slug) по ссылке, иначе перебором по имени. None — своя страница."""
    hit = from_url(url)
    if hit:
        return hit
    for s in slug_variants(name):
        for ats in ("ashby", "greenhouse", "lever", "smartrecruiters", "workable",
                    "recruitee", "teamtailor", "pinpoint", "personio"):
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
                # Текст лежит рядом с заголовком: у BETBY сама карточка ведёт на
                # Sage HR за Cloudflare, и описание оттуда уже не достать.
                found.append((link, title.strip(), str(node.get("location") or
                                                       node.get("city") or "")[:60],
                              None, None, None,
                              plain_text(next((t for t in (node.get("content"), node.get("description"))
                                               if isinstance(t, str)), None))))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return found or None


# Вакансия как обычная ссылка на свой же сайт: /careers/7928089, /jobs/1234.
# Внутри ссылки название и локация отдельными кусками текста.
# Ссылка-кандидат: в пути есть «карьерный» сегмент. Что в хвосте — решает
# job_id_like: одной регуляркой это не выражается без ложных срабатываний.
# Окно тела 1200, а не 600: у Findev карточка с грейдом и списком стран
# длиннее, и на 600 из восьми вакансий читались три.
LINKED_JOB = re.compile(
    r'<a[^>]+href="(?P<href>[^"]*/(?:careers?|jobs?|vacanc\w*|open-positions|positions?)/'
    r'[^"]*)"[^>]*>(?P<body>.{0,1200}?)</a>', re.S)
SEPARATORS = {"—", "-", "–", "·", "|", ",", ""}


# Ступень 3г: на своей странице только заголовок и кнопка «Apply now» на
# внешний ATS. Так устроен Surfe: текст ссылки бесполезен, название вакансии
# стоит в ближайшем заголовке перед ней. Хосты перечислены явно — ловить любой
# /apply/ значит собирать кнопки «откликнуться» со всего сайта.
APPLY_HOSTS = r"app\.dover\.com"
APPLY_LINK = re.compile(
    r"<h[1-4][^>]*>(?P<title>[^<]{3,80})</h[1-4]>"
    r"(?:(?!<h[1-4])[\s\S]){0,800}?"
    r'href="(?P<href>https://(?:' + APPLY_HOSTS + r')/apply/[^"]+)"')


def apply_links(html):
    out, seen = [], set()
    for m in APPLY_LINK.finditer(html):
        href = m.group("href")
        if href in seen:
            continue
        seen.add(href)
        out.append((href, html_mod.unescape(m.group("title")).strip(), "", None))
    return out


# Ступень 3д: Next.js App Router держит данные не в __NEXT_DATA__, а в потоке
# self.__next_f — JSON там экранирован дважды. У Kake вакансии лежат в блоке
# jobsList; вместо разбора всего потока выбираем тройки полей напрямую.
# Локация у них не указана вовсе, ближайшее к ней — timezone (GMT-6).
FLIGHT_JOB = re.compile(
    r'\\"id\\":\\"(?P<id>[0-9a-f-]{36})\\",'
    r'\\"title\\":\\"(?P<title>[^\\]{3,100})\\",'
    r'\\"timezone\\":\\"(?P<tz>[^\\]{0,20})\\"')


# Текст вакансии в потоке лежит отдельной строкой «<ref>:T<длина>,<html>», а в
# карточке стоит только ссылка на неё: "requirementsHtml":"$19". Длина задана в
# байтах — у Kake в тексте неразрывные пробелы, и по символам конец уезжает.
FLIGHT_BLOCK = re.compile(rb"([0-9a-f]+):T([0-9a-f]+),")
# [^{] держит поиск внутри одной карточки: с точкой он перескакивал на соседнюю
# и привязывал к вакансии чужой текст.
FLIGHT_REF = re.compile(r'"id":"([0-9a-f-]{36})"[^{]{0,600}?"requirementsHtml":"\$([0-9a-f]+)"')


def json_loads(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def flight_texts(html):
    """id вакансии -> её текст. Пусто, если поток устроен иначе."""
    stream = "".join(
        chunk for m in re.finditer(r"self\.__next_f\.push\(\[1,(\".*?\")\]\)", html, re.S)
        for chunk in [json_loads(m.group(1))] if isinstance(chunk, str))
    if not stream:
        return {}
    raw = stream.encode()
    blocks = {m.group(1).decode(): raw[m.end():m.end() + int(m.group(2), 16)].decode("utf8", "replace")
              for m in FLIGHT_BLOCK.finditer(raw)}
    return {jid: plain_text(blocks[ref])
            for jid, ref in FLIGHT_REF.findall(stream) if ref in blocks}


def flight_jobs(html, url):
    """Ссылка собирается как <корень>/<id>: у Kake карточка живёт на /jobs/<id>."""
    root = re.match(r"https?://[^/]+(?:/[^/?#]+)*", url)
    if not root:
        return []
    base = root.group(0).rstrip("/")
    texts = flight_texts(html)
    out, seen = [], set()
    for m in FLIGHT_JOB.finditer(html):
        if m.group("id") in seen:
            continue
        seen.add(m.group("id"))
        out.append((f"{base}/{m.group('id')}", html_mod.unescape(m.group("title")),
                    m.group("tz"), None, None, "", texts.get(m.group("id"))))
    return out


NOTION_JOB = re.compile(
    r'https://[a-z.]*notion\.(?:com|so)/(?:p/)?[A-Za-z0-9-]+/'
    r'(?P<slug>[A-Za-z0-9-]+?)-(?P<id>[0-9a-f]{32})')


def notion_jobs(html):
    """Ступень 3в: вакансия — страница в Notion. Так устроен Podscribe: на
    карьерной странице только ссылки в app.notion.com, своих карточек нет.
    Заголовок берём из слага, он же заголовок страницы."""
    out, seen = [], set()
    for m in NOTION_JOB.finditer(html):
        if m.group("id") in seen:
            continue
        seen.add(m.group("id"))
        out.append((m.group(0), m.group("slug").replace("-", " "), "", None))
    return out


def job_id_like(href):
    """Последний сегмент пути похож на id вакансии, а не на раздел сайта.

    Форматов три: голые цифры (/jobs/123456), слаг с числовым хвостом у Findev
    (senior-java-developer-storm-4256535) и буквенно-цифровой код у careers-page
    и RED Global (3W39VW58, 8qTvhsLE). Общее у них — цифра в сегменте, которой
    нет у разделов вроде /careers/about или /career/open-positions/filter."""
    path = href.split("?")[0].split("#")[0].rstrip("/")
    if "/apply/" in path:        # у RED Global та же вакансия ещё и как «Apply now»
        return False
    seg = path.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,60}", seg) or not re.search(r"\d", seg):
        return False
    # Слаг без числового хвоста — это раздел: /jobs/web3-developer-2024-guide.
    return "-" not in seg or bool(re.search(r"-\d{4,}$", seg))


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
        if not job_id_like(href):
            continue
        full = href if href.startswith("http") else root.group(0) + href
        if full in seen:                 # одна вакансия часто и в списке, и в JSON страницы
            continue
        seen.add(full)
        out.append((full, parts[0], parts[-1] if len(parts) > 1 else "", None))
    return out


# Dover отдаёт пустую оболочку SPA, а карточку — своим API, без ключа.
# Путь подсмотрен в их же openapi-бандле: /apply/<клиент>/<id> ведёт сюда.
DOVER_JOB = re.compile(r"app\.dover\.com/apply/[^/]+/([0-9a-f-]{36})")


def dover_job(url):
    """(локация, текст) по ссылке на Dover. (None, None), если это не он."""
    m = DOVER_JOB.search(url)
    if not m:
        return None, None
    d = get(f"https://app.dover.com/api/v1/inbound/application-portal-job/{m.group(1)}")
    if not isinstance(d, dict):
        return None, None
    where = ", ".join(dict.fromkeys(
        l.get("name") for l in d.get("locations") or [] if l.get("name")))
    remote = any(l.get("location_type") == "REMOTE" for l in d.get("locations") or [])
    if remote and where:
        where = f"Remote: {where}"
    return where or None, plain_text(d.get("user_provided_description"))


def page_text(rows, listing):
    """Дочитать текст тем вакансиям, у которых его нет: страница вакансии —
    обычный HTML, и целиком она годится не хуже вырезанного куска.

    Только для известной компании, не при разведке: там половина «вакансий» —
    случайные ссылки, и запрос на каждую обошёлся бы дороже находки."""
    out = []
    for r in rows:
        r = tuple(r) + (None,) * (7 - len(r))
        if not r[6] and r[0] and r[0].startswith("http") and r[0].rstrip("/") != listing.rstrip("/"):
            where, text = dover_job(r[0])
            if where and not r[2]:
                r = r[:2] + (where,) + r[3:]
            if not text:
                page = get(r[0], want_json=False)
                text = plain_text(page) if isinstance(page, str) else None
            # ponytail: порог в 300 символов отсекает оболочку SPA («You need to
            # enable JavaScript»), какую отдаёт Dover у Surfe. Пустое поле честнее
            # мусора: по нему видно, что текста нет. Понадобится тоньше — смотреть
            # на долю букв в строке, а не на длину.
            r = r[:6] + (text if text and len(text) >= 300 else None,)
        out.append(r)
    return out


def embedded(url, with_text=False):
    """Ступень 3: вакансии лежат в HTML, а не в API. Так устроены top.co
    (свой формат), betby.com (__NEXT_DATA__) и xata.io (ссылки на свой сайт)."""
    html = get(url, want_json=False)
    if not html:
        return None
    base = url.rstrip("/")
    out = [(f"{base}/{jid}", title.strip(), company.strip(), None)
           for jid, title, company in JOB_IN_PAYLOAD.findall(html)]
    rows = (out or next_data(html) or linked_jobs(html, url)
            or notion_jobs(html) or apply_links(html) or flight_jobs(html, url))
    return page_text(rows, url) if rows and with_text else rows


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
    # Комментарий убираем до тегов: внутри него бывает своя разметка
    # («<!--[if IE]><div>…»), и тогда от него остаётся хвост «-->» в тексте.
    txt = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", txt, flags=re.S)
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
        return name, ats, slug, embedded(url, with_text=True) or [], None
    if ats:
        # None (доска не ответила) не схлопывать в []: иначе 429 от Workable
        # попадает в отчёт как «доска вернула пустой список» и diagnose молчит
        return name, ats, slug, ADAPTERS[ats](slug), None
    jobs = embedded(url)          # вакансии внутри страницы — ступень 3
    if jobs:
        return name, "embedded", url, jobs, None
    host = urllib.parse.urlparse(url).netloc
    jobs = wordpress(host) if host else None   # ступень 3б: WP REST API сайта
    if jobs:
        return name, "wordpress", host, jobs, None
    return name, None, None, None, page_hash(url)


# Бот перезапускается чаще, чем доски обновляются: сборщик стартует вместе с ним
# и гонит весь реестр заново. Компанию, проверенную недавно, пропускаем.
# Строго меньше интервала обхода (4 часа), иначе плановый запуск сочтёт свежим
# всё, что проверил предыдущий, и пройдёт вхолостую.
FRESH_HOURS = 3


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

    new, changed, stats, closed, to_diagnose = [], [], [], [], []
    reopened = 0
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
                # diagnose ходит в сеть, и внутри открытой транзакции это держало
                # бы блокировку записи до 15 секунд на каждую молчащую доску.
                # Откладываем на после коммита, сюда пишем только счётчик.
                if jobs == []:
                    why = "доска вернула пустой список"
                else:
                    why, to_diagnose = None, to_diagnose + [(name, ats, slug, url)]
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
            # Текст вакансии: многие ATS отдают его в том же ответе, что и список.
            # Оценка по одним заголовку и локации слепа к стеку и требованиям.
            desc = row[6] if len(row) > 6 else None
            if not j_url:
                continue
            cur = conn.execute("INSERT OR IGNORE INTO jobs(url,company,title,location,source,first_seen,posted,salary,contact,dedup,description) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (j_url, name, title, loc, ats, now, pub, pay, who,
                                storage.dedup_key(name, title), desc))
            if not cur.rowcount and who:
                conn.execute("UPDATE jobs SET contact=? WHERE url=? AND contact IS NULL", (who, j_url))
            if not cur.rowcount and loc:
                # локацию адаптер иногда узнаёт позже, чем саму вакансию:
                # у Surfe она пришла только после разбора API Dover
                conn.execute("UPDATE jobs SET location=? WHERE url=? "
                             "AND (location IS NULL OR location='')", (loc, j_url))
            if not cur.rowcount and pay:
                conn.execute("UPDATE jobs SET salary=? WHERE url=? AND salary IS NULL", (pay, j_url))
            if not cur.rowcount and desc:
                conn.execute("UPDATE jobs SET description=? WHERE url=? AND description IS NULL",
                             (desc, j_url))
            if not cur.rowcount and pub:      # вакансия известна, дату узнали позже
                conn.execute("UPDATE jobs SET posted=? WHERE url=? AND posted IS NULL", (pub, j_url))
            if not cur.rowcount:
                # Вернулась на доску под тем же адресом. Такое бывает после
                # разового сбоя выдачи: один проход не показал вакансию, мы её
                # закрыли, а она никуда не девалась. Без этого строка оставалась
                # закрытой навсегда — INSERT OR IGNORE её молча пропускает.
                # Новой не считаем: человек её уже видел, оценка в matches есть.
                reopened += conn.execute(
                    "UPDATE jobs SET closed_at=NULL WHERE url=? AND closed_at IS NOT NULL",
                    (j_url,)).rowcount
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
    # Причины отказов — после коммита: сеть и открытая транзакция не совмещаются.
    # Каждая запись своей короткой транзакцией, между ними база свободна.
    for name, ats, slug, url in to_diagnose:
        why = diagnose(ats, slug, url)
        conn.execute("UPDATE companies SET last_error=? WHERE name=?", (why, name))
        conn.commit()
    return new, changed, stats, skipped, reopened


if __name__ == "__main__":
    new, changed, stats, skipped, reopened = run(sys.argv[1] if len(sys.argv) > 1 else None)
    for name, ats, fresh in sorted(stats, key=lambda x: -x[2])[:15]:
        print(f"{fresh:4}  {name:24} {ats}")
    shut = db().execute("select count(*) from jobs where closed_at is not null").fetchone()[0]
    print(f"\nновых вакансий {len(new)}, страниц изменилось {len(changed)}, "
          f"компаний {len(stats)}, пропущено свежих {skipped}, "
          f"вернулось {reopened}, закрытых всего {shut}")
