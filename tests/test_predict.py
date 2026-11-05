"""매칭 예측: 통계 기본 추정치, AI 응답 처리(가짜 응답), 실패 시 대체, 캐시, API."""
import json
import math
from datetime import datetime

import httpx
import pytest

import app.llm as llm
from app.config import Settings
from app.db import Database
from app.llm import GEMINI_BASE, GROQ_BASE, AIRouter, GeminiProvider, GroqProvider
from app.predict import Features, MatchPredictor, baseline
from app.service import TaxiShareService
from tests.conftest import GATE, STATION, TERMINAL, Env, pt

NOW = datetime(2026, 11, 5, 21, 40)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm, "RETRY_DELAY_SEC", 0)


def feat(**kw):
    base = dict(minutes_left=20, compatible_now=0, nearest_now_m=None, history_matches=0, history_minutes=120,
                arrival_per_hour=1.5, accept_rate=0.7, proposals_seen=0, hour=21, weekday="목", seats=1,
                detour_ratio=0.3)
    base.update(kw)
    return Features(**base)


def test_baseline_formula():
    p, m = baseline(feat(), 30)
    lam = 1.5 / 60 * 0.7
    assert math.isclose(p, 1 - math.exp(-lam * 20), rel_tol=1e-9)
    assert 0 < m < 20


def test_more_people_or_time_raise_probability():
    p0, _ = baseline(feat(), 30)
    assert baseline(feat(compatible_now=2), 30)[0] > p0
    assert baseline(feat(minutes_left=40), 30)[0] > p0
    assert baseline(feat(minutes_left=0), 30)[0] == 0


def svc_with(router: AIRouter | None = None):
    router = router if router is not None else AIRouter([])
    return TaxiShareService(Database(":memory:"), Settings(), clock=lambda: NOW,
                            predictor=MatchPredictor(Settings(), router), router=router)


def make_request(svc, name, origin, dest, gender="F"):
    u = svc.create_user(name, gender)
    return svc.create_request(u["id"], pt(origin), pt(dest), type="quick")


def test_waiting_partner_counts_as_compatible():
    svc = svc_with()
    me = make_request(svc, "주현", GATE, STATION)
    alone = svc.predict_request(me["id"])
    make_request(svc, "누리", GATE, TERMINAL)
    make_request(svc, "한결", GATE, TERMINAL, gender="M")  # 성별이 달라 제외
    svc.predictor._cache.clear()
    together = svc.predict_request(me["id"])
    assert together["features"]["compatible_now"] == 1
    assert together["features"]["match_type"] == "quick"
    assert together["probability"] > alone["probability"]
    assert together["source"] == "baseline"


def gemini_router(handler, seen=None):
    def wrapped(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        return handler(request)
    client = httpx.Client(base_url=GEMINI_BASE, transport=httpx.MockTransport(wrapped))
    return GeminiProvider("test-key", "gemini-test", client=client)


def answer(data):
    return lambda request: httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(data, ensure_ascii=False)}]}}]})


def test_ai_answer_is_used_and_clamped():
    seen = []
    ans = {"probability": 0.99, "expected_minutes": 999, "reason": "같은 방향 대기자가 있어요.", "tip": ""}
    svc = svc_with(AIRouter([gemini_router(answer(ans), seen)]))
    me = make_request(svc, "주현", GATE, STATION)
    out = svc.predict_request(me["id"])
    assert out["source"] == "gemini" and out["model"] == "gemini-test"
    assert out["probability"] <= out["baseline"]["probability"] + 0.25 + 1e-9  # 기본값 ±0.25로 제한
    assert out["expected_minutes"] <= out["features"]["minutes_left"]          # 마감 이후는 불가
    body = seen[0]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    sent = json.loads(body["contents"][0]["parts"][0]["text"])
    assert "baseline" in sent and "features" in sent


def test_gemini_quota_then_groq_answers():
    ans = {"probability": 0.5, "expected_minutes": 3, "reason": "Groq 답", "tip": ""}
    g = gemini_router(lambda r: httpx.Response(429, json={"error": "quota"}))
    x = GroqProvider("x", "groq-test", client=httpx.Client(base_url=GROQ_BASE, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(ans)}}]}))))
    svc = svc_with(AIRouter([g, x]))
    me = make_request(svc, "주현", GATE, STATION)
    out = svc.predict_request(me["id"])
    assert out["source"] == "groq" and out["reason"] == "Groq 답"


def test_all_ai_fail_falls_back_to_baseline_and_caches_failure():
    calls = []

    def boom(request):
        calls.append(1)
        return httpx.Response(500, json={"error": "down"})

    svc = svc_with(AIRouter([gemini_router(boom)], cooldown_sec=0))
    me = make_request(svc, "주현", GATE, STATION)
    out = svc.predict_request(me["id"])
    assert out["source"] == "baseline" and out["reason"]
    assert out["error"].startswith("gemini: HTTP 500")
    n = len(calls)
    svc.predict_request(me["id"])           # 30초 안에는 실패한 결과를 다시 쓴다
    assert len(calls) == n


def test_same_situation_uses_cache():
    calls = []

    def ok(request):
        calls.append(1)
        return answer({"probability": 0.5, "expected_minutes": 5, "reason": "캐시", "tip": ""})(request)

    svc = svc_with(AIRouter([gemini_router(ok)]))
    me = make_request(svc, "주현", GATE, STATION)
    svc.predict_request(me["id"])
    svc.predict_request(me["id"])
    assert len(calls) == 1


def test_prediction_api_without_key():
    env = Env()
    a = env.student("주현")
    r = a.post("/api/requests", json={"type": "quick", "origin": GATE, "dest": STATION}).json()
    data = a.get(f"/api/requests/{r['id']}/prediction").json()
    assert 0 <= data["probability"] <= 1 and data["source"] == "baseline"
    assert env.client().get("/api/config").json()["ai_enabled"] is False
