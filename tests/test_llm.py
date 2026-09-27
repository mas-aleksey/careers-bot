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
