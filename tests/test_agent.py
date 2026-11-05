"""AI 매칭 에이전트(하이브리드): 정상 선택, 규칙 위반, AI 실패, 마감 임박 대기, 대체 비율 경보, 배차 설명."""
from app.llm import AIRouter, LLMError
from tests.conftest import GATE, GATE_2, MARKET, STATION, TERMINAL, Env, ask, state


class FakeAI:
    """payload를 보고 decide(payload) 결과를 JSON으로 돌려주는 가짜 제공자."""

    name = "gemini"
    model = "fake-model"

    def __init__(self, decide):
        self.decide = decide
        self.payloads = []

    def generate_json(self, system, payload, schema):
        self.payloads.append(payload)
        out = self.decide(payload)
        if isinstance(out, Exception):
            raise out
        return out, self.model


def make_env(decide, auto=False):
    ai = FakeAI(decide)
    env = Env(router=AIRouter([ai], cooldown_sec=0))
    if not auto:
        env.svc.set_setting("ai_auto_match", False)
    return env, ai


def alerts(env, kind=None):
    rows = env.svc.db.all("SELECT * FROM alerts ORDER BY id")
    return [a for a in rows if kind is None or a["kind"] == kind]


def two_waiting(env, type="quick", **win):
    a, b = env.student("가람"), env.student("누리")
    ra = ask(a, GATE, STATION, type=type, **win)
    rb = ask(b, GATE_2, TERMINAL, type=type, **win)
    return a, b, ra, rb


def test_ai_proposes_optimal_candidate():
    def decide(p):
        cid = p["optimal_plan"][0]["id"]
        return {"decisions": [{"candidate_id": cid, "action": "propose", "reason": "절감이 커요"}], "summary": "그대로 제안"}

    env, ai = make_env(decide)
    a, b, ra, rb = two_waiting(env)
    res = env.svc.run_matching()
    assert len(res["created"]) == 1 and res["ai"]["used"] and res["ai"]["provider"] == "gemini"
    g = env.svc.get_group_row(res["created"][0])
    assert g["match_method"] == "ai" and g["match_reason"] == "절감이 커요"
    # AI에게는 규칙을 통과한 후보와 익명 id만 간다
    p = ai.payloads[0]
    assert p["candidates"] and all(m["rid"].startswith("R") for c in p["candidates"] for m in c["members"])
    d = env.svc.db.one("SELECT * FROM ai_decisions WHERE kind = 'match' ORDER BY id DESC")
    assert d["used_ai"] == 1 and d["result"]["final_saving_m"] > 0
    assert alerts(env) == []


def test_unknown_or_overlapping_choice_is_not_executed_and_alerts_high():
    def decide(p):
        return {"decisions": [{"candidate_id": "C999", "action": "propose", "reason": "지어낸 그룹"}], "summary": "?"}

    env, _ = make_env(decide)
    two_waiting(env)
    res = env.svc.run_matching()
    assert res["ai"]["violations"]
    # 지어낸 그룹은 실행하지 않고, AI가 언급하지 않은 최적 계획 그룹만 만든다
    assert len(res["created"]) == 1
    assert env.svc.get_group_row(res["created"][0])["match_method"] == "cp-sat"
    high = alerts(env, "ai_violation")
    assert high and high[0]["severity"] == "high"


def test_overlapping_proposals_rejected():
    def decide(p):
        ids = [c["id"] for c in p["candidates"]]
        return {"decisions": [{"candidate_id": i, "action": "propose", "reason": "둘 다"} for i in ids], "summary": ""}

    env, _ = make_env(decide)    # 자동 매칭 꺼짐: 세 명을 모은 뒤 매칭
    users = [env.student(n) for n in ("하늘", "바다", "이슬")]
    ask(users[0], GATE, STATION)
    ask(users[1], GATE_2, TERMINAL)
    ask(users[2], GATE, MARKET)
    res = env.svc.run_matching()
    ids = [rid for gid in res["created"] for rid in env.svc.get_group_row(gid)["member_ids"]]
    assert len(ids) == len(set(ids))                      # 한 사람이 두 그룹에 들어가지 않는다
    assert alerts(env, "ai_violation")


