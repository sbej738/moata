"""요청 → 매칭 → 수락 → 탑승 전 안내 → 배차 → 운행 → 정산 → 송금 완료 전체 흐름을 API로 확인한다."""
from datetime import datetime

from app.config import Settings
from tests.conftest import GATE, GATE_2, MARKET, STATION, TERMINAL, Env, ask, state


def test_full_flow_quick_match_to_settlement(env):
    a, b, c = env.student("가람"), env.student("누리"), env.student("한결", "M")
    ra = ask(a, GATE, STATION)
    rb = ask(b, GATE_2, TERMINAL)          # 빠른 요청이 들어오면 AI 자동 매칭(키 없음 → 최적화)이 바로 돈다
    ask(c, GATE, TERMINAL)                 # 성별이 달라 같은 그룹에 들어가면 안 된다

    sa = state(a)["active"]
    assert sa["request"]["status"] == "proposed"
    g = sa["group"]
    assert {m["request_id"] for m in g["members"]} == {ra["id"], rb["id"]}
    assert g["match_method"] == "cp-sat"
    assert g["expires_at"]                                    # 빠른 매칭 응답 시간
    names = {m["nickname"] for m in g["members"]}
    assert names == {"나", "누리"}                                     # 동승자끼리는 닉네임·성별·신뢰도만 공유
    other = next(m for m in g["members"] if not m["is_me"])
    assert other["trust"] == 40 and other["gender"] == "F" and other["user_id"] is None
    drops = [s["request_id"] for s in g["stops"] if s["kind"] == "dropoff"]
    assert drops == [rb["id"], ra["id"]]                      # 가까운 터미널(B)이 먼저 내린다
    assert sum(m["share"] for m in g["members"]) == g["est_fare"]
    assert all(m["share"] < m["solo_fare"] for m in g["members"])

    gid = g["id"]
    a.post(f"/api/groups/{gid}/accept", json={"request_id": ra["id"]})
    g = b.post(f"/api/groups/{gid}/accept", json={"request_id": rb["id"]}).json()
    assert g["status"] == "confirmed" and g["waiting_driver"]

    # 운행 가능한 기사님이 생기면 배차 제안 → 수락
    d = env.driver()
    offer = d.get("/api/driver/state").json()["offer"]
    assert offer and offer["id"] == gid and offer["offer_source"] == "rule"
    assert all(m["nickname"].startswith("익명") for m in offer["members"])
    g = d.post(f"/api/driver/offers/{gid}/accept").json()
    assert g["status"] == "riding" and g["driver"]["car_number"]

    # 탑승 전 안내를 확인하지 않은 승객은 탑승 처리할 수 없다
    r = d.post(f"/api/driver/groups/{gid}/advance")
    assert r.status_code == 409 and "안내" in r.json()["detail"]
    assert a.post(f"/api/requests/{ra['id']}/notice-ack").status_code == 200
    assert b.post(f"/api/requests/{rb['id']}/notice-ack").status_code == 200
    for _ in range(len(g["stops"])):
        g = d.post(f"/api/driver/groups/{gid}/advance").json()
    assert g["ready_to_complete"]

    preview = d.post(f"/api/driver/groups/{gid}/preview", json={"final_fare": 9000}).json()
    assert sum(preview["settlement"]["final"].values()) == 9000
    g = d.post(f"/api/driver/groups/{gid}/complete", json={"final_fare": 9000}).json()
    assert g["status"] == "completed"
    assert sum(g["settlement"]["final"].values()) == 9000
    hist = state(a)["history"][0]
    assert hist["request"]["status"] == "done"
    assert a.post(f"/api/requests/{ra['id']}/paid").json()["paid_at"]

    # C는 마감이 지나면 단독 탑승으로 전환된다(기본 fallback = solo)
    env.clock.t = datetime(2026, 11, 5, 21, 46)
    result = env.svc.run_matching(batch=False)
    assert len(result["solo"]) == 1
    sc = state(c)["active"]
    assert sc["request"]["status"] == "confirmed" and sc["group"]["is_solo"]


def test_reject_dissolves_and_prevents_same_pair(env):
    a, b = env.student("별"), env.student("솔")
    ra = ask(a, GATE, STATION)
    ask(b, GATE_2, TERMINAL)
    gid = state(a)["active"]["group"]["id"]
    a.post(f"/api/groups/{gid}/reject", json={"request_id": ra["id"]})
    assert env.svc.get_group_row(gid)["status"] == "cancelled"
    assert state(b)["active"]["request"]["status"] == "waiting"
    assert env.svc.run_matching()["created"] == []


