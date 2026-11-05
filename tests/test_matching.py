from datetime import datetime

from app.config import Settings
from app.geo import Point
from app.matching import Req, best_order, enumerate_orders, pair_reject_reason, run_batch

CFG = Settings()
KNU = Point(37.8690, 127.7440, "강원대 정문")
T0 = datetime(2026, 11, 5, 22, 0)
T1 = datetime(2026, 11, 5, 22, 30)


def req(i, dest, *, gender="F", origin=KNU, start=T0, end=T1, seats=1, user=None):
    return Req(i, user or i, gender, origin, dest, start, end, seats, 0.3)


def test_three_riders_order_count():
    # 탑승<하차만 지키면 6!/2³ = 90가지, 그중 중간에 차가 비는 30가지를 빼면 60가지
    reqs = [req(i, Point(37.85, 127.74)) for i in (1, 2, 3)]
    assert len(list(enumerate_orders(reqs))) == 60


def test_no_order_leaves_car_empty_midway():
    reqs = [req(i, Point(37.85, 127.74)) for i in (1, 2)]
    for order in enumerate_orders(reqs):
        onboard = 0
        for k, st in enumerate(order):
            onboard += 1 if st.kind == "pickup" else -1
            if k < len(order) - 1:
                assert onboard > 0


def test_same_direction_riders_are_grouped_with_nearest_dropped_first():
    near = Point(37.8545, 127.7450, "석사동")
    far = Point(37.8380, 127.7460, "더 남쪽")
    groups = run_batch([req(1, far), req(2, near)], CFG)
    assert len(groups) == 1
    g = groups[0]
    drops = [st.request_id for st in g.stops if st.kind == "dropoff"]
    assert drops == [2, 1]
    assert g.saving_m > 0


def test_opposite_directions_are_not_grouped():
    north = Point(37.8946, 127.7230)
    south = Point(37.8380, 127.7460)
    assert pair_reject_reason(req(1, north), req(2, south), CFG) == "진행 방향이 다르다"
    assert run_batch([req(1, north), req(2, south)], CFG) == []


def test_gender_and_time_rules():
    dest = Point(37.85, 127.745)
    assert pair_reject_reason(req(1, dest, gender="F"), req(2, dest, gender="M"), CFG) is not None
    late = req(2, dest, start=datetime(2026, 11, 5, 23, 0), end=datetime(2026, 11, 5, 23, 30))
    assert pair_reject_reason(req(1, dest), late, CFG) == "희망 시간대가 겹치지 않음"


def test_capacity_limits_group_size():
    dest = Point(37.8545, 127.7450)
    reqs = [req(i, dest) for i in range(1, 6)]
    groups = run_batch(reqs, CFG)
    assert all(g.seats <= CFG.capacity for g in groups)
    assert sum(len(g.members) for g in groups) == 5  # 3명 + 2명


def test_detour_limit_blocks_long_detours():
    # 같은 방향이지만 한 명의 목적지가 크게 옆으로 빠져 있으면 우회율 0.3을 넘는다
    a = req(1, Point(37.8545, 127.7450))
    b = req(2, Point(37.8550, 127.7700))
    a.detour_ratio = b.detour_ratio = 0.05
    for r in (a, b):
        r.solo_m = 0
    for r in (a, b):
        from app.routing import estimate_distance_m
        r.solo_m = estimate_distance_m(r.origin, r.dest)
    assert best_order([a, b]) is None
