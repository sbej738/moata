"""작업별 AI 순서와 AI 배차 판단(OR-Tools 후보 → AI 선택 → 코드 검증)."""
from app.config import Settings
from app.llm import AIRouter, LLMError
from tests.conftest import GATE, GATE_2, STATION, TERMINAL, Env, ask, state

NEAR = (37.4515, 129.1618)        # 정문 바로 앞
MID = (37.44015, 129.16904)       # 터미널: 약 1.4km
FAR = (37.43007, 129.17874)       # 삼척역: 약 2.8km


class Named:
    """이름을 고를 수 있는 가짜 AI 제공자. decide(payload)의 결과를 돌려주고 호출을 기록한다."""

    def __init__(self, name, decide, calls):
        self.name, self.model, self.decide, self.calls = name, f"{name}-fake", decide, calls

    def generate_json(self, system, payload, schema):
        self.calls.append(self.name)
        out = self.decide(payload)
        if isinstance(out, Exception):
            raise out
        return out, self.model


def env_with(decide, names=("gemini", "groq")):
    calls = []
    providers = [Named(n, decide, calls) for n in names]
    env = Env(router=AIRouter(providers, cooldown_sec=0))
    env.svc.set_setting("ai_auto_match", False)
    return env, calls


def match_answer(p):
    return {"decisions": [{"candidate_id": p["optimal_plan"][0]["id"], "action": "propose", "reason": "ok"}], "summary": ""}


def confirmed_pair(env):
    a, b = env.student("가람"), env.student("누리")
    ra, rb = ask(a, GATE, STATION), ask(b, GATE_2, TERMINAL)
    env.svc.run_matching()
    gid = state(a)["active"]["group"]["id"]
    a.post(f"/api/groups/{gid}/accept", json={"request_id": ra["id"]})
    b.post(f"/api/groups/{gid}/accept", json={"request_id": rb["id"]})   # 확정 → 배차 판단
    return gid


def test_each_task_uses_its_own_first_provider():
    seen = []

    def decide(p):
        if "optimal_plan" in p:
            seen.append("match")
            return match_answer(p)
        seen.append("predict")
        return {"probability": 0.5, "expected_minutes": 3, "reason": "r", "tip": ""}

    env, calls = env_with(decide)
    a, b = env.student("가람"), env.student("누리")
    ra = ask(a, GATE, STATION)
    a.get(f"/api/requests/{ra['id']}/prediction")
    assert calls[-1] == "gemini"                 # 매칭 예상: Gemini 먼저
    ask(b, GATE_2, TERMINAL)
    env.svc.run_matching()
    assert calls[-1] == "groq"                   # 매칭 판단: Groq 먼저


def test_order_falls_back_when_first_provider_fails():
    def decide(p):
        return match_answer(p)

    calls = []
    groq = Named("groq", lambda p: LLMError("HTTP 429: quota", 429), calls)
    gem = Named("gemini", decide, calls)
    env = Env(router=AIRouter([gem, groq], cooldown_sec=60))
    env.svc.set_setting("ai_auto_match", False)
    a, b = env.student("가람"), env.student("누리")
    ask(a, GATE, STATION)
    ask(b, GATE_2, TERMINAL)
    res = env.svc.run_matching()
    assert calls == ["groq", "gemini"] and res["ai"]["provider"] == "gemini"


def judge_with(pick):
    """pick(group_payload) → 고를 후보 dict. 매칭은 최적 계획 그대로."""
    def decide(p):
        if "optimal_plan" in p:
            return match_answer(p)
        if "groups" in p and "optimal" in p:
            g = p["groups"][0]
            c = pick(g)
            return {"assignments": [{"group": g["id"], "driver": c["driver"], "reason": f"AI가 {c['driver']}를 골랐어요"}],
                    "summary": "공정성 고려"}
        return {"reason": "설명"}
    return decide


def test_ai_can_pick_a_slightly_farther_driver():
    env, calls = env_with(judge_with(lambda g: max(g["candidates"], key=lambda c: c["pickup_km"])))
    near = env.driver("near", *NEAR)
    mid = env.driver("mid", *MID)
    gid = confirmed_pair(env)
    offer = mid.get("/api/driver/state").json()["offer"]
    assert offer and offer["id"] == gid and offer["offer_source"] == "groq"   # 배차 판단: Groq 먼저
    assert offer["offer_reason"].startswith("AI가")
    assert near.get("/api/driver/state").json()["offer"] is None
    d = env.svc.db.one("SELECT * FROM ai_decisions WHERE kind = 'dispatch' AND method = 'judge' ORDER BY id DESC")
    assert d["used_ai"] == 1 and d["result"]["chosen"][str(gid)] != d["result"]["optimal"][str(gid)]


def test_too_far_choice_is_reverted_with_alert():
    env, _ = env_with(judge_with(lambda g: max(g["candidates"], key=lambda c: c["pickup_km"])))
    near = env.driver("near", *NEAR)
    env.driver("far", *FAR)
    gid = confirmed_pair(env)
    assert near.get("/api/driver/state").json()["offer"]["id"] == gid       # 최적(가까운) 기사님으로 되돌림
    a = env.svc.db.one("SELECT * FROM alerts WHERE kind = 'ai_dispatch_far'")
    assert a and a["severity"] == "medium"


def test_unknown_driver_is_a_violation():
    env, _ = env_with(judge_with(lambda g: {"driver": "D99"}))
    near = env.driver("near", *NEAR)
    env.driver("mid", *MID)
    gid = confirmed_pair(env)
    assert near.get("/api/driver/state").json()["offer"]["id"] == gid
    assert env.svc.db.one("SELECT * FROM alerts WHERE kind = 'ai_dispatch_violation'")["severity"] == "high"


def test_ai_failure_uses_optimal_assignment():
    def decide(p):
        if "optimal_plan" in p:
            return match_answer(p)
        if "groups" in p and "optimal" in p:
            return LLMError("HTTP 503: busy", 503)
        return {"reason": "설명"}

    env, _ = env_with(decide)
    near = env.driver("near", *NEAR)
    env.driver("mid", *MID)
    gid = confirmed_pair(env)
    offer = near.get("/api/driver/state").json()["offer"]
    assert offer["id"] == gid
    assert env.svc.db.one("SELECT * FROM alerts WHERE kind = 'ai_dispatch_failed'")


def test_single_driver_does_not_call_judge():
    asked = []

    def decide(p):
        if "optimal_plan" in p:
            return match_answer(p)
        if "groups" in p and "optimal" in p:
            asked.append(1)
        return {"reason": "설명"}

    env, _ = env_with(decide)
    env.driver("only", *NEAR)
    confirmed_pair(env)
    assert asked == []                      # 고를 게 없으면 AI 배차 판단을 부르지 않는다
