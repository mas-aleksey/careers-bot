#!/usr/bin/env python3
"""careers-bot: принимает резюме и настройки, рассылает готовые уведомления.

Намеренно тупой: не ходит в LLM, не решает, подходит ли вакансия, не выполняет
код. Оценку делает Claude отдельно и кладёт результат в .jobs-outbox/<профиль>.md.
Поэтому один процесс на всех безопасен.
"""
import json, os, re, sqlite3, secrets, subprocess, sys, threading, time
import urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import storage
from storage import log, now

TOKEN = os.environ.get("TG_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}"
INVITE_HOURS = 48

HELP = """Что я умею:

/profile — как я понял ваш профиль
/cv — прислать новое резюме файлом
/edit <текст> — поправить профиль словами
/add <ссылка или название> — добавить компанию в отслеживание
/settings — частота уведомлений и порог совпадения
/pause, /resume — приостановить и вернуть уведомления
/cancel — отменить начатое действие"""

ADMIN_HELP = "\n\nАдминское: /invite, /users, /revoke <id>"

WELCOME = "Начнём с резюме — пришлите его файлом."

# Один вопрос за раз: длинное приветствие со списком никто не дочитывает,
# и непонятно, когда на что отвечать.
QUESTIONS = [
    "В какой стране вы находитесь и в каком часовом поясе?",
    "Как готовы оформляться: трудовой договор, B2B-контракт, EOR? Можно несколько.",
    "Рассматриваете релокацию? А гибрид с офисом — сколько дней в неделю приемлемо?",
    "С какой суммы разговор имеет смысл? Ставка в час или в месяц, валюта любая.",
    "Что ищете и чего точно не хотите? Пара строк своими словами.",
]
DONE = "Спасибо, этого хватит. Собираю профиль по резюме и ответам…"


# --- телеграм ---------------------------------------------------------------

def call(method, **params):
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    try:
        return json.load(urllib.request.urlopen(req, timeout=70)).get("result")
    except urllib.error.HTTPError as e:
        log("http-error", method, e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        log("net-error", method, repr(e))
    return None


def keyboard(rows):
    return json.dumps({"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                           for row in rows]})


CANCEL_MENU = [[("Отмена", "go:cancel")]]
PROFILE_MENU = [[("⚙️ Настройки", "set:menu")],
                [("✏️ Поправить профиль", "go:edit")],
                [("➕ Добавить компанию", "go:add")],
                [("📄 Обновить резюме", "go:cv")]]
SETTINGS_MENU = [[("Порог совпадения", "set:th"), ("Уведомления", "set:nf")],
                 [("← к профилю", "go:profile")]]
TH_MENU = [[("50%", "th:50"), ("65%", "th:65"), ("80%", "th:80"), ("90%", "th:90")],
           [("← настройки", "set:menu")]]
# Значения во фронтматтере остаются английскими — по ним ищут; на кнопках русский.
NOTIFY_RU = {"instant": "сразу", "daily": "раз в день", "off": "выключены"}
NF_MENU = [[("Сразу", "nf:instant"), ("Раз в день", "nf:daily"), ("Выключить", "nf:off")],
           [("← настройки", "set:menu")]]


def send(chat, text, html=False, markup=None):
    """HTML, а не MarkdownV2: во втором экранировать нужно 18 символов, включая
    дефис и точку, которые есть в каждом втором заголовке вакансии."""
    return call("sendMessage", chat_id=chat, text=text, disable_web_page_preview="true",
                parse_mode="HTML" if html else None, reply_markup=markup)


def esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def age(iso):
    """«3 дня назад» понятнее, чем «2024-12-01»: решение зависит от свежести."""
    if not iso:
        return None
    try:
        d = (datetime.now(timezone.utc).date() - date.fromisoformat(iso[:10])).days
    except ValueError:
        return None
    if d < 0:
        return None
    if d <= 1:
        return "опубликована сегодня"
    if d < 14:
        return f"опубликована {d} дн. назад"
    if d < 60:
        return f"опубликована {d // 7} нед. назад"
    months = d // 30
    return f"висит {months} мес." if months < 12 else f"висит {months // 12} г. {months % 12} мес."


def who_hires(value):
    """В reporting_to лежит либо имя, либо почта: у Tabby 27 имён и 4 адреса.
    Из адреса имя достаётся однозначно, обратное — нет."""
    v = (value or "").strip()
    if "@" not in v:
        return v or None
    local = v.split("@")[0]
    name = " ".join(p.capitalize() for p in re.split(r"[._]+", local) if p)
    return f"{name} · {v}" if name else v


def job_card_db(conn, url, pct, why):
    row = conn.execute("SELECT company, title, location, posted, salary, contact "
                       "FROM jobs WHERE url=?", (url,)).fetchone()
    if not row:
        return None
    company, title, loc, pub, pay, who = row
    lines = [f'<a href="{esc(url)}">{esc(title)} — {esc(company)}</a>',
             f"<b>Совпадение {pct}%</b>"]
    facts = [x for x in (esc(loc), age(pub), esc(pay)) if x]
    if facts:
        lines.append(" · ".join(facts))
    hiring = who_hires(who)
    if hiring:
        lines.append(f"Нанимает: {esc(hiring)}")
    if why:
        lines.append(esc(why))
    return "\n".join(lines)


def download(file_id, dest):
    f = call("getFile", file_id=file_id)
    if not f:
        return None
    url = f"https://api.telegram.org/file/bot{TOKEN}/{f['file_path']}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.write_bytes(urllib.request.urlopen(url, timeout=60).read())
    except (urllib.error.URLError, OSError):
        return None
    return dest


# --- база и аудит -----------------------------------------------------------

def db():
    return storage.connect()


# --- профиль из резюме -------------------------------------------------------

PROFILE_SYSTEM = """Ты собираешь профиль кандидата для поиска вакансий.
Верни JSON строго такой формы, без пояснений:
{"name": "", "role": "", "level": "junior|middle|senior|lead|staff",
 "location": "", "timezone": "", "employment": "", "relocation": "", "hybrid": "",
 "rate": "", "languages": "", "stack": ["..."], "domains": ["..."],
 "looking_for": ["..."], "avoid": ["..."]}
Правила: level ставь по опыту из резюме. employment, relocation, hybrid, rate
бери из ответов человека, а не из резюме — там их обычно нет. stack — ключевые
технологии, не больше десяти. avoid — то, чего человек точно не хочет; пусто,
если не сказал. Пустые поля оставляй пустой строкой или пустым списком."""


def pdf_text(path, limit=20000):
    """Текст резюме. Без картинок и сканов: если текста нет, вернётся пусто."""
    try:
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)[:limit]
    except Exception as e:
        log("pdf-error", repr(e))
        return ""


def build_profile(conn, tg_id, cv_path):
    """Резюме плюс ответы -> JSON в таблицу. Файл после этого не нужен."""
    import llm
    cv = pdf_text(cv_path) if cv_path and Path(cv_path).exists() else ""
    qa = conn.execute("SELECT question, answer FROM answers WHERE tg_id=? ORDER BY rowid", (tg_id,)).fetchall()
    if not cv and not qa:
        return None
    parts = []
    if cv:
        parts.append("РЕЗЮМЕ:\n" + cv)
    if qa:
        parts.append("ОТВЕТЫ:\n" + "\n".join(f"{q}\n{a}" for q, a in qa))
    data = llm.ask_json(PROFILE_SYSTEM, "\n\n".join(parts), max_tokens=2000)
    conn.execute("INSERT INTO profiles(tg_id,data,updated_at) VALUES(?,?,?) "
                 "ON CONFLICT(tg_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                 (tg_id, json.dumps(data, ensure_ascii=False), now()))
    conn.commit()
    # Новое резюме — тот же случай, что и правка: у нового профиля матчей нет,
    # rescore просто сразу пускает оценку, не дожидаясь таймера.
    rescore(conn, tg_id)
    return data


def get_profile(conn, tg_id):
    row = conn.execute("SELECT data FROM profiles WHERE tg_id=?", (tg_id,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


# --- оценка вакансий ---------------------------------------------------------

SCORE_SYSTEM = """Ты оцениваешь вакансии под профиль кандидата. На входе —
текст вакансии, читай его, а не только заголовок.

Сто баллов складываются так: стек и домен 40, роль и уровень 35, география и
оформление 25. Денег в баллах нет.

Стек сверяется в ОДНУ сторону: покрывает ли кандидат требования вакансии.
Технологии из профиля, которые этой вакансии не нужны, баллов НЕ снимают.
Полное покрытие обязательных требований — это полные 40, даже если прочие
навыки кандидата здесь не задействованы. Технология из раздела «будет плюсом»
весит меньше той, что в обязательных требованиях.

Домены в профиле — список подходящих, а не обязательных. Совпадение по любому
из них плюс, несовпадение само по себе не блокер.

Оценивай по явным требованиям и условиям. Описание компании, миссия и
перечисление льгот на балл не влияют.

Локация в поле — подсказка, а не истина. Она бывает пустой, неполной или
противоречит тексту. Пустое поле локации само по себе НЕ блокер: страну найма
ищи в тексте вакансии. Если текст и поле расходятся — верь тексту.

Пометка «Remote» вместе с регионом (Americas, APAC, US) означает удалёнку внутри
этого региона, а не по миру.

Грейд блокером не является, кроме junior и стажировок. Middle и middle+ senior-
кандидату подойти могут: разрыв снижает балл, но не обнуляет. И наоборот —
вакансия уровнем выше кандидата тоже не блокер, смотри на требования, а не на
слово в названии.

Блокеры дают ровно 0, а не пониженный балл:
- junior-позиция, стажировка или trainee;
- страна найма, откуда кандидата нанять нельзя;
- onsite или обязательная релокация, если кандидат их не рассматривает;
- гибрид, если кандидат его не рассматривает;
- другая профессия.

Верни JSON: {"scores": [{"url": "...", "pct": 0-100, "where": "...", "why": "..."}]}

where — что о стране найма и формате работы сказано в тексте, с опорой на текст,
а не на поле локации. Коротко, до 80 символов.
why — НЕ БОЛЬШЕ 400 символов, две-три фразы по-русски, в таком порядке:
1) что совпало: назови технологии из обязательных требований и домен. Если стек
   в тексте не назван, так и скажи, не достраивай его по названию должности;
2) что не совпало или вызывает сомнение;
3) если балл 0 — блокер прямо и одной фразой.
Пиши плотно, без вводных и повторов: географию не повторяй, она уже в where. Не пересказывай вакансию, пиши про совпадение с ЭТИМ кандидатом.
Общих фраз вроде «хорошая возможность» не используй.

Оценивай каждую присланную вакансию, порядок сохраняй.

В ответе только JSON, без пояснений до или после него."""

BATCH = 6              # с текстом пачка тяжелее: двадцать длинных
                       # описаний в одном запросе оцениваются небрежно
SCORE_CHARS = 3000     # требования лежат в первой половине текста
SCORE_SLICE = 120      # вакансий за один проход: потолок расхода на цикл
WHY_CHARS = 400        # обоснование: выходные токены впятеро дороже входных
WHERE_CHARS = 80       # строка про страну найма и формат
TRIAGE_BATCH = 10      # с текстом пачка тяжелее, и на длинном входе модель
                       # чаще не перечисляет всё, что ей дали
TRIAGE_FLOOR = 30      # ниже — к дорогой модели не пускаем. Замер 2026-10-06
                       # на 400 вакансиях: у всех, кому Sonnet поставил 70 и
                       # выше, балл триажа не опускался ниже 30. Выше ставить
                       # нельзя — на 40 теряются 4 подходящие вакансии из 76
TRIAGE_CHARS = 3000    # медиана первого упоминания стека — 1286-й символ
RESCORE_TOP = 20       # сколько вакансий истории дооценивать после смены
                       # профиля: не полный пересчёт, а верхушка по баллу
                       # дешёвой модели. Замеры 2026-10-06: Sonnet на всю
                       # историю это $2.40, на верхушку — пять центов

TRIAGE_SYSTEM = """Ты отбираешь вакансии для кандидата перед дорогой оценкой.

Смотри ТОЛЬКО на три вещи: профессия, стек и грейд. Географию не оценивай
вообще — её уже проверили до тебя, локацию игнорируй.

Верни JSON: {"scores": [{"url": "...", "pct": 0-100}]}
pct — насколько вакансия профессионально близка кандидату:
- 0 — другая профессия;
- 1-30 — та же область, но стек и задачи не те;
- 31-60 — смежная роль или частичное совпадение стека;
- 61-100 — профессия и стек совпадают.

Соседние профессии считаются своими: для аналитика это product manager,
product owner, solution architect; для бэкенда — platform, infrastructure, SRE.

Грейд: junior и стажировки — 0. Middle и выше штрафом не считается.
Технология из раздела «будет плюсом» весит меньше обязательной.

Оценивай каждую присланную вакансию, порядок сохраняй. Только JSON."""


def triage(profile, rows):
    """Балл по профессии и стеку дешёвой моделью. Возвращает (прошедшие, баллы).

    Невернувшиеся проходят дальше: молчание модели — это «не знаю», а не ноль,
    и терять из-за него вакансию дороже, чем оценить лишнюю."""
    import llm
    listing = "\n\n".join(
        f'{i+1}. url={u}\n   {t}\n   {(d or "")[:TRIAGE_CHARS]}'
        for i, (u, _, t, _, d) in enumerate(rows))
    short = {k: profile.get(k) for k in ("role", "level", "stack", "domains", "looking_for")}
    out = llm.ask_json(
        TRIAGE_SYSTEM,
        "ПРОФИЛЬ: " + json.dumps(short, ensure_ascii=False) + f"\n\nВАКАНСИИ:\n{listing}",
        model=llm.TRIAGE_MODEL, max_tokens=2000)
    known = {r[0] for r in rows}
    seen = {x.get("url"): int(x.get("pct") or 0)
            for x in out.get("scores", []) if x.get("url") in known}
    return [r for r in rows if seen.get(r[0], TRIAGE_FLOOR) >= TRIAGE_FLOOR], seen


def score_batch(profile, rows):
    """rows: [(url, company, title, location, description)] -> [(url, pct, why)]

    Текст идёт блоками, а не одной строкой: модели нужно видеть, где кончается
    поле и начинается описание. Обрезка на SCORE_CHARS — требования лежат в
    первой половине, дальше обычно льготы и юридические оговорки."""
    import llm
    jobs_txt = "\n\n".join(
        f'{i+1}. url={u}\n   компания: {c}\n   должность: {t}\n'
        f'   локация (поле): {loc or "не указана"}\n'
        f'   текст вакансии:\n   {(d or "текста нет")[:SCORE_CHARS]}'
        for i, (u, c, t, loc, d) in enumerate(rows))
    out = llm.ask_json(SCORE_SYSTEM,
                       "ПРОФИЛЬ:\n" + json.dumps(profile, ensure_ascii=False) +
                       "\n\nВАКАНСИИ:\n" + jobs_txt,
                       max_tokens=8000)
    known = {u for u, *_ in rows}
    out_rows = []
    for x in out.get("scores", []):
        if x.get("url") not in known:
            continue
        why = str(x.get("why", "")).strip()[:WHY_CHARS]
        # Стек отдельным полем не просим: он и так назван в why, а выходные
        # токены стоят впятеро дороже входных. География остаётся — поле
        # локации ей не замена, у BNP оно пустое у всех 213 вакансий.
        where = str(x.get("where") or "").strip()[:WHERE_CHARS]
        if where:
            why = f"{why}\n{where}" if why else where
        out_rows.append((x["url"], int(x.get("pct") or 0), why))
    return out_rows


def spread(conn, tg_id, url, pct, why, profile=None, triage_pct=None):
    """Оценка ложится на всю группу дублей разом: одна и та же вакансия под
    десятью ссылками должна и оцениваться, и не отправляться одинаково.

    Но ключ дедупликации — компания и должность без локации, а значит в одной
    группе лежат и «remote», и «San Francisco». Проходной балл на такую группу
    разносить нельзя: удалённая вакансия протащила бы за собой офисную.
    Поэтому при ненулевом балле строки, которые отбрасывают ворота, получают
    свой отказ, а не чужой балл."""
    rows = conn.execute(
        "SELECT url, title, location FROM jobs WHERE dedup = "
        "(SELECT dedup FROM jobs WHERE url = ?) AND closed_at IS NULL", (url,)).fetchall()
    profile = profile if profile is not None else get_profile(conn, tg_id)
    for u, title, loc in rows:
        blocked = gate_reason(profile, title, loc) if (pct and profile) else None
        conn.execute("INSERT OR REPLACE INTO matches"
                     "(tg_id, job_url, pct, why, scored_at, triage_pct) VALUES(?,?,?,?,?,?)",
                     (tg_id, u, 0 if blocked else pct, blocked or why, now(), triage_pct))


# --- ворота до моделей -------------------------------------------------------
# География и грейд решаются поиском по строке, без единого вызова LLM. Триаж
# те же данные читает хуже: из 1768 вакансий с заведомо чужой страной он ловил
# 219, а 1549 доходили до дорогой модели и получали там ноль за ту же географию.

# Справочник мест. Неполный намеренно: незнакомая страна означает «ворота
# промолчали», а не «вакансию выбросили».
PLACES = re.compile(
    r"united states|\busa\b|u\.s\.|canada|mexico|brazil|argentina|chile|colombia|peru"
    r"|latam|americas|india|bengaluru|bangalore|hyderabad|pune|gurgaon|japan|tokyo"
    r"|singapore|china|hong kong|korea|apac|australia|sydney|melbourne|brisbane"
    r"|auckland|new zealand|philippines|vietnam|indonesia|malaysia|thailand|nigeria"
    r"|lagos|kenya|egypt|south africa|dubai|\buae\b|united arab|saudi|\bksa\b|qatar|kuwait"
    r"|israel|turkey|pakistan|san francisco|seattle|austin|new york|boston|chicago"
    r"|denver|atlanta|miami|toronto|vancouver|montreal|gmt-"
    # Код страны отдельным словом: «US - California», «Remote (US only)»,
    # «Kansas City, US». Границы слова обязательны — иначе Belarus и Aarhus
    # читаются как Штаты. Проверено на всех 1005 строках локаций в базе:
    # ловит 34, все действительно американские.
    r"|\bus\b", re.I)

# Наднациональное: годится почти любому европейцу, уточнять по стране не нужно.
SUPRA = r"europe|emea|\beu\b|anywhere|worldwide|global"
# Европа целиком считается допустимой: компании тут часто нанимают через границы,
# и ошибиться в сторону лишней оценки дешевле, чем потерять вакансию.
EUROPE = (r"portugal|porto|lisbo|spain|poland|germany|netherlands|france|italy|czech"
          r"|serbia|cyprus|united kingdom|\buk\b|ireland|sweden|denmark|norway|finland"
          r"|switzerland|austria|belgium|greece|romania|bulgaria|estonia|latvia"
          r"|lithuania|hungary|croatia|slovak|slovenia")
# Пустая строка и голый Remote без страны — не блокер: у BNP Paribas локации нет
# вовсе, страна стоит только в тексте вакансии.
BARE = re.compile(r"^\s*(remote|anywhere|fully remote)?\s*$", re.I)
LOW_GRADE = re.compile(r"\b(junior|jr\.?|intern|internship|trainee|graduate"
                       r"|entry.level|apprentice|working student|werkstudent"
                       r"|стажёр|стажер|младший)\b", re.I)
SENIOR_LEVELS = {"senior", "lead", "staff", "principal"}


def allowed_places(profile):
    """Регулярка допустимых мест или None, если ворота по географии не нужны.

    Готов переезжать — география перестаёт быть блокером вообще."""
    rel = str(profile.get("relocation", "")).lower()
    # Подстрокой «да» ловится «не покиДАет» — сверяем по слову и смотрим на
    # отрицание первым. Пустое поле считаем «не переезжает»: локация в профиле
    # есть всегда, а relocation заполняет модель и иногда коротко.
    movable = not re.search(r"\b(нет|no)\b", rel) and re.search(r"\b(да|yes)\b|готов", rel)
    if movable:
        return None
    where = " ".join(str(profile.get(k, "")) for k in ("location", "timezone", "hybrid"))
    own = [w for w in (EUROPE.split("|")) if re.search(w, where, re.I)]
    return re.compile("|".join(own + [SUPRA, EUROPE]), re.I)


def gate_reason(profile, title, location):
    """Почему вакансию можно отбросить без моделей. None — пропускаем дальше.

    Причина возвращается с найденным словом: молчаливый отказ не отследить при
    ручном разборе, а им мы ловим ошибки фильтров."""
    if str(profile.get("level", "")).lower() in SENIOR_LEVELS:
        m = LOW_GRADE.search(title or "")
        if m:
            return f"грейд: в названии «{m.group(0)}», профиль от senior"
    ok = allowed_places(profile)
    loc = location or ""
    if ok and not BARE.match(loc):
        m = PLACES.search(loc)
        if m and not ok.search(loc):
            return f"локация «{m.group(0)}», подходящих мест в строке нет"
    return None


def backlog_pass(conn, tg_id):
    """Разовый проход по истории после смены профиля: дешёвая модель по всему,
    дорогая — только по верхушке.

    Всё, что не попало в верхушку, получает ноль с указанием балла. Иначе
    обычный цикл подобрал бы их как неоценённые и позвал бы дорогую модель на
    каждую: на сегодняшней базе это 970 вызовов вместо двадцати."""
    profile = get_profile(conn, tg_id)
    if not profile:
        return 0
    rows = conn.execute("""
        SELECT j.url, j.company, j.title, j.location, j.description FROM jobs j
        LEFT JOIN matches m ON m.job_url = j.url AND m.tg_id = ?
        WHERE m.job_url IS NULL AND j.closed_at IS NULL
        GROUP BY j.dedup""", (tg_id,)).fetchall()
    kept = []
    for r in rows:
        why = gate_reason(profile, r[2], r[3])
        if why:
            spread(conn, tg_id, r[0], 0, why, profile=profile)
        else:
            kept.append(r)
    conn.commit()
    scores = {}
    for i in range(0, len(kept), TRIAGE_BATCH):
        part = kept[i:i + TRIAGE_BATCH]
        try:
            _, got = triage(profile, part)
        except Exception as e:
            log("triage-error", tg_id, repr(e))
            got = {}
        scores.update(got)
    # Молчание модели в доборке читаем как ноль, а не как «не знаю»: здесь
    # пропуск стоит вызова дорогой модели, а вакансия всё равно не свежая.
    ranked = sorted(kept, key=lambda r: (scores.get(r[0], 0), r[0]), reverse=True)
    top = {r[0] for r in ranked[:RESCORE_TOP]}
    for r in ranked:
        if r[0] not in top:
            spread(conn, tg_id, r[0], 0,
                   f"история: балл триажа {scores.get(r[0], 0)}, не в верхушке",
                   profile=profile, triage_pct=scores.get(r[0]))
    conn.commit()
    log("backlog", tg_id, f"история {len(rows)}, к дорогой модели {len(top)}")
    return {u: scores.get(u, 0) for u in top}


def inherit_scores(conn, tg_id, profile):
    """Вакансия вернулась на доску под новым адресом — оценка у неё уже есть.

    Ashby у Clera перевыпускает id: та же «Founding Engineer» уходит закрытой и
    тут же заводится заново. Без этого обычный цикл видит её как неоценённую и
    платит за неё второй раз — на сегодняшней базе так оплачено около 400 групп
    дублей на каждый профиль.

    Балл берём наибольший в группе, вместе с его обоснованием: в одной группе
    лежат и удалённая вакансия, и офисная, и гейт в spread разложит их по
    строкам сам."""
    rows = conn.execute("""
        SELECT min(j.url), max(m.pct), m.why FROM jobs j
        JOIN jobs j2 ON j2.dedup = j.dedup
        JOIN matches m ON m.job_url = j2.url AND m.tg_id = ?
        WHERE j.closed_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM matches mm
                          WHERE mm.tg_id = ? AND mm.job_url = j.url)
        GROUP BY j.dedup""", (tg_id, tg_id)).fetchall()
    for url, pct, why in rows:
        spread(conn, tg_id, url, pct, why, profile=profile)
    if rows:
        conn.commit()
        log("inherit", tg_id, f"оценка перенесена на {len(rows)}")
    return len(rows)


def score_pending(conn, tg_id, limit=SCORE_SLICE, known_triage=None):
    """Оценивает то, что для этого профиля ещё не оценено. Закрытые пропускаем:
    платить за оценку снятой вакансии незачем.

    known_triage — вакансии, которые уже прошли триаж в доборке истории. Их не
    гоняем через дешёвую модель второй раз: это не только лишняя пачка, но и
    риск, что недетерминированная модель отсеет то, что сама же отобрала."""
    profile = get_profile(conn, tg_id)
    if not profile:
        return 0
    # Вернувшиеся под новым адресом забирают прежнюю оценку, не доходя до модели.
    inherit_scores(conn, tg_id, profile)
    # По одной вакансии из группы дублей: у Mozilla десять url на одну должность,
    # платить за неё десять раз незачем. Результат раскладывается на всю группу.
    rows = conn.execute("""
        SELECT j.url, j.company, j.title, j.location, j.description FROM jobs j
        LEFT JOIN matches m ON m.job_url = j.url AND m.tg_id = ?
        WHERE m.job_url IS NULL AND j.closed_at IS NULL
        GROUP BY j.dedup
        ORDER BY j.posted DESC NULLS LAST LIMIT ?""", (tg_id, limit)).fetchall()
    # Ворота: география и грейд — до моделей, бесплатно.
    passed, blocked = [], 0
    for r in rows:
        why = gate_reason(profile, r[2], r[3])
        if why:
            spread(conn, tg_id, r[0], 0, why)
            blocked += 1
        else:
            passed.append(r)
    if blocked:
        conn.commit()
        log("gate", tg_id, f"{blocked} из {len(rows)}")
    rows = passed

    # Первый проход дешёвой моделью: отсеянным сразу ноль, без оплаты основной.
    # Баллы копим по всем, включая прошедших: по ним калибруется порог, а
    # внутри batch-цикла они теряются.
    known_triage = known_triage or {}
    survivors = [r for r in rows if r[0] in known_triage]
    triage_pct = dict(known_triage)
    todo = [r for r in rows if r[0] not in known_triage]
    for i in range(0, len(todo), TRIAGE_BATCH):
        part = todo[i:i + TRIAGE_BATCH]
        scores = {}
        try:
            keep, scores = triage(profile, part)
        except Exception as e:
            log("triage-error", tg_id, repr(e))
            keep = part                      # не смогли отсеять — оцениваем всё
        survivors += keep
        triage_pct.update(scores)
        kept = {u for u, *_ in keep}
        for row in part:
            if row[0] not in kept:
                spread(conn, tg_id, row[0], 0,
                       f"профессия и стек мимо: предварительный балл {scores.get(row[0], 0)}",
                       triage_pct=scores.get(row[0], 0))
        # Коммит после каждой пачки: между вызовами модели проходят секунды, и
        # незакрытая транзакция всё это время держит запись в базе — сборщик,
        # рассылка и команды бота ждут её молча.
        conn.commit()
    if len(rows) != len(survivors):
        log("triage", tg_id, f"{len(survivors)} из {len(rows)}")
    rows = survivors

    done, failed = 0, 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        try:
            scored = score_batch(profile, chunk)
            failed = 0
        except Exception as e:
            log("score-error", tg_id, repr(e))
            failed += 1
            if failed >= 3:      # три подряд — что-то с провайдером, ждём прохода
                break
            continue
        # Ответ обрезан по max_tokens — salvage спасает начало пачки, остаток
        # вернётся в следующий проход. Видно здесь, иначе только по счёту.
        if len(scored) < len(chunk):
            log("score-short", tg_id, f"{len(scored)} из {len(chunk)}")
        for url, pct, why in scored:
            spread(conn, tg_id, url, pct, why, triage_pct=triage_pct.get(url))
        conn.commit()
        done += len(scored)
    if done:
        log("scored", tg_id, done)
    return done


# --- добавление компании и правка профиля ------------------------------------

EDIT_SYSTEM = """Ты правишь профиль кандидата по его сообщению.
Верни JSON: {"profile": {...весь профиль целиком...}, "changed": "что изменилось,
одной строкой по-русски"}. Меняй только то, о чём сказал человек, остальные поля
оставь как были. Форма профиля та же, что на входе."""


def add_company(conn, text, tg_id=None):
    """Бот сам ищет доску: у публичного сервиса нет человека на подхвате."""
    import jobs as J
    raw = text.strip()
    url = raw if raw.startswith("http") else ""
    name = raw
    if url:
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        hit = J.from_url(url)
        name = (hit[1] if hit else host.split(".")[0]).replace("-", " ").title()
    exists = conn.execute("SELECT name FROM companies WHERE lower(name)=lower(?)", (name,)).fetchone()
    if exists:
        return f"{exists[0]} уже в списке отслеживания."
    found = J.discover(name, url)
    conn.execute("INSERT OR IGNORE INTO companies(name,page_url,ats,slug,checked_at,added_by,added_at) "
                 "VALUES(?,?,?,?,?,?,?)",
                 (name, url, found[0] if found else None, found[1] if found else None,
                  now(), str(tg_id) if tg_id else None, now()))
    conn.commit()
    if not found:
        return (f"Добавил {name}, но читаемой доски не нашёл — буду следить за "
                f"страницей и замечать изменения." if url else
                f"Добавил {name}. Доску не нашёл и ссылки нет: пришлите ссылку "
                f"на их карьерную страницу через «Добавить компанию».")
    rows = J.ADAPTERS[found[0]](found[1]) or []
    return f"Добавил {name}: доска на {found[0]}, сейчас {len(rows)} вакансий. Оценю в ближайший проход."


def rescore(conn, tg_id):
    """Профиль изменился — прежние оценки под него больше не действительны.

    История не пересчитывается целиком: дорого и незачем. Снимаем оценки со
    всего, что ещё не отправляли, и помечаем это как доборку — дальше
    score_pending прогонит их дешёвой моделью и дооценит дорогой только
    верхушку по её баллу.

    Уже отправленное не трогаем: человек это видел, второй раз не придёт."""
    n = conn.execute(
        "DELETE FROM matches WHERE tg_id = ? AND job_url IN ("
        "  SELECT j.url FROM jobs j WHERE j.closed_at IS NULL"
        "   AND NOT EXISTS (SELECT 1 FROM sent s WHERE s.tg_id = ?"
        "                    AND (s.job_url = j.url OR s.dedup = j.dedup)))",
        (tg_id, tg_id)).rowcount
    conn.execute("INSERT INTO meta(key,value) VALUES(?,'1') "
                 "ON CONFLICT(key) DO UPDATE SET value='1'", (backlog_key(tg_id),))
    conn.commit()
    log("rescore", tg_id, f"снято оценок {n}")
    SCORE_WAKE.set()


def backlog_key(tg_id):
    return f"backlog:{tg_id}"


def apply_edit(conn, tg_id, text):
    import llm
    cur = get_profile(conn, tg_id)
    if not cur:
        return "Профиля ещё нет — сначала пришлите резюме."
    try:
        out = llm.ask_json(EDIT_SYSTEM,
                           "ПРОФИЛЬ:\n" + json.dumps(cur, ensure_ascii=False) +
                           "\n\nПРАВКА:\n" + text, max_tokens=2000)
        data = out.get("profile") or {}
        changed = str(out.get("changed", ""))[:200]
    except Exception as e:
        log("edit-error", tg_id, repr(e))
        return "Не смог применить правку, попробуйте иначе."
    conn.execute("UPDATE profiles SET data=?, updated_at=? WHERE tg_id=?",
                 (json.dumps(data, ensure_ascii=False), now(), tg_id))
    conn.execute("INSERT INTO profile_edits(tg_id,text,changed,created_at) VALUES(?,?,?,?)",
                 (tg_id, text, changed, now()))
    rescore(conn, tg_id)
    return (f"{changed}\n\n" if changed else "") + render_db_profile(conn, tg_id)


# --- доступ -----------------------------------------------------------------

def check_access(conn, tg_id, username, text):
    """(разрешён, ответ). Первый написавший — админ, дальше только по коду."""
    row = conn.execute("SELECT is_admin, paused FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    if row:
        return True, None
    has_any = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    if not has_any:
        conn.execute("INSERT INTO users(tg_id,username,is_admin,joined_at) VALUES(?,?,1,?)",
                     (tg_id, username, now()))
        conn.commit()
        log("admin-created", tg_id, username)
        setup_commands(conn)
        return True, "Вы первый — значит администратор.\n\n" + WELCOME
    code = ""
    m = re.match(r"/start\s+(\S+)", text or "")
    if m:
        code = m.group(1)
    if code:
        inv = conn.execute("SELECT created_by, expires_at, used_by FROM invites WHERE code=?",
                           (code,)).fetchone()
        if inv and not inv[2] and inv[1] > now():
            conn.execute("UPDATE invites SET used_by=? WHERE code=?", (tg_id, code))
            conn.execute("INSERT INTO users(tg_id,username,invited_by,joined_at) VALUES(?,?,?,?)",
                         (tg_id, username, inv[0], now()))
            conn.commit()
            log("invite-used", tg_id, username, code)
            return True, WELCOME
    log("denied", tg_id, username)
    return False, "Доступ по приглашению."


# --- очередь на отправку ----------------------------------------------------

def deliver(conn):
    """Разносит оценённое. Источник — таблица matches, порог и режим берутся из
    профиля, закрытые и уже отправленные пропускаются."""
    count = 0
    for tg_id, paused, last in conn.execute(
            "SELECT tg_id, paused, last_notified FROM users"):
        if paused:
            continue
        row = conn.execute("SELECT min_match, notify FROM profiles WHERE tg_id=?",
                           (tg_id,)).fetchone()
        if not row:
            continue
        floor, mode = row[0] or 70, row[1] or "daily"
        if mode == "off":
            continue
        if mode == "daily" and last:
            try:
                if datetime.fromisoformat(last) > datetime.now(timezone.utc) - timedelta(hours=20):
                    continue
            # TypeError — дата без зоны: сравнение падало бы и уносило с собой
            # рассылку всем остальным, а не только этому пользователю
            except (ValueError, TypeError):
                pass
        rows = conn.execute("""
            SELECT m.job_url, m.pct, m.why FROM matches m
            JOIN jobs j ON j.url = m.job_url
            WHERE m.tg_id = ? AND m.pct >= ? AND j.closed_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM sent s WHERE s.tg_id = m.tg_id
                              AND (s.job_url = m.job_url OR s.dedup = j.dedup))
            GROUP BY j.dedup
            ORDER BY m.pct DESC LIMIT 20""", (tg_id, floor)).fetchall()
        for url, pct, why in rows:
            card = job_card_db(conn, url, pct, why)
            if card and send(tg_id, card, html=True):
                conn.execute("INSERT INTO sent(tg_id,job_url,sent_at,dedup) VALUES(?,?,?,"
                             "(SELECT dedup FROM jobs WHERE url=?))", (tg_id, url, now(), url))
                conn.execute("UPDATE users SET last_notified=? WHERE tg_id=?", (now(), tg_id))
                # Коммит до следующей отправки. С коммитом в конце цикла запись
                # держала бы базу всю рассылку: двадцать карточек на человека,
                # у каждой запрос к телеграму и пауза, — это полминуты на двоих
                # и полчаса на сотне.
                conn.commit()
                count += 1
                time.sleep(0.4)
    return count


# --- команды ----------------------------------------------------------------

def settings_text(conn, tg_id):
    row = conn.execute("SELECT min_match, notify FROM profiles WHERE tg_id=?", (tg_id,)).fetchone()
    floor, mode = (row or (70, "daily"))
    return (f"Порог совпадения: {floor}%\n"
            f"Уведомления: {NOTIFY_RU.get(mode, mode)}\n\n"
            "Что поменять?")


def on_callback(conn, cq):
    """Кнопки настроек. Allowlist проверяется и здесь: callback приходит тем же
    потоком апдейтов и мимо хендлера сообщений."""
    data = cq.get("data", "")
    msg = cq.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    tg_id = cq["from"]["id"]
    known = conn.execute("SELECT 1 FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    if not known:
        call("answerCallbackQuery", callback_query_id=cq["id"], text="Доступ по приглашению")
        return
    note = ""
    if get_profile(conn, tg_id):
        if data.startswith("th:"):
            conn.execute("UPDATE profiles SET min_match=? WHERE tg_id=?", (int(data[3:]), tg_id))
            conn.commit()
            note = f"Порог {data[3:]}%"
        elif data.startswith("nf:"):
            conn.execute("UPDATE profiles SET notify=? WHERE tg_id=?", (data[3:], tg_id))
            conn.commit()
            note = f"Уведомления: {NOTIFY_RU.get(data[3:], data[3:])}"
    call("answerCallbackQuery", callback_query_id=cq["id"], text=note)
    if data == "go:cancel":
        conn.execute("UPDATE users SET awaiting=NULL WHERE tg_id=?", (tg_id,))
        conn.commit()
        call("editMessageText", chat_id=chat, message_id=msg.get("message_id"),
             text="Отменил, ничего не отправляю. /profile — вернуться в меню.")
        log("callback", tg_id, data)
        return
    if data in ("go:add", "go:edit"):
        kind = data.split(":")[1]
        conn.execute("UPDATE users SET awaiting=? WHERE tg_id=?", (kind, tg_id))
        conn.commit()
        ask = ("Пришлите ссылку на карьерную страницу, ссылку на вакансию "
               "или просто название компании." if kind == "add" else
               "Напишите, что поправить. Например: «рассматриваю гибрид в Лиссабоне» "
               "или «ставка от 4000 евро».")
        send(chat, ask, markup=keyboard(CANCEL_MENU))
        log("callback", tg_id, data)
        return
    if data == "go:cv":
        send(chat, "Пришлите файл резюме следующим сообщением — профиль пересоберу.",
             markup=keyboard(CANCEL_MENU))
        log("callback", tg_id, data)
        return
    if data == "go:profile":
        text = (render_db_profile(conn, tg_id) if get_profile(conn, tg_id)
                else "Профиля ещё нет.")[:3500]
        view = PROFILE_MENU
        call("answerCallbackQuery", callback_query_id=cq["id"])
    else:
        text = settings_text(conn, tg_id) if get_profile(conn, tg_id) else "Профиля ещё нет."
        view = {"set:th": TH_MENU, "set:nf": NF_MENU}.get(data, SETTINGS_MENU)
    call("editMessageText", chat_id=chat, message_id=msg.get("message_id"),
         text=text, reply_markup=keyboard(view), parse_mode="HTML")
    log("callback", tg_id, data)


DB_FIELDS = [("role", "Роль"), ("level", "Грейд"), ("location", "Где"),
             ("timezone", "Часовой пояс"), ("employment", "Оформление"),
             ("relocation", "Релокация"), ("hybrid", "Гибрид"), ("rate", "Ставка"),
             ("languages", "Языки")]


def render_db_profile(conn, tg_id):
    d = get_profile(conn, tg_id)
    if not d:
        return "Профиля ещё нет."
    row = conn.execute("SELECT min_match, notify FROM profiles WHERE tg_id=?", (tg_id,)).fetchone()
    floor, mode = (row or (70, "daily"))
    out = [f"<b>{esc(d.get('name') or '—')}</b>"]
    for key, label in DB_FIELDS:
        if d.get(key):
            out.append(f"<b>{label}:</b> {esc(str(d[key]))}")
    for key, label in (("stack", "Стек"), ("domains", "Домены"),
                       ("looking_for", "Ищет"), ("avoid", "Не хочет")):
        if d.get(key):
            out.append(f"<b>{label}:</b> {esc(', '.join(str(x) for x in d[key]))}")
    out.append(f"<b>Уведомления:</b> {NOTIFY_RU.get(mode, mode)}, от {floor}%")
    return "\n".join(out)


# --- команды -----------------------------------------------------------------
# Таблица вместо цепочки elif: одна команда — одна функция, видно, что есть.
# Каждая принимает один и тот же контекст.

class Ctx:
    def __init__(self, conn, chat, tg_id, arg, is_admin):
        self.conn, self.chat, self.tg_id = conn, chat, tg_id
        self.arg, self.is_admin = arg, is_admin

    def say(self, text, **kw):
        return send(self.chat, text, **kw)


def cmd_help(c):
    c.say(HELP + (ADMIN_HELP if c.is_admin else ""))


def cmd_profile(c):
    if get_profile(c.conn, c.tg_id):
        c.say(render_db_profile(c.conn, c.tg_id)[:3500], html=True, markup=keyboard(PROFILE_MENU))
    elif (Path("/tmp") / f"cv-{c.tg_id}.pdf").exists():
        c.say("Резюме получено. Профиль соберётся, когда ответите на вопросы.")
    else:
        c.say("Профиля пока нет. Пришлите резюме файлом — дальше пять вопросов.")


def cmd_cv(c):
    c.say("Пришлите файл резюме следующим сообщением.")


def cmd_settings(c):
    if not get_profile(c.conn, c.tg_id):
        c.say("Профиля ещё нет — настраивать нечего.")
        return
    if not c.arg:
        c.say(settings_text(c.conn, c.tg_id), markup=keyboard(SETTINGS_MENU))
        return
    low = c.arg.lower()
    if low in ("daily", "instant", "off"):
        c.conn.execute("UPDATE profiles SET notify=? WHERE tg_id=?", (low, c.tg_id))
        c.conn.commit()
        c.say(f"Уведомления: {NOTIFY_RU.get(low, low)}." +
              (" Новые вакансии копятся, придут когда включите." if low == "off" else ""))
    elif low.isdigit() and 0 <= int(low) <= 100:
        c.conn.execute("UPDATE profiles SET min_match=? WHERE tg_id=?", (int(low), c.tg_id))
        c.conn.commit()
        c.say(f"Порог {low}%. Ниже этого не присылаю. Уже отправленное не повторяется.")
    else:
        c.say("Не понял. Нужно число 0–100 или daily | instant | off.")


def cmd_add(c):
    if not c.arg:
        c.say("Пришлите ссылку или название после команды, либо нажмите кнопку в /profile.")
        return
    c.say(add_company(c.conn, c.arg, c.tg_id))


def cmd_edit(c):
    if not c.arg:
        c.say("Напишите после команды, что поправить.")
        return
    c.say(apply_edit(c.conn, c.tg_id, c.arg), html=True, markup=keyboard(PROFILE_MENU))


def cmd_pause(c):
    c.conn.execute("UPDATE users SET paused=1 WHERE tg_id=?", (c.tg_id,)); c.conn.commit()
    c.say("Уведомления остановлены. /resume вернёт.")


def cmd_resume(c):
    c.conn.execute("UPDATE users SET paused=0 WHERE tg_id=?", (c.tg_id,)); c.conn.commit()
    c.say("Уведомления снова идут.")


def cmd_invite(c):
    code = secrets.token_urlsafe(9)
    exp = (datetime.now(timezone.utc) + timedelta(hours=INVITE_HOURS)).isoformat(timespec="seconds")
    c.conn.execute("INSERT INTO invites(code,created_by,created_at,expires_at) VALUES(?,?,?,?)",
                   (code, c.tg_id, now(), exp))
    c.conn.commit()
    me = call("getMe") or {}
    link = f"https://t.me/{me.get('username','')}?start={code}" if me.get("username") else code
    c.say(f"Ссылка живёт {INVITE_HOURS} часа:\n{link}")


def cmd_users(c):
    rows = c.conn.execute("SELECT tg_id, username, profile, paused FROM users").fetchall()
    c.say("\n".join(f"{r[0]} @{r[1] or '—'} профиль {r[2] or 'нет'}"
                     f"{' (пауза)' if r[3] else ''}" for r in rows) or "пусто")


def cmd_revoke(c):
    c.conn.execute("DELETE FROM users WHERE tg_id=? AND is_admin=0", (c.arg,))
    c.conn.commit()
    c.say(f"Доступ {c.arg} отозван." if c.conn.total_changes else "Такого нет или это админ.")


HANDLERS = {"/start": cmd_help, "/help": cmd_help, "/profile": cmd_profile, "/cv": cmd_cv,
            "/settings": cmd_settings, "/add": cmd_add, "/edit": cmd_edit,
            "/pause": cmd_pause, "/resume": cmd_resume}
ADMIN_HANDLERS = {"/invite": cmd_invite, "/users": cmd_users, "/revoke": cmd_revoke}


# --- разбор входящего --------------------------------------------------------

def on_document(conn, chat, tg_id, doc):
    """Резюме живёт минуты: текст уедет в профиль, файл удалится."""
    dest = Path("/tmp") / f"cv-{tg_id}.pdf"
    if not download(doc["file_id"], dest):
        send(chat, "Файл не скачался. Пришлите ещё раз — лучше PDF до 20 МБ.")
        return
    log("cv", tg_id, dest.name)
    conn.execute("UPDATE users SET step=1 WHERE tg_id=?", (tg_id,))
    conn.commit()
    send(chat, "Резюме получил. Теперь пять коротких вопросов.\n\n" + QUESTIONS[0])


def on_answer(conn, chat, tg_id, step, text):
    """Ответ на вопрос онбординга. Последний запускает сборку профиля."""
    conn.execute("INSERT INTO answers(tg_id,question,answer,created_at) VALUES(?,?,?,?)",
                 (tg_id, QUESTIONS[step - 1], text, now()))
    if step < len(QUESTIONS):
        conn.execute("UPDATE users SET step=? WHERE tg_id=?", (step + 1, tg_id))
        conn.commit()
        send(chat, f"{step + 1} из {len(QUESTIONS)}. {QUESTIONS[step]}")
        return
    conn.execute("UPDATE users SET step=0 WHERE tg_id=?", (tg_id,))
    conn.commit()
    send(chat, DONE)
    log("onboarding-done", tg_id)
    cv = Path("/tmp") / f"cv-{tg_id}.pdf"
    try:
        data = build_profile(conn, tg_id, cv)
    except Exception as e:
        log("profile-error", tg_id, repr(e))
        send(chat, "Не получилось собрать профиль из этого файла. "
                   "Пришлите резюме текстом или другим PDF: /cv")
        return
    finally:
        cv.unlink(missing_ok=True)          # чужое резюме на диске не держим
    if data:
        conn.execute("UPDATE users SET profile=? WHERE tg_id=?", (str(tg_id), tg_id))
        conn.commit()
        send(chat, render_db_profile(conn, tg_id), html=True, markup=keyboard(PROFILE_MENU))


def on_awaited(conn, chat, tg_id, kind, text):
    """Ответ на кнопку «Добавить компанию» или «Поправить профиль»."""
    conn.execute("UPDATE users SET awaiting=NULL WHERE tg_id=?", (tg_id,))
    conn.commit()
    if kind == "add":
        send(chat, "Ищу доску вакансий, это займёт до минуты…")
        send(chat, add_company(conn, text, tg_id))
    else:
        send(chat, "Правлю профиль…")
        send(chat, apply_edit(conn, tg_id, text), html=True, markup=keyboard(PROFILE_MENU))
    log(f"{kind}-done", tg_id, text[:60])


def handle(conn, msg):
    chat = msg["chat"]["id"]
    tg_id = msg["from"]["id"]
    username = msg["from"].get("username", "")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    ok, reply = check_access(conn, tg_id, username, text)
    if reply:
        send(chat, reply)
    if not ok:
        return
    log("msg", tg_id, username, text[:80])

    if msg.get("document"):
        on_document(conn, chat, tg_id, msg["document"])
        return

    step, waiting, is_admin = conn.execute(
        "SELECT step, awaiting, is_admin FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    free_text = text and not text.startswith("/")
    if step and free_text:
        on_answer(conn, chat, tg_id, step, text)
        return
    if waiting in ("add", "edit") and free_text:
        on_awaited(conn, chat, tg_id, waiting, text)
        return

    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower()
    if waiting and text.startswith("/"):      # передумал и пошёл другой командой
        conn.execute("UPDATE users SET awaiting=NULL WHERE tg_id=?", (tg_id,))
        conn.commit()
    if cmd == "/cancel":
        send(chat, "Отменил." if waiting else "Нечего отменять.")
        return

    fn = HANDLERS.get(cmd) or (ADMIN_HANDLERS.get(cmd) if is_admin else None)
    (fn or cmd_help)(Ctx(conn, chat, tg_id, arg.strip(), is_admin))


COMMANDS = [
    ("profile", "как я понял ваш профиль"),
    ("cv", "прислать новое резюме"),
    ("edit", "поправить профиль словами"),
    ("add", "добавить компанию в отслеживание"),
    ("settings", "частота уведомлений и порог совпадения"),
    ("pause", "остановить уведомления"),
    ("resume", "вернуть уведомления"),
    ("cancel", "отменить начатое действие"),
    ("help", "список команд"),
]
ADMIN_COMMANDS = COMMANDS + [
    ("invite", "ссылка-приглашение на 48 часов"),
    ("users", "кто пользуется ботом"),
    ("revoke", "отозвать доступ по id"),
]


def setup_commands(conn):
    """Меню в клиенте. Админские команды видны только админу: у Bot API для
    этого есть scope на конкретный чат."""
    call("setMyCommands", commands=json.dumps(
        [{"command": c, "description": d} for c, d in COMMANDS]))
    admin = conn.execute("SELECT tg_id FROM users WHERE is_admin=1").fetchone()
    if admin:
        call("setMyCommands",
             commands=json.dumps([{"command": c, "description": d} for c, d in ADMIN_COMMANDS]),
             scope=json.dumps({"type": "chat", "chat_id": admin[0]}))


COLLECT_EVERY = 4 * 3600
RETRY_AFTER_FAIL = 600  # сборщик упал, не дойдя до записи: пауза перед повтором
COLLECTOR = str(Path(__file__).with_name("jobs.py"))


SCORE_EVERY = 900
# Таймер остаётся фоном, а не единственным источником запуска: ждать 15 минут
# после обхода или после правки профиля не из чего. Будят обход, /cv и /edit.
SCORE_WAKE = threading.Event()


def score_once():
    """Один проход оценки по всем профилям. Доборка истории идёт первой и ровно
    один раз на смену профиля: флаг снимаем сразу, чтобы сбой дорогой модели
    её не повторил."""
    conn = db()
    try:
        for (tg_id,) in conn.execute("SELECT tg_id FROM profiles").fetchall():
            known = None
            if conn.execute("SELECT 1 FROM meta WHERE key=?",
                            (backlog_key(tg_id),)).fetchone():
                conn.execute("DELETE FROM meta WHERE key=?", (backlog_key(tg_id),))
                conn.commit()
                known = backlog_pass(conn, tg_id)
            score_pending(conn, tg_id, known_triage=known)
    except Exception as e:
        log("scorer-error", repr(e))
    finally:
        conn.close()


def health_report(conn):
    """Сводка о здоровье источников. Молчание — самое опасное состояние:
    поломка выглядит ровно как спокойный день без новых вакансий."""
    silent = conn.execute("""
        SELECT name, ats, last_error, last_ok FROM companies
        WHERE ats IS NOT NULL AND last_error IS NOT NULL
        ORDER BY name""").fetchall()
    empty = conn.execute("""
        SELECT name FROM companies
        WHERE ats IS NOT NULL AND last_error IS NULL AND COALESCE(last_count, 0) = 0
        ORDER BY name""").fetchall()
    # Считаем вакансии, а не строки. Одна вакансия лежит на доске под несколькими
    # адресами, а Ashby у Clera ещё и перевыпускает id: та же «Founding Engineer»
    # уходит закрытой и заводится заново. По строкам выходило +363/-334 в сутки
    # при настоящих +321/-238.
    # Новой считается та, которой раньше не было вовсе. Перевыпуск id заводит
    # новую строку в старой группе — вакансия при этом не появилась.
    day = conn.execute("""
        SELECT COUNT(DISTINCT j.dedup) FROM jobs j
        WHERE j.first_seen > datetime('now','-1 day')
          AND NOT EXISTS (SELECT 1 FROM jobs p WHERE p.dedup = j.dedup
                          AND p.first_seen <= datetime('now','-1 day'))""").fetchone()[0]
    # Закрытой считаем ту, у которой не осталось ни одной живой копии.
    closed = conn.execute("""
        SELECT COUNT(DISTINCT o.dedup) FROM jobs o
        WHERE o.closed_at > datetime('now','-1 day')
          AND NOT EXISTS (SELECT 1 FROM jobs n
                          WHERE n.dedup = o.dedup AND n.closed_at IS NULL)""").fetchone()[0]
    # Вернувшиеся из закрытых: в приход не попадают (first_seen старый), из
    # ухода выбывают (closed_at снят) — без своей строки их не видно вовсе.
    back = conn.execute("SELECT COUNT(DISTINCT dedup) FROM jobs "
                        "WHERE reopened_at > datetime('now','-1 day')").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    head = f"{total} компаний · +{day} вакансий · закрылось {closed}"
    lines = [f"<b>Источники за сутки</b>", head + (f" · вернулось {back}" if back else "")]
    if silent:
        lines.append("")
        lines.append("⚠️ <b>Не отдают вакансии:</b>")
        for name, ats, err, ok in silent[:10]:
            when = f", последний раз {ok[:10]}" if ok else ", ни разу"
            lines.append(f"· {esc(name)} ({ats}): {esc(err)}{when}")
    if empty:
        lines.append("")
        lines.append("Пусто, но доска отвечает: " + ", ".join(esc(n) for n, in empty[:10]))
    if not silent and not empty:
        lines.append("Все источники отвечают.")
    return "\n".join(lines)


def daily_health(conn):
    """Раз в сутки админу. Чаще — шум, реже — узнаешь о поломке поздно."""
    row = conn.execute("SELECT value FROM meta WHERE key='health_at'").fetchone()
    if row and row[0] > (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat():
        return
    admin = conn.execute("SELECT tg_id FROM users WHERE is_admin=1").fetchone()
    if admin and send(admin[0], health_report(conn), html=True):
        conn.execute("INSERT INTO meta(key,value) VALUES('health_at',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now(),))
        conn.commit()


def collect_due_in(conn):
    """Сколько секунд до следующего обхода. Отсчёт от последней проверки, а не
    от старта процесса: иначе каждая пересборка образа сдвигает расписание, и
    после рестарта вскоре после обхода следующий приходит через восемь часов.

    Отдельного поля не нужно — companies.checked_at обновляется только у
    реально проверенных, и прогон, пропустивший всех как свежих, его не двигает."""
    row = conn.execute("SELECT max(checked_at) FROM companies").fetchone()
    if not row or not row[0]:
        return 0                      # пустой реестр — идём сразу
    try:
        last = datetime.fromisoformat(row[0])
    except ValueError:
        return 0
    passed = (datetime.now(timezone.utc) - last).total_seconds()
    return max(0, COLLECT_EVERY - passed)


def collect_once():
    """Обход доской. Отдельным процессом: падение сборщика не должно уносить
    бота, а его память освобождается вместе с процессом."""
    try:
        r = subprocess.run([sys.executable, COLLECTOR], capture_output=True,
                           text=True, timeout=3600)
        tail = (r.stdout or r.stderr).strip().split("\n")[-1][:200]
        log("collect", r.returncode, tail)
    except Exception as e:
        log("collect-error", repr(e))


def worker_loop():
    """Обход и оценка по очереди, в одном потоке: оба пишут в одну базу, и
    делать это одновременно незачем. Телеграм остаётся в главном потоке и
    отвечает на команды, пока здесь идёт работа."""
    while True:
        conn = db()
        due = collect_due_in(conn) <= 0
        conn.close()
        if due and Path(COLLECTOR).exists():
            collect_once()
        score_once()
        conn = db()
        wait = collect_due_in(conn)
        conn.close()
        # Упавший сборщик не двигает checked_at, срок остаётся просроченным:
        # без паузы цикл дёргал бы доски без остановки.
        if due and wait <= 0:
            wait = RETRY_AFTER_FAIL
        SCORE_WAKE.wait(min(SCORE_EVERY, wait or SCORE_EVERY))
        SCORE_WAKE.clear()


def main():
    if not TOKEN:
        sys.exit("нет TG_BOT_TOKEN")
    conn = db()
    row = conn.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
    offset = int(row[0]) if row else 0
    log("start")
    setup_commands(conn)
    threading.Thread(target=worker_loop, daemon=True).start()
    last_deliver = 0.0
    while True:
        ups = call("getUpdates", offset=offset, timeout=50) or []
        for u in ups:
            offset = u["update_id"] + 1
            try:
                if "message" in u:
                    handle(conn, u["message"])
                elif "callback_query" in u:
                    on_callback(conn, u["callback_query"])
            except Exception as e:                      # бот не должен падать из-за одного апдейта
                log("handler-error", repr(e))
            conn.execute("INSERT INTO meta(key,value) VALUES('offset',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))
            conn.commit()
        if time.time() - last_deliver > 300:
            daily_health(conn)
            n = deliver(conn)
            if n:
                log("delivered", n)
            last_deliver = time.time()


if __name__ == "__main__":
    main()