def test_ai_failure_runs_optimal_plan_and_alerts_medium():
    env, _ = make_env(lambda p: LLMError("HTTP 503: busy", 503))
    two_waiting(env)
    res = env.svc.run_matching()
    assert len(res["created"]) == 1 and not res["ai"]["used"] and res["ai"]["error"]
    assert env.svc.get_group_row(res["created"][0])["match_method"] == "cp-sat"
    a = alerts(env, "ai_failed")
    assert a and a[0]["severity"] == "medium"


def test_wait_is_overridden_when_deadline_is_near():
    def decide(p):
        return {"decisions": [{"candidate_id": c["id"], "action": "wait", "reason": "3명을 기다려요"}
                              for c in p["optimal_plan"]], "summary": "기다리기"}

    env, _ = make_env(decide)
    two_waiting(env)                   # 빠른 매칭: 마감까지 5분 → 기다릴 수 없다
    res = env.svc.run_matching()
    assert len(res["created"]) == 1 and res["ai"]["overridden"]
    assert alerts(env, "ai_overwait")[0]["severity"] == "medium"


def test_wait_is_respected_for_reservations():
    def decide(p):
        return {"decisions": [{"candidate_id": c["id"], "action": "wait", "reason": "예약은 여유가 있어요"}
                              for c in p["optimal_plan"]], "summary": "기다리기"}

    env, _ = make_env(decide)
    two_waiting(env, type="reserved", window_start="2026-11-06T09:00", window_end="2026-11-06T09:30")
    res = env.svc.run_matching()
    assert res["created"] == [] and res["ai"]["waited"]
    assert alerts(env, "ai_worse") == []         # 기다린 그룹 절감액도 함께 센다


def test_repeated_fallbacks_raise_high_alert():
    env, _ = make_env(lambda p: {})
    for _ in range(4):
        env.svc._log_decision("match", False, "", "", "cp-sat", "AI 실패", {}, {}, {"attempted_ai": True}, "down")
    env.svc._check_fallback_ratio()
    a = alerts(env, "ai_fallback_ratio")
    assert a and a[0]["severity"] == "high"


def test_auto_match_on_quick_request_uses_agent():
    def decide(p):
        return {"decisions": [{"candidate_id": p["optimal_plan"][0]["id"], "action": "propose", "reason": "바로"}],
                "summary": ""}

    env, ai = make_env(decide, auto=True)
    a, b, ra, rb = two_waiting(env)
    assert state(a)["active"]["request"]["status"] == "proposed"
    assert ai.payloads


def test_dispatch_reason_written_by_ai_once():
    def decide(p):
        if "pickup_km" in p:
            return {"reason": f"픽업까지 {p['pickup_km']}km라 가까워요."}
        return {"decisions": [{"candidate_id": p["optimal_plan"][0]["id"], "action": "propose", "reason": "ok"}],
                "summary": ""}

    env, ai = make_env(decide)
    a, b, ra, rb = two_waiting(env)
    env.svc.run_matching()
    gid = state(a)["active"]["group"]["id"]
    a.post(f"/api/groups/{gid}/accept", json={"request_id": ra["id"]})
    b.post(f"/api/groups/{gid}/accept", json={"request_id": rb["id"]})
    d = env.driver()
    offer = d.get("/api/driver/state").json()["offer"]
    assert offer["offer_source"] == "gemini" and offer["offer_reason"].startswith("픽업까지")
    n = len(ai.payloads)
    d.get("/api/driver/state")
    assert len(ai.payloads) == n                 # 같은 제안에는 다시 부르지 않는다


def test_alert_dedupe_and_ack():
    env, _ = make_env(lambda p: {})
    a1 = env.svc.raise_alert("x", "medium", "같은 경보")
    a2 = env.svc.raise_alert("x", "medium", "같은 경보")
    assert a1 == a2 and alerts(env, "x")[0]["count"] == 2
    adm = env.admin()
    assert adm.post(f"/api/admin/alerts/{a1}/ack").json()["acked_at"]
    assert env.svc.raise_alert("x", "medium", "같은 경보") != a1     # 확인한 뒤에는 새 경보
