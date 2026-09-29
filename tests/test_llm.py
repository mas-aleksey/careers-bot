import json
import pytest
import llm


def test_json_mode_only_when_asked():
    body = llm.build([{"role": "user", "content": "hi"}], json_mode=True)
    assert body["response_format"] == {"type": "json_object"}
    assert "response_format" not in llm.build([{"role": "user", "content": "hi"}])


@pytest.mark.parametrize("raw, expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    # модель объясняет словами и только потом выдаёт JSON — так терялась
    # целая пачка вакансий, пока разбор этого не умел
    ('Все вакансии мимо.\n\n{"scores": []}', {"scores": []}),
    ('текст {"scores": [{"pct": 9}]} хвост', {"scores": [{"pct": 9}]}),
    # ответ не уместился в max_tokens и оборвался: то, что модель успела
    # дописать, надо забрать — иначе пачка из 20 вакансий теряется целиком
    ('{"scores": [{"url": "a", "pct": 9}, {"url": "b", "pct": 5}, {"url": "c", "p',
     {"scores": [{"url": "a", "pct": 9}, {"url": "b", "pct": 5}]}),
    ('Все мимо.\n\n{"scores": [{"url": "a", "pct": 0}, {"url": "b"',
     {"scores": [{"url": "a", "pct": 0}]}),
])
def test_extract_json(raw, expected):
    assert json.loads(llm.extract_json(raw)) == expected


def test_retry_only_on_transient_codes():
    """400 повтором не лечится, 429 и 5xx — лечатся."""
    assert 429 in llm.RETRY_CODES and 503 in llm.RETRY_CODES
    assert 400 not in llm.RETRY_CODES and 404 not in llm.RETRY_CODES


def test_missing_key_fails_fast(monkeypatch):
    monkeypatch.setattr(llm, "KEY", "")
    with pytest.raises(llm.LLMError):
        llm.ask([{"role": "user", "content": "hi"}])


def test_null_choices_retries_not_crashes(monkeypatch):
    """OpenRouter иногда отдаёт choices=null. Это ValueError и ретрай,
    а не TypeError мимо обработки — на нём терялась целая пачка."""
    import io, json as j
    monkeypatch.setattr(llm, "KEY", "x")
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    calls = []

    def fake(req, timeout=None):
        calls.append(1)
        body = {"choices": None} if len(calls) == 1 else {
            "choices": [{"message": {"content": '{"ok": true}'}}]}
        return io.BytesIO(j.dumps(body).encode())

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake)
    assert llm.ask_json("s", "u") == {"ok": True}
    assert len(calls) == 2


def test_space_out_serialises_same_host(monkeypatch):
    """Два запроса к одному хосту разводятся на GAP, к разным — нет."""
    import jobs
    slept = []
    monkeypatch.setattr(jobs.time, "sleep", lambda d: slept.append(d))
    jobs._last.clear()
    jobs.space_out("https://a.com/1")
    jobs.space_out("https://b.com/1")
    assert not slept
    jobs.space_out("https://a.com/2")
    assert slept and 0 < slept[0] <= jobs.GAP