def test_proposal_timeout_dissolves(env):
    a, b = env.student("별"), env.student("솔")
    ra = ask(a, GATE, STATION)
    ask(b, GATE_2, TERMINAL)
    gid = state(a)["active"]["group"]["id"]
    a.post(f"/api/groups/{gid}/accept", json={"request_id": ra["id"]})
    env.clock.t = datetime(2026, 11, 5, 21, 43)       # 빠른 매칭 응답 시간(2분) 초과
    result = env.svc.run_matching(batch=False)
    assert gid in result["dissolved"]
    assert state(a)["active"]["request"]["status"] == "waiting"


def test_cancel_after_confirm_keeps_remaining_group(env):
    env.svc.set_setting("ai_auto_match", False)   # 세 명을 모은 뒤 한 번에 매칭
    users = [env.student(n) for n in ("하늘", "바다", "이슬")]
    reqs = [ask(users[0], GATE, STATION), ask(users[1], GATE_2, TERMINAL), ask(users[2], GATE, MARKET)]
    env.svc.run_matching()
    g = state(users[0])["active"]["group"]
    assert len(g["members"]) == 3
    for u, r in zip(users, reqs):
        u.post(f"/api/groups/{g['id']}/accept", json={"request_id": r["id"]})
    users[2].post(f"/api/requests/{reqs[2]['id']}/cancel")
    g = state(users[0])["active"]["group"]
    assert g["status"] == "confirmed" and len(g["members"]) == 2
    assert sum(m["share"] for m in g["members"]) == g["est_fare"]


def test_validation_errors(env):
    a = env.student("다온")
    bad = a.post("/api/requests", json={"type": "quick", "origin": GATE, "dest": GATE_2})
    assert bad.status_code == 400 and "가깝" in bad.json()["detail"]
    two = a.post("/api/requests", json={"type": "quick", "origin": GATE, "dest": STATION, "seats": 2})
    assert two.status_code == 400 and "동행인" in two.json()["detail"]
    soon = a.post("/api/requests", json={"type": "reserved", "origin": GATE, "dest": STATION,
                                         "window_start": "2026-11-05T21:50", "window_end": "2026-11-05T22:20"})
    assert soon.status_code == 400 and "빠른 매칭" in soon.json()["detail"]
    ask(a, GATE, STATION)
    dup = a.post("/api/requests", json={"type": "quick", "origin": GATE, "dest": TERMINAL})
    assert dup.status_code == 400 and "진행 중" in dup.json()["detail"]


def test_detour_in_minutes_becomes_ratio_of_solo_time(env):
    a = env.student("다온")
    r = ask(a, GATE, STATION, detour_min=5)
    assert r["detour_min"] == 5
    assert abs(r["detour_ratio"] - min(1.0, max(0.05, 300 / r["solo_duration_s"]))) < 1e-9
    a.post(f"/api/requests/{r['id']}/cancel")
    bad = a.post("/api/requests", json={"type": "quick", "origin": GATE, "dest": STATION, "detour_min": 0})
    assert bad.status_code == 422


def test_reservation_up_to_two_weeks(env):
    a = env.student("다온")
    ok = a.post("/api/requests", json={"type": "reserved", "origin": GATE, "dest": STATION,
                                       "window_start": "2026-11-19T09:00", "window_end": "2026-11-19T09:30"})
    assert ok.status_code == 200, ok.text
    a.post(f"/api/requests/{ok.json()['id']}/cancel")
    late = a.post("/api/requests", json={"type": "reserved", "origin": GATE, "dest": STATION,
                                         "window_start": "2026-11-20T09:00", "window_end": "2026-11-20T09:30"})
    assert late.status_code == 400 and "14일" in late.json()["detail"]


def test_reserved_request_waits_for_periodic_matching(env):
    a, b = env.student("가람"), env.student("누리")
    win = {"window_start": "2026-11-06T09:00", "window_end": "2026-11-06T09:30"}
    ask(a, GATE, STATION, type="reserved", **win)
    ask(b, GATE_2, TERMINAL, type="reserved", **win)
    assert state(a)["active"]["request"]["status"] == "waiting"      # 예약은 요청 즉시 매칭하지 않는다
    env.svc.run_matching(batch=env.svc.auto_match, trigger="periodic")
    g = state(a)["active"]["group"]
    assert g and g["status"] == "proposed"
    assert g["expires_at"] > "2026-11-05T22:00"                      # 예약 매칭은 응답 시간이 길다


def confirmed_reserved_group(env, start="2026-11-06T09:00", end="2026-11-06T09:30", names=("가람", "누리")):
    a, b = env.student(names[0]), env.student(names[1])
    win = {"window_start": start, "window_end": end}
    ra = ask(a, GATE, STATION, type="reserved", **win)
    rb = ask(b, GATE_2, TERMINAL, type="reserved", **win)
    env.svc.run_matching()
    gid = state(a)["active"]["group"]["id"]
    a.post(f"/api/groups/{gid}/accept", json={"request_id": ra["id"]})
    b.post(f"/api/groups/{gid}/accept", json={"request_id": rb["id"]})
    return a, b, ra, rb, gid


