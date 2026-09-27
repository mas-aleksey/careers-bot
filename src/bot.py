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

SCORE_SYSTEM = """Ты оцениваешь вакансии под профиль кандидата.

Сто баллов складываются так: стек и домен 40, роль и уровень 35, география и
оформление 25. Денег в баллах нет — вилку называют редко, она бы только сдвинула
шкалу всем одинаково.

Блокеры дают ровно 0, а не пониженный балл:
- уровень ниже, чем у кандидата;
- страна найма, откуда кандидата нанять нельзя;
- onsite или обязательная релокация, если кандидат их не рассматривает;
- гибрид, если кандидат его не рассматривает;
- другая профессия.

Пометка «Remote» вместе с регионом (Americas, APAC, US) означает удалёнку внутри
этого региона, а не по миру.

Верни JSON: {"scores": [{"url": "...", "pct": 0-100, "why": "одно предложение"}]}
Оценивай каждую присланную вакансию, порядок сохраняй. why — по-русски, коротко,
именно про совпадение с этим кандидатом.

В ответе только JSON, без пояснений до или после него."""

BATCH = 20
TRIAGE_BATCH = 50

TRIAGE_SYSTEM = """Отсев по названию должности. Тебе дают профессию кандидата и
список вакансий с номерами.

Верни JSON: {"keep": [номера тех, чья должность относится к этой профессии или
к соседней, откуда переходят]}.

Соседние считаются: для аналитика это product manager, product owner, solution
architect; для бэкенда — platform, infrastructure, SRE. Маркетинг, продажи,
поддержка, бухгалтерия, рекрутинг — не относятся ни к чему из этого.
Сомневаешься — оставляй."""


def triage(profile, rows):
    """Кто вообще из этой профессии. Дешёвая модель, только заголовки."""
    import llm
    listing = "\n".join(f"{i+1}. {t}" for i, (_, _, t, _) in enumerate(rows))
    out = llm.ask_json(
        TRIAGE_SYSTEM,
        f"ПРОФЕССИЯ: {profile.get('role', '')}, уровень {profile.get('level', '')}\n\n"
        f"ВАКАНСИИ:\n{listing}",
        model=llm.TRIAGE_MODEL, max_tokens=1500)
    keep = {int(n) for n in out.get("keep", []) if str(n).isdigit()}
    return [r for i, r in enumerate(rows, 1) if i in keep]


def score_batch(profile, rows):
    """rows: [(url, company, title, location)] -> [(url, pct, why)]"""
    import llm
    jobs_txt = "\n".join(
        f'{i+1}. url={u} | {c} | {t} | {loc or "локация не указана"}'
        for i, (u, c, t, loc) in enumerate(rows))
    out = llm.ask_json(SCORE_SYSTEM,
                       "ПРОФИЛЬ:\n" + json.dumps(profile, ensure_ascii=False) +
                       "\n\nВАКАНСИИ:\n" + jobs_txt,
                       max_tokens=4000)
    known = {u for u, *_ in rows}
    return [(x["url"], int(x.get("pct", 0)), str(x.get("why", ""))[:200])
            for x in out.get("scores", []) if x.get("url") in known]


def score_pending(conn, tg_id, limit=200):
    """Оценивает то, что для этого профиля ещё не оценено. Закрытые пропускаем:
    платить за оценку снятой вакансии незачем."""
    profile = get_profile(conn, tg_id)
    if not profile:
        return 0
    rows = conn.execute("""
        SELECT j.url, j.company, j.title, j.location FROM jobs j
        LEFT JOIN matches m ON m.job_url = j.url AND m.tg_id = ?
        WHERE m.job_url IS NULL AND j.closed_at IS NULL
        ORDER BY j.posted DESC NULLS LAST LIMIT ?""", (tg_id, limit)).fetchall()
    # Первый проход дешёвой моделью: отсеянным сразу ноль, без оплаты основной.
    survivors = []
    for i in range(0, len(rows), TRIAGE_BATCH):
        part = rows[i:i + TRIAGE_BATCH]
        try:
            keep = triage(profile, part)
        except Exception as e:
            log("triage-error", tg_id, repr(e))
            keep = part                      # не смогли отсеять — оцениваем всё
        survivors += keep
        for url, *_ in part:
            if url not in {u for u, *_ in keep}:
                conn.execute("INSERT OR REPLACE INTO matches(tg_id,job_url,pct,why,scored_at) "
                             "VALUES(?,?,0,'другая профессия',?)", (tg_id, url, now()))
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
        for url, pct, why in scored:
            conn.execute("INSERT OR REPLACE INTO matches(tg_id,job_url,pct,why,scored_at) "
                         "VALUES(?,?,?,?,?)", (tg_id, url, pct, why, now()))
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


