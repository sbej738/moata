"""신뢰도·포인트: 예약금, 자동 결제, 직전 취소·노쇼 보상, 낮은 신뢰도 추가 부담, 신뢰도 오르내림, 신고 확인."""
from datetime import datetime, timedelta

from app.fare import apply_trust_surcharge
from tests.conftest import GATE, GATE_2, MARKET, STATION, TERMINAL, Env, ask, state


def user_row(env, client):
    return env.svc.get_user(client.user["id"])


def set_trust(env, client, value):
    with env.svc.db.tx() as c:
        c.execute("UPDATE users SET trust = ? WHERE id = ?", (value, client.user["id"]))


def confirmed_trio(env):
    """세 명(빠른 매칭)을 모아 매칭하고 전원 수락까지."""
    env.svc.set_setting("ai_auto_match", False)
    users = [env.student(n) for n in ("하늘", "바다", "이슬")]
    reqs = [ask(users[0], GATE, STATION), ask(users[1], GATE_2, TERMINAL), ask(users[2], GATE, MARKET)]
    env.svc.run_matching()
    gid = state(users[0])["active"]["group"]["id"]
    return users, reqs, gid


def accept_all(users, reqs, gid):
    for u, r in zip(users, reqs):
        assert u.post(f"/api/groups/{gid}/accept", json={"request_id": r["id"]}).status_code == 200


def test_signup_gives_trust_40_and_500k_points(env):
    a = env.student("가람")
    w = a.get("/api/me/wallet").json()
    assert w["trust"] == 40 and w["points"] == 500000 and w["held"] == 0
    assert w["tx"][0]["kind"] == "grant"


def test_accept_holds_deposit_and_completion_pays_and_raises_trust(env):
    users, reqs, gid = confirmed_trio(env)
    g = state(users[0])["active"]["group"]
    my_share = next(m for m in g["members"] if m["is_me"])["share"]
    accept_all(users, reqs, gid)
    w = users[0].get("/api/me/wallet").json()
    assert w["held"] == my_share and w["points"] == 500000 - my_share   # 예약금 = 예상 몫 100%
    d = env.driver()
    d.post(f"/api/driver/offers/{gid}/accept")
    for u, r in zip(users, reqs):
        u.post(f"/api/requests/{r['id']}/notice-ack")
    for _ in env.svc.get_group_row(gid)["stops"]:
        d.post(f"/api/driver/groups/{gid}/advance")
    g = d.post(f"/api/driver/groups/{gid}/complete", json={"final_fare": 12000}).json()
    paid = {m["request_id"]: g["settlement"]["final"][str(m["request_id"])] for m in g["members"]}
    assert sum(paid.values()) == 12000
    for u, r in zip(users, reqs):
        w = u.get("/api/me/wallet").json()
        assert w["held"] == 0 and w["points"] == 500000 - paid[r["id"]]   # 예약금 풀고 실제 몫만 결제
        assert w["trust"] == 41 and w["rides_done"] == 1                   # 문제없이 탔으니 +1
    assert state(users[0])["history"][0]["request"]["paid_at"]


def test_insufficient_points_blocks_accept(env):
    users, reqs, gid = confirmed_trio(env)
    with env.svc.db.tx() as c:
        c.execute("UPDATE users SET points = 100 WHERE id = ?", (users[0].user["id"],))
    r = users[0].post(f"/api/groups/{gid}/accept", json={"request_id": reqs[0]["id"]})
    assert r.status_code == 402 and "포인트" in r.json()["detail"]


def test_reject_releases_holds_without_penalty(env):
    users, reqs, gid = confirmed_trio(env)
    users[0].post(f"/api/groups/{gid}/accept", json={"request_id": reqs[0]["id"]})
    assert users[0].get("/api/me/wallet").json()["held"] > 0
    users[1].post(f"/api/groups/{gid}/reject", json={"request_id": reqs[1]["id"]})
    w0, w1 = users[0].get("/api/me/wallet").json(), users[1].get("/api/me/wallet").json()
    assert w0["held"] == 0 and w0["points"] == 500000           # 돌려받음
    assert w1["trust"] == 40                                     # 거절한 사람은 감점 없음


def test_late_cancel_penalizes_and_compensates_remaining(env):
    users, reqs, gid = confirmed_trio(env)
    accept_all(users, reqs, gid)
    before = {r["id"]: env.svc.get_request(r["id"])["base_share"] for r in reqs}
    users[2].post(f"/api/requests/{reqs[2]['id']}/cancel")        # 출발 30분 이내(빠른 매칭)
    w2 = users[2].get("/api/me/wallet").json()
    assert w2["trust"] == 30 and w2["held"] == 0                  # −10, 남은 예약금은 돌려받음
    g = state(users[0])["active"]["group"]
    for u, r in zip(users[:2], reqs[:2]):
        new_share = next(m for m in g["members"] if m["request_id"] == r["id"])["share"]
        comp = sum(t["amount"] for t in u.get("/api/me/wallet").json()["tx"] if t["kind"] == "comp_in")
        assert comp == max(0, new_share - before[r["id"]])        # 늘어난 만큼 보상 → 원래 몫 그대로
    comp_out = -sum(t["amount"] for t in w2["tx"] if t["kind"] == "comp_out")
    assert comp_out == sum(t["amount"] for u in users[:2] for t in u.get("/api/me/wallet").json()["tx"] if t["kind"] == "comp_in")


