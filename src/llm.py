#!/usr/bin/env python3
"""Клиент OpenRouter: один POST, ответ строго JSON.

Отдельный модуль, чтобы смена провайдера не растекалась по боту: наружу торчат
две функции — ask() для свободного текста и ask_json() со схемой в промпте.
"""
import json, os, re, time, urllib.error, urllib.request

URL = "https://openrouter.ai/api/v1/chat/completions"
KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-5")
# Первый проход — отсев заведомо чужих профессий. Втрое дешевле не бывает:
# 93% вакансий получают ноль, и платить за них основной моделью незачем.
TRIAGE_MODEL = os.environ.get("OPENROUTER_TRIAGE_MODEL", "google/gemini-2.5-flash-lite")
TIMEOUT = 180
RETRY_CODES = (408, 409, 429, 500, 502, 503, 504)


class LLMError(RuntimeError):
    pass


def build(messages, model=None, max_tokens=4096, json_mode=False):
    """Тело запроса. Вынесено, чтобы проверять формат без сети."""
    body = {"model": model or MODEL, "messages": messages, "max_tokens": max_tokens}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return body


def ask(messages, model=None, max_tokens=4096, json_mode=False, attempts=3):
    if not KEY:
        raise LLMError("нет OPENROUTER_API_KEY")
    data = json.dumps(build(messages, model, max_tokens, json_mode)).encode()
    req = urllib.request.Request(URL, data=data, headers={
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
        # OpenRouter просит их для статистики; на доступ не влияют
        "HTTP-Referer": "https://github.com/tiraill/careers-bot",
        "X-Title": "careers-bot",
    })
    last = None
    for n in range(attempts):
        try:
            raw = urllib.request.urlopen(req, timeout=TIMEOUT).read()
            out = json.loads(raw)
            if "error" in out:
                raise LLMError(str(out["error"])[:200])
            # choices и message бывают null: без явной проверки это TypeError,
            # а он мимо ретраев — пачка терялась с одного битого ответа
            msg = ((out.get("choices") or [None])[0] or {}).get("message") or {}
            if not msg.get("content"):
                raise ValueError(f"пустой ответ: {str(out)[:200]}")
            return msg["content"]
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'ignore')}"
            if e.code not in RETRY_CODES or n == attempts - 1:
                raise LLMError(last)
            time.sleep(2 ** n * 2)
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            last = repr(e)
            if n == attempts - 1:
                raise LLMError(last)
            time.sleep(2 ** n * 2)
    raise LLMError(last or "неизвестная ошибка")


def extract_json(text):
    """Достаёт объект из ответа. Модель может обернуть его в ```json, может
    сначала объяснить словами и только потом выдать JSON, а может не уместиться
    в max_tokens и оборваться на середине массива."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    i = t.find("{")
    if i < 0:
        return t
    try:
        obj, _ = json.JSONDecoder().raw_decode(t, i)
        return json.dumps(obj, ensure_ascii=False)
    except ValueError:
        return salvage(t, i)


def salvage(t, i):
    """Ответ оборвался на середине: собираем элементы массива, которые модель
    успела дописать. Иначе пачка из 20 вакансий теряется целиком, и следующий
    проход оплачивает её заново."""
    m = re.search(r'"(\w+)"\s*:\s*\[', t[i:])
    if not m:
        return t
    dec, items, p = json.JSONDecoder(), [], i + m.end()
    while (p := t.find("{", p)) >= 0:
        try:
            obj, p = dec.raw_decode(t, p)
        except ValueError:
            break
        items.append(obj)
    return json.dumps({m.group(1): items}, ensure_ascii=False)


def ask_json(system, user, model=None, max_tokens=4096):
    """Ответ разбирается в объект. Схему описывать в system — response_format
    гарантирует валидный JSON, но не его форму."""
    text = ask([{"role": "system", "content": system}, {"role": "user", "content": user}],
               model=model, max_tokens=max_tokens, json_mode=True)
    try:
        return json.loads(extract_json(text))
    except ValueError as e:
        raise LLMError(f"не JSON: {e}: {text[:200]}")


if __name__ == "__main__":
    print(ask_json("Отвечай JSON.", 'Верни {"ok": true} и ничего больше.'))
