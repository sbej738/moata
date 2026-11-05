"""신고하기: 탭·카테고리 검증, 서버가 채우는 첨부 정보, 동승자 대상, 중복 방지, 긴급 신고 경보."""
from tests.conftest import GATE, GATE_2, STATION, TERMINAL, ask, state


def riding_group(env):
    a, b = env.student("가람"), env.student("누리")
    ra, rb = ask(a, GATE, STATION), ask(b, GATE_2, TERMINAL)
    gid = state(a)["active"]["group"]["id"]
    a.post(f"/api/groups/{gid}/accept", json={"request_id": ra["id"]})
    b.post(f"/api/groups/{gid}/accept", json={"request_id": rb["id"]})
    d = env.driver()
    d.post(f"/api/driver/offers/{gid}/accept")
    for c, r in ((a, ra), (b, rb)):
        c.post(f"/api/requests/{r['id']}/notice-ack")
    d.post(f"/api/driver/groups/{gid}/advance")
    return a, b, d, gid, ra, rb


def test_invalid_tab_or_category_is_422(env):
    a, b, d, gid, ra, rb = riding_group(env)
    assert a.post("/api/reports", json={"group_id": gid, "kind": "emergency", "category": "late"}).status_code == 422
    assert a.post("/api/reports", json={"group_id": gid, "kind": "weird", "category": "x"}).status_code == 422


def test_snapshot_is_filled_by_server(env):
    a, b, d, gid, ra, rb = riding_group(env)
    r = a.post("/api/reports", json={"group_id": gid, "kind": "driving", "category": "reckless",
                                     "note": "너무 빨라요", "car_number": "가짜 번호"}).json()
    snap = r["snapshot"]
    assert snap["car_number"] == env.svc.get_driver(d.user["id"])["car_number"]
    assert snap["route"] and snap["group_id"] == gid
    assert r["location_source"] == "last_stop" and r["lat"] is not None   # GPS가 없으면 마지막 정차 지점
    assert r["category_label"] == "난폭·과속 운전" and r["request_id"] == ra["id"]


def test_companion_target_must_be_in_same_group(env):
    a, b, d, gid, ra, rb = riding_group(env)
    bad = a.post("/api/reports", json={"group_id": gid, "kind": "companion", "category": "harassment",
                                       "target_request_id": ra["id"]})
    assert bad.status_code == 422                                    # 자기 자신은 대상이 아님
    ok = a.post("/api/reports", json={"group_id": gid, "kind": "companion", "category": "harassment"}).json()
    assert ok["target_request_id"] == rb["id"]                       # 동승자가 한 명이면 자동으로 정한다
    outsider = env.student("다른")
    assert outsider.post("/api/reports", json={"group_id": gid, "kind": "fare", "category": "toll"}).status_code == 403


def test_duplicate_within_a_minute_returns_existing(env):
    a, b, d, gid, ra, rb = riding_group(env)
    body = {"group_id": gid, "kind": "fare", "category": "fare_mismatch"}
    first = a.post("/api/reports", json=body).json()
    again = a.post("/api/reports", json=body).json()
    assert again["duplicate"] and again["id"] == first["id"]
    e1 = a.post("/api/reports", json={"group_id": gid, "kind": "emergency", "category": "sick"}).json()
    e2 = a.post("/api/reports", json={"group_id": gid, "kind": "emergency", "category": "sick"}).json()
    assert e1["id"] != e2["id"]                                      # 긴급·안전은 매번 접수


def test_emergency_goes_to_top_with_high_alert(env):
    a, b, d, gid, ra, rb = riding_group(env)
    a.post("/api/reports", json={"group_id": gid, "kind": "driving", "category": "vehicle"})
    a.post("/api/reports", json={"group_id": gid, "kind": "emergency", "category": "violence",
                                 "lat": 37.44, "lng": 129.17})
    ov = env.admin().get("/api/admin/overview").json()
    assert ov["reports"][0]["kind"] == "emergency" and ov["reports"][0]["location_source"] == "gps"
    assert ov["alerts"][0]["kind"] == "emergency_report" and ov["alerts"][0]["severity"] == "high"
    rid = ov["reports"][0]["id"]
    assert env.admin().post(f"/api/admin/reports/{rid}", json={"status": "resolved"}).json()["status"] == "resolved"


def test_driver_can_report_own_trip(env):
    a, b, d, gid, ra, rb = riding_group(env)
    r = d.post("/api/reports", json={"group_id": gid, "kind": "emergency", "category": "accident"})
    assert r.status_code == 200
