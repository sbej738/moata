"""OR-Tools 최적화: 탐욕보다 같거나 좋은 결과, 한 요청은 한 그룹, 불러오기 실패 시 대체, 기사 배정."""
import random
from datetime import datetime

import app.optimize as opt
from app.config import Settings
from app.geo import Point
from app.matching import Req, group_reject_reason, run_batch
from app.optimize import DriverSlot, GroupSlot, assign_drivers, plan_groups

CFG = Settings()
GATE = Point(37.45140, 129.16172, "정문")
T0 = datetime(2026, 11, 5, 22, 0)
T1 = datetime(2026, 11, 5, 22, 30)


def req(i, dest, origin=GATE, gender="F", excluded=()):
    return Req(i, i, gender, origin, dest, T0, T1, 1, 0.4, set(excluded))


def near(lat, lng, i):
    return Point(lat + 0.0004 * (i % 3), lng + 0.0004 * (i % 2))


def test_cp_sat_beats_greedy_when_greedy_pairs_badly():
    # 정원 2명. A-B를 먼저 묶으면 C-D는 서로 거절한 사이라 둘 다 혼자 남는다.
    # 최적은 A-C, B-D 두 그룹.
    cfg = Settings(capacity=2)
    dest = Point(37.43007, 129.17874)
    a, b = req(1, near(dest.lat, dest.lng, 0)), req(2, near(dest.lat, dest.lng, 1))
    c, d = req(3, near(dest.lat, dest.lng, 2), excluded={4}), req(4, near(dest.lat, dest.lng, 3), excluded={3})
    greedy = run_batch([req(1, a.dest), req(2, b.dest), req(3, c.dest, excluded={4}), req(4, d.dest, excluded={3})], cfg)
    assert len(greedy) == 1
    plan = plan_groups([a, b, c, d], cfg)
    assert plan.method == "cp-sat"
    assert len(plan.groups) == 2
    assert plan.objective > plan.greedy_objective
    assert sorted(i for g in plan.groups for i in g.ids) == [1, 2, 3, 4]


def test_result_is_never_worse_than_greedy_and_no_double_membership():
    rng = random.Random(7)
    dests = [Point(37.44015, 129.16904), Point(37.43007, 129.17874), Point(37.44209, 129.16519)]
    for trial in range(5):
        reqs = []
        for i in range(1, 10):
            o = Point(GATE.lat + rng.uniform(-0.003, 0.003), GATE.lng + rng.uniform(-0.003, 0.003))
            dd = rng.choice(dests)
            reqs.append(Req(i, i, rng.choice("FFM"), o, Point(dd.lat + rng.uniform(-0.002, 0.002), dd.lng),
                            T0, T1, 1, 0.4))
        plan = plan_groups(reqs, CFG)
        assert plan.objective + 1e-6 >= plan.greedy_objective
        ids = [i for g in plan.groups for i in g.ids]
        assert len(ids) == len(set(ids))
        by_id = {r.id: r for r in reqs}
        for g in plan.groups:
            assert group_reject_reason([by_id[i] for i in g.ids], CFG) is None   # 모두 규칙을 지킨다


def test_missing_ortools_falls_back_to_greedy(monkeypatch):
    monkeypatch.setattr(opt, "cp_model", None)
    dest = Point(37.43007, 129.17874)
    plan = plan_groups([req(1, dest), req(2, near(dest.lat, dest.lng, 1))], CFG)
    assert plan.method == "greedy(fallback)" and len(plan.groups) == 1


def test_optimizer_switch_off_uses_greedy():
    dest = Point(37.43007, 129.17874)
    plan = plan_groups([req(1, dest), req(2, near(dest.lat, dest.lng, 1))], Settings(optimizer_enabled=False))
    assert plan.method == "greedy"


def test_mixed_genders_never_in_candidates():
    dest = Point(37.43007, 129.17874)
    plan = plan_groups([req(1, dest, gender="F"), req(2, dest, gender="M")], CFG)
    assert plan.groups == [] and plan.candidates == []


def test_assign_drivers_one_to_one_and_nearest():
    d1 = DriverSlot(10, Point(37.4400, 129.1690))   # 터미널 근처
    d2 = DriverSlot(11, Point(37.4300, 129.1787))   # 역 근처
    g1 = GroupSlot(1, Point(37.4402, 129.1691))     # 터미널 픽업
    g2 = GroupSlot(2, Point(37.4301, 129.1786))     # 역 픽업
    pairs, method = assign_drivers([d1, d2], [g1, g2])
    assert method == "cp-sat"
    got = {(a.driver_id, a.group_id) for a in pairs}
    assert got == {(10, 1), (11, 2)}
    # 거절한 기사님에게는 다시 보내지 않는다
    pairs, _ = assign_drivers([d1], [g1], banned={(10, 1)})
    assert pairs == []
