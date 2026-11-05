"""AI 제공자 계층: Gemini 실패 → Groq 대체, 서킷 브레이커, 재시도, 스키마 변환."""
import json

import httpx
import pytest

import app.llm as llm
from app.llm import (GEMINI_BASE, GROQ_BASE, AIRouter, AllProvidersFailed, GeminiProvider, GroqProvider,
                     to_gemini_schema)

SCHEMA = {"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"], "additionalProperties": False}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm, "RETRY_DELAY_SEC", 0)


def gemini(handler, fallback=""):
    return GeminiProvider("g-key", "gemini-main", fallback, client=httpx.Client(base_url=GEMINI_BASE,
                                                                               transport=httpx.MockTransport(handler)))


def groq(handler):
    return GroqProvider("x-key", "groq-test", client=httpx.Client(base_url=GROQ_BASE, transport=httpx.MockTransport(handler)))


def gemini_ok(data):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(data)}]}}]})


def groq_ok(data):
    return httpx.Response(200, json={"model": "groq-test", "choices": [{"message": {"content": json.dumps(data)}}]})


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_schema_conversion_for_gemini():
    s = to_gemini_schema(SCHEMA)
    assert s["type"] == "OBJECT" and s["properties"]["x"]["type"] == "NUMBER"
    assert "additionalProperties" not in s
    assert SCHEMA["type"] == "object"  # 원본은 그대로


def test_gemini_rate_limit_falls_through_to_groq():
    calls = {"g": 0, "x": 0}

    def g(req):
        calls["g"] += 1
        assert req.headers["x-goog-api-key"] == "g-key"
        return httpx.Response(429, json={"error": {"message": "quota"}})

    def x(req):
        calls["x"] += 1
        body = json.loads(req.content)
        assert body["response_format"]["type"] == "json_schema"
        assert req.headers["authorization"] == "Bearer x-key"
        return groq_ok({"x": 1})

    router = AIRouter([gemini(g), groq(x)], cooldown_sec=60, clock=FakeClock())
    out = router.generate_json("sys", {"a": 1}, SCHEMA)
    assert out.provider == "groq" and out.data == {"x": 1} and out.model == "groq-test"
    assert calls == {"g": 1, "x": 1}            # 429는 재시도하지 않는다
    assert router.is_open("gemini")             # 한도 초과 → 잠시 쉼


def test_open_circuit_skips_provider_until_cooldown_ends():
    calls = []
    clock = FakeClock()

    def g(req):
        calls.append(1)
        return httpx.Response(429, json={"error": "quota"})

    router = AIRouter([gemini(g)], cooldown_sec=60, clock=clock)
    with pytest.raises(AllProvidersFailed):
        router.generate_json("s", {}, SCHEMA)
    with pytest.raises(AllProvidersFailed) as e:
        router.generate_json("s", {}, SCHEMA)
    assert len(calls) == 1 and "쉬는 중" in str(e.value)
    clock.t += 61
    with pytest.raises(AllProvidersFailed):
        router.generate_json("s", {}, SCHEMA)
    assert len(calls) == 2


def test_transient_error_retries_once_then_uses_fallback_model():
    tried = []

    def g(req):
        model = req.url.path.split("/models/")[1].split(":")[0]
        tried.append(model)
        if model == "gemini-backup":
            return gemini_ok({"x": 2})
        return httpx.Response(503, json={"error": {"message": "high demand"}})

    router = AIRouter([gemini(g, fallback="gemini-backup")], clock=FakeClock())
    out = router.generate_json("s", {}, SCHEMA)
    assert out.model == "gemini-backup" and out.data == {"x": 2}
    assert tried == ["gemini-main", "gemini-main", "gemini-backup"]   # 재시도 1번 → 대체 모델 1번


def test_both_fail_reports_reasons_without_keys():
    router = AIRouter([gemini(lambda r: httpx.Response(500, json={"error": "boom"})),
                       groq(lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}))], clock=FakeClock())
    with pytest.raises(AllProvidersFailed) as e:
        router.generate_json("s", {}, SCHEMA)
    msg = str(e.value)
    assert "gemini" in msg and "groq" in msg and "g-key" not in msg and "x-key" not in msg
    st = {s["provider"]: s for s in router.status()}
    assert st["gemini"]["failed"] == 1 and st["groq"]["failed"] == 1


def test_groq_switches_to_available_model_when_name_is_gone():
    tried = []

    def x(req):
        if req.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "whisper-large-v3"}, {"id": "llama-guard-4-12b"},
                                                      {"id": "llama-3.3-70b-versatile"}]})
        model = json.loads(req.content)["model"]
        tried.append(model)
        if model == "old-model":
            return httpx.Response(404, json={"error": {"message": "The model `old-model` does not exist"}})
        return groq_ok({"x": 3})

    p = GroqProvider("x-key", "old-model", client=httpx.Client(base_url=GROQ_BASE, transport=httpx.MockTransport(x)))
    out = AIRouter([p], clock=FakeClock()).generate_json("s", {}, SCHEMA)
    assert out.data == {"x": 3} and tried == ["old-model", "llama-3.3-70b-versatile"]   # 음성·안전 모델은 건너뜀
    assert p.model == "llama-3.3-70b-versatile"


def test_groq_falls_back_to_json_mode_when_schema_unsupported():
    formats = []

    def x(req):
        body = json.loads(req.content)
        formats.append(body["response_format"]["type"])
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": {"message": "This model does not support response_format `json_schema`"}})
        assert "JSON 스키마" in body["messages"][0]["content"]          # 스키마를 프롬프트로 알려 준다
        return groq_ok({"x": 4})

    p = GroqProvider("x-key", "llama-3.3-70b-versatile",
                     client=httpx.Client(base_url=GROQ_BASE, transport=httpx.MockTransport(x)))
    router = AIRouter([p], clock=FakeClock())
    assert router.generate_json("s", {}, SCHEMA).data == {"x": 4}
    assert router.generate_json("s", {}, SCHEMA).data == {"x": 4}
    assert formats == ["json_schema", "json_object", "json_object"]   # 한 번 바꾸면 기억한다


def test_bad_json_is_a_provider_failure():
    def g(req):
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "not json"}]}}]})

    router = AIRouter([gemini(g)], clock=FakeClock())
    with pytest.raises(AllProvidersFailed) as e:
        router.generate_json("s", {}, SCHEMA)
    assert "응답 형식" in str(e.value)