def test_reserved_group_is_offered_to_driver_right_away(env):
    d = env.driver()
    a, b, ra, rb, gid = confirmed_reserved_group(env)       # 출발은 내일 아침이지만
    offer = d.get("/api/driver/state").json()["offer"]
    assert offer and offer["id"] == gid                      # 확정되자마자 예약 배차로 제안된다
    assert "예약 배차" in offer["offer_reason"]
    g = d.post(f"/api/driver/offers/{gid}/accept").json()
    assert g["status"] == "confirmed" and g["reservation_assigned"]   # 운행은 아직 시작하지 않는다
    sa = state(a)["active"]["group"]
    assert sa["driver"]["car_number"] and sa["reservation_assigned"]
    st = d.get("/api/driver/state").json()
    assert [r["id"] for r in st["reservations"]] == [gid] and st["trip"] is None

    # 예약을 받아 둔 기사님도 지금 출발하는 빠른 그룹을 받을 수 있다
    c, e = env.student("하늘"), env.student("바다")
    ask(c, GATE, STATION)
    ask(e, GATE_2, TERMINAL)
    qg = state(c)["active"]["group"]["id"]
    rc, re_ = state(c)["active"]["request"]["id"], state(e)["active"]["request"]["id"]
    c.post(f"/api/groups/{qg}/accept", json={"request_id": rc})
    e.post(f"/api/groups/{qg}/accept", json={"request_id": re_})
    assert d.get("/api/driver/state").json()["offer"]["id"] == qg

    # 운행 중에는 예약 운행을 시작할 수 없고, 끝나면 시작할 수 있다
    d.post(f"/api/driver/offers/{qg}/accept")
    assert d.post(f"/api/driver/groups/{gid}/start").status_code == 409
    for c_, r_ in ((c, rc), (e, re_)):
        c_.post(f"/api/requests/{r_}/notice-ack")
    g = env.svc.get_group_row(qg)
    for _ in g["stops"]:
        d.post(f"/api/driver/groups/{qg}/advance")
    d.post(f"/api/driver/groups/{qg}/complete", json={"final_fare": 8000})
    g = d.post(f"/api/driver/groups/{gid}/start").json()
    assert g["status"] == "riding" and g["progress"] == -1


def test_overlapping_reservation_not_offered_to_same_driver(env):
    d = env.driver()
    *_, g1 = confirmed_reserved_group(env)
    d.post(f"/api/driver/offers/{g1}/accept")
    *_, g2 = confirmed_reserved_group(env, "2026-11-06T09:20", "2026-11-06T09:50", names=("하늘", "바다"))
    assert d.get("/api/driver/state").json()["offer"] is None   # 1시간 안에 겹치는 예약은 보내지 않는다
    *_, g3 = confirmed_reserved_group(env, "2026-11-06T12:00", "2026-11-06T12:30", names=("이슬", "솔"))
    assert d.get("/api/driver/state").json()["offer"]["id"] == g3


def test_unanswered_offers_are_retried_after_all_drivers_declined(env):
    from datetime import timedelta
    d = env.driver()
    a, b = env.student("가람"), env.student("누리")
    ra, rb = ask(a, GATE, STATION), ask(b, GATE_2, TERMINAL)
    gid = state(a)["active"]["group"]["id"]
    a.post(f"/api/groups/{gid}/accept", json={"request_id": ra["id"]})
    b.post(f"/api/groups/{gid}/accept", json={"request_id": rb["id"]})
    assert d.get("/api/driver/state").json()["offer"]["id"] == gid
    env.clock.t += timedelta(seconds=31)                         # 응답 없이 시간 초과
    assert d.get("/api/driver/state").json()["offer"] is None     # 바로는 같은 기사님에게 다시 안 보냄
    env.clock.t += timedelta(minutes=3)
    assert d.get("/api/driver/state").json()["offer"]["id"] == gid   # 다른 기사님이 없으면 3분 뒤 다시 제안


def test_confirmed_group_without_driver_is_cancelled_and_refunded(env):
    a, b = env.student("가람"), env.student("누리")
    ra, rb = ask(a, GATE, STATION), ask(b, GATE_2, TERMINAL)
    gid = state(a)["active"]["group"]["id"]
    a.post(f"/api/groups/{gid}/accept", json={"request_id": ra["id"]})
    b.post(f"/api/groups/{gid}/accept", json={"request_id": rb["id"]})
    assert a.get("/api/me/wallet").json()["held"] > 0
    env.clock.t = datetime(2026, 11, 5, 22, 6)                   # 시간대 끝(21:55) + 10분 지남, 기사님 없음
    res = env.svc.run_matching(batch=False)
    assert res["no_driver"] == [gid]
    assert env.svc.get_group_row(gid)["status"] == "cancelled"
    w = a.get("/api/me/wallet").json()
    assert w["held"] == 0 and w["points"] == 500000
    assert state(a)["history"][0]["request"]["status"] == "expired"