def test_no_show_needs_wait_then_penalizes_and_continues(env):
    users, reqs, gid = confirmed_trio(env)
    accept_all(users, reqs, gid)
    d = env.driver()
    d.post(f"/api/driver/offers/{gid}/accept")
    g = env.svc.get_group_row(gid)
    first = g["stops"][0]["request_id"]
    others = [(u, r) for u, r in zip(users, reqs) if r["id"] != first]
    absent = next(u for u, r in zip(users, reqs) if r["id"] == first)
    assert d.post(f"/api/driver/groups/{gid}/no-show").status_code == 400          # 도착 알림 먼저
    d.post(f"/api/driver/groups/{gid}/arrive")
    r = d.post(f"/api/driver/groups/{gid}/no-show")
    assert r.status_code == 409 and "기다린 뒤" in r.json()["detail"]
    assert d.get("/api/driver/state").json()["trip"]["no_show_wait_min"] == 3   # 화면은 서버 값으로 센다
    env.clock.t += timedelta(minutes=3)                        # 도착 후 3분 기다리면 처리 가능
    g = d.post(f"/api/driver/groups/{gid}/no-show").json()
    assert len(g["members"]) == 2 and g["status"] == "riding"
    w = absent.get("/api/me/wallet").json()
    assert w["trust"] == 20 and w["held"] == 0
    assert env.svc.get_request(first)["no_show"] == 1
    for u, rq in others:                                      # 남은 사람은 원래 몫 기준으로 보상받는다
        u.post(f"/api/requests/{rq['id']}/notice-ack")


def test_low_trust_pays_more_but_still_cheaper_than_solo(env):
    env.svc.set_setting("ai_auto_match", False)
    a, b = env.student("가람"), env.student("누리")
    set_trust(env, b, 0)                                      # 0점이어도 이용 가능
    ra, rb = ask(a, GATE, STATION), ask(b, GATE_2, TERMINAL)
    env.svc.run_matching()
    g = state(a)["active"]["group"]
    low = next(m for m in g["members"] if m["request_id"] == rb["id"])
    me = next(m for m in g["members"] if m["is_me"])
    assert low["low_trust"] and low["surcharge"] == 0.15 and low["trust"] == 0
    assert low["trust_adjust"] > 0 and me["trust_adjust"] == -low["trust_adjust"]   # 더 낸 만큼 내 몫이 준다
    assert low["share"] <= int(low["solo_fare"] * 0.95)                            # 그래도 혼자보다 싸다
    assert sum(m["share"] for m in g["members"]) == g["est_fare"]


def test_surcharge_function_keeps_total_and_cap():
    final = {1: 4000, 2: 3000, 3: 2000}
    new, delta, notes = apply_trust_surcharge(final, {3: 0.15}, {1: 6000, 2: 5000, 3: 2400}, 0.05)
    assert sum(new.values()) == sum(final.values())
    assert new[3] == 2280                                        # 300원 더 내야 하지만 혼자 요금의 95%(2,280원)에서 멈춤
    assert delta[3] == 280 and delta[1] + delta[2] == -280 and notes
    same, _, _ = apply_trust_surcharge(final, {1: 0.15, 2: 0.15, 3: 0.15}, {1: 9000, 2: 9000, 3: 9000})
    assert same == final                                         # 모두 낮으면 나눌 사람이 없어 그대로


def test_recovery_and_weekly_cap(env):
    a = env.student("가람")
    uid = a.user["id"]
    with env.svc.db.tx() as c:
        for _ in range(7):
            env.svc._ride_trust(c, uid, 0)
    assert env.svc.get_user(uid)["trust"] == 45                  # 주 최대 +5
    set_trust(env, a, 10)
    with env.svc.db.tx() as c:
        env.svc._ride_trust(c, uid, 0)
    assert env.svc.get_user(uid)["trust"] == 13                  # 기준점 미만은 회복 +3


def test_confirmed_companion_report_lowers_target_trust(env):
    users, reqs, gid = confirmed_trio(env)
    accept_all(users, reqs, gid)
    rep = users[0].post("/api/reports", json={"group_id": gid, "kind": "companion", "category": "harassment",
                                              "target_request_id": reqs[1]["id"]}).json()
    assert users[1].get("/api/me/wallet").json()["trust"] == 40   # 신고만으로는 안 깎인다
    adm = env.admin()
    assert adm.post(f"/api/admin/reports/{rep['id']}/confirm").json()["confirmed"] == 1
    assert users[1].get("/api/me/wallet").json()["trust"] == 25
    other = users[0].post("/api/reports", json={"group_id": gid, "kind": "fare", "category": "toll"}).json()
    assert adm.post(f"/api/admin/reports/{other['id']}/confirm").status_code == 409


def test_admin_reset_with_demo_companions_and_holds(env):
    """데모 동승자(포인트·신뢰도 내역 있음)가 있고 예약금이 잡힌 상태에서도 운행 기록 초기화가 된다."""
    a = env.student("가람")
    r = ask(a, GATE, STATION)
    a.post("/api/demo/seed-near")
    gid = state(a)["active"]["group"]["id"]
    a.post(f"/api/groups/{gid}/accept", json={"request_id": r["id"]})
    assert a.get("/api/me/wallet").json()["held"] > 0
    adm = env.admin()
    res = adm.post("/api/admin/demo/reset")
    assert res.status_code == 200, res.text
    w = a.get("/api/me/wallet").json()
    assert w["held"] == 0 and w["points"] == 500000          # 잡혀 있던 예약금은 돌려받는다
    assert env.svc.db.scalar("SELECT COUNT(*) FROM users WHERE is_demo = 1 AND email IS NULL") == 0
    assert a.get("/api/me/state").json()["active"] is None


def test_topup(env):
    a = env.student("가람")
    assert a.post("/api/me/points/topup").json()["points"] == 600000