def add_company(conn, text):
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
    conn.execute("INSERT OR IGNORE INTO companies(name,page_url,ats,slug,checked_at) "
                 "VALUES(?,?,?,?,?)",
                 (name, url, found[0] if found else None, found[1] if found else None, now()))
    conn.commit()
    if not found:
        return (f"Добавил {name}, но читаемой доски не нашёл — буду следить за "
                f"страницей и замечать изменения." if url else
                f"Добавил {name}. Доску не нашёл и ссылки нет: пришлите ссылку "
                f"на их карьерную страницу через «Добавить компанию».")
    rows = J.ADAPTERS[found[0]](found[1]) or []
    return f"Добавил {name}: доска на {found[0]}, сейчас {len(rows)} вакансий. Оценю в ближайший проход."


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
    # профиль изменился — старые оценки больше не действительны
    conn.execute("DELETE FROM matches WHERE tg_id=?", (tg_id,))
    conn.commit()
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
            except ValueError:
                pass
        rows = conn.execute("""
            SELECT m.job_url, m.pct, m.why FROM matches m
            JOIN jobs j ON j.url = m.job_url
            WHERE m.tg_id = ? AND m.pct >= ? AND j.closed_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM sent s
                              WHERE s.tg_id = m.tg_id AND s.job_url = m.job_url)
            ORDER BY m.pct DESC LIMIT 20""", (tg_id, floor)).fetchall()
        for url, pct, why in rows:
            card = job_card_db(conn, url, pct, why)
            if card and send(tg_id, card, html=True):
                conn.execute("INSERT INTO sent(tg_id,job_url,sent_at) VALUES(?,?,?)",
                             (tg_id, url, now()))
                conn.execute("UPDATE users SET last_notified=? WHERE tg_id=?", (now(), tg_id))
                count += 1
                time.sleep(0.4)
    conn.commit()
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
    c.say(add_company(c.conn, c.arg))


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
        send(chat, add_company(conn, text))
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


COLLECT_EVERY = 12 * 3600
COLLECTOR = str(Path(__file__).with_name("jobs.py"))


SCORE_EVERY = 900


def scorer_loop():
    """Оценка новых вакансий под каждый профиль. Отдельный поток: один вызов
    LLM на двадцать вакансий, при пустой очереди не стоит ничего."""
    while True:
        time.sleep(SCORE_EVERY)
        try:
            conn = db()
            for (tg_id,) in conn.execute("SELECT tg_id FROM profiles"):
                score_pending(conn, tg_id)
            conn.close()
        except Exception as e:
            log("scorer-error", repr(e))


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
    day = conn.execute("SELECT COUNT(*) FROM jobs WHERE first_seen > datetime('now','-1 day')").fetchone()[0]
    closed = conn.execute("SELECT COUNT(*) FROM jobs WHERE closed_at > datetime('now','-1 day')").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    lines = [f"<b>Источники за сутки</b>",
             f"{total} компаний · +{day} вакансий · закрылось {closed}"]
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


def collector_loop():
    """Сборщик вакансий крутится здесь же: отдельный планировщик ради одной
    команды дважды в сутки не нужен, а контейнер и так перезапускается сам."""
    while True:
        try:
            r = subprocess.run([sys.executable, COLLECTOR], capture_output=True,
                               text=True, timeout=3600)
            tail = (r.stdout or r.stderr).strip().split("\n")[-1][:200]
            log("collect", r.returncode, tail)
        except Exception as e:
            log("collect-error", repr(e))
        time.sleep(COLLECT_EVERY)


def main():
    if not TOKEN:
        sys.exit("нет TG_BOT_TOKEN")
    conn = db()
    row = conn.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
    offset = int(row[0]) if row else 0
    log("start")
    setup_commands(conn)
    if Path(COLLECTOR).exists():
        threading.Thread(target=collector_loop, daemon=True).start()
    threading.Thread(target=scorer_loop, daemon=True).start()
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