def test_auto_match_switch_off_means_manual_only():
    env = Env()
    env.admin().post("/api/admin/settings", json={"auto_match": False})
    a, b = env.student("별"), env.student("솔")
    ask(a, GATE, STATION)
    ask(b, GATE_2, TERMINAL)
    assert state(a)["active"]["request"]["status"] == "waiting"
    assert env.svc.run_matching(batch=env.svc.auto_match)["created"] == []
    res = env.admin().post("/api/admin/match", json={"use_ai": True}).json()
    assert len(res["created"]) == 1


def test_demo_companions_auto_accept(env):
    a = env.student("하늘")
    ask(a, GATE, STATION)
    assert a.post("/api/demo/seed-near").json()["created"] == 2
    g = state(a)["active"]["group"]
    assert g is not None
    # 데모 동승자는 자동 수락·안내 확인
    others = [m for m in g["members"] if not m["is_me"]]
    assert all(m["accepted"] and m["notice_ack"] for m in others)


def test_admin_overview_and_metrics(env):
    a, b = env.student("별"), env.student("솔")
    ask(a, GATE, STATION)
    ask(b, GATE_2, TERMINAL)
    ov = env.admin().get("/api/admin/overview").json()
    assert ov["counts"]["proposed"] == 2
    assert ov["metrics"]["requests_24h"] == 2 and ov["metrics"]["quick_24h"] == 2
    assert ov["ai_decisions"] and ov["ai_decisions"][0]["method"] == "cp-sat"
    assert ov["auto_match"] is True


def test_health_and_pages(env):
    c = env.client()
    assert c.get("/api/health").json()["ok"]
    for path in ("/", "/passenger", "/driver", "/admin", "/sw.js"):
        assert c.get(path).status_code == 200, path
    cfg = c.get("/api/config").json()
    assert cfg["app_name"] == "모아타" and cfg["params"]["same_gender_only"] is True
    assert [t["kind"] for t in cfg["report_tabs"]] == ["emergency", "driving", "fare", "companion"]


def test_admin_demo_seed_random_places_in_samcheok(env):
    """데모 요청은 삼척시 안에서 무작위 출발·도착으로 만들고, 2~3명씩 같은 방향 무리로 묶는다."""
    from app.service import SAMCHEOK_PLACES, _split_counts
    import random

    for n in range(1, 9):                       # 1명만 남는 무리는 없다(전체가 1명일 때만)
        for s in range(20):
            sizes = _split_counts(n, random.Random(s))
            assert sum(sizes) == n and all(2 <= k <= 3 for k in sizes) or sizes == [1]
    origins = set()
    for seed in range(6):
        reqs = env.svc.seed_demo(count=6, gender="F", seed=seed)
        assert len(reqs) == 6
        for r in reqs:
            for p in (r["origin"], r["dest"]):
                assert 37.30 < p["lat"] < 37.49 and 129.10 < p["lng"] < 129.30   # 삼척 시내~근덕(동해시 제외)
            origins.add(r["origin"]["name"])
    assert len(origins) >= 4                    # 정문 한 곳에서만 출발하지 않는다
    assert all(name.endswith("근처") for name in origins)
    assert len(SAMCHEOK_PLACES) >= 10
    r = env.svc.seed_demo(count=4, gender="M", seed=1, type="reserved")
    assert all(x["type"] == "reserved" for x in r)


def test_static_files_are_revalidated(env):
    """고친 화면 코드가 일반 새로고침으로 반영되도록 JS·CSS는 매번 바뀌었는지 확인하게 한다."""
    c = env.admin()
    for path in ("/static/common.js", "/static/admin.js", "/static/moata.css"):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert "askConfirm" in c.get("/static/common.js").text        # 브라우저 confirm() 대신 화면 안 확인 창


def test_admin_overview_driver_has_trip_id(env):
    """운영 지도에서 기사님 아이콘을 누르면 그 기사님의 운행을 열 수 있도록 운행 번호를 같이 준다."""
    from tests.test_trust import accept_all, confirmed_trio
    users, reqs, gid = confirmed_trio(env)
    accept_all(users, reqs, gid)
    d = env.driver()
    d.post(f"/api/driver/offers/{gid}/accept")
    drivers = env.admin().get("/api/admin/overview").json()["drivers"]
    me = next(x for x in drivers if x["state"] == "운행 중")
    assert me["trip_id"] == gid and me["name"]
    assert all(x["trip_id"] is None for x in drivers if x is not me)
