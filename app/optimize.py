"""수리 최적화: Google OR-Tools CP-SAT.

1. 그룹 묶기(집합 분할)
   - 후보: 규칙(동성, 좌석, 시간대, 출발지, 방향, 우회)을 통과한 2명·3명 조합. 정차 순서는 기존 전수 탐색(best_order).
   - 변수: 후보마다 선택 여부(0/1)
   - 제약: 한 요청은 최대 한 그룹에만
   - 목적: 전체 가중치 합 최대(가중치 = 절감 거리 + 마감 임박·여러 번 거절당한 요청 우대)
   - 시간 제한 안에 못 풀거나 OR-Tools를 못 불러오면 탐욕적 삽입(matching.run_batch)으로 대체.
     탐욕 결과가 더 좋으면(시간 제한에 걸린 경우) 탐욕 결과를 쓴다. 그래서 결과는 항상 탐욕 이상이다.
2. 기사-그룹 배정
   - 변수: (기사, 그룹) 쌍마다 0/1. 한 기사는 한 그룹, 한 그룹은 한 기사
   - 목적: 배정 수 최대 → 픽업 거리 합 최소 → 마감 임박 그룹 우선
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from itertools import combinations

from . import matching
from .config import Settings, settings
from .geo import Point, haversine_m
from .matching import DistFn, MatchGroup, Req
from .routing import estimate_distance_m

log = logging.getLogger("moata.optimize")

try:  # 설치되지 않았거나 불러오지 못해도 서비스는 탐욕 매칭으로 돈다
    from ortools.sat.python import cp_model
except Exception:  # pragma: no cover - 환경에 따라 다름
    cp_model = None

PRIORITY_WEIGHT_M = 150.0   # 우대 점수 1점 = 절감 거리 150m만큼의 가치
MAX_CANDIDATES = 6000


@dataclass
class Candidate:
    key: str
    group: MatchGroup
    weight: float

    @property
    def ids(self) -> list[int]:
        return [m.id for m in self.group.members]


@dataclass
class PlanResult:
    groups: list[Candidate]
    method: str                     # "cp-sat" | "greedy" | "greedy(fallback)"
    objective: float
    greedy_objective: float
    candidates: list[Candidate] = field(default_factory=list)
    elapsed_ms: int = 0
    note: str = ""

    @property
    def saving_m(self) -> float:
        return sum(c.group.saving_m for c in self.groups)


def _weight(group: MatchGroup, priority: dict[int, float]) -> float:
    return group.saving_m + PRIORITY_WEIGHT_M * sum(priority.get(m.id, 0.0) for m in group.members)


def generate_candidates(reqs: list[Req], cfg: Settings = settings, dist: DistFn | None = None,
                        priority: dict[int, float] | None = None) -> list[Candidate]:
    """규칙을 모두 통과하고 절감이 있는 2명·3명 후보 그룹."""
    dist = dist or (lambda a, b: estimate_distance_m(a, b, cfg))
    priority = priority or {}
    for r in reqs:
        if not r.solo_m:
            r.solo_m = dist(r.origin, r.dest)
    ordered = sorted(reqs, key=lambda r: r.id)
    ok_pair: dict[tuple[int, int], bool] = {}
    for a, b in combinations(ordered, 2):
        ok_pair[(a.id, b.id)] = matching.pair_reject_reason(a, b, cfg) is None

    def build(members: list[Req]) -> MatchGroup | None:
        if sum(r.seats for r in members) > cfg.capacity or not matching.group_time_ok(members, cfg):
            return None
        plan = matching.best_order(members, dist, cfg)
        if plan is None:
            return None
        g = MatchGroup(list(members), *plan)
        return g if g.saving_m > 0 else None

    out: list[Candidate] = []
    pairs = [(a, b) for a, b in combinations(ordered, 2) if ok_pair[(a.id, b.id)]]
    for a, b in pairs:
        g = build([a, b])
        if g:
            out.append(Candidate(f"C{len(out) + 1}", g, _weight(g, priority)))
    if cfg.capacity >= 3:
        neighbors: dict[int, set[int]] = {}
        for a, b in pairs:
            neighbors.setdefault(a.id, set()).add(b.id)
            neighbors.setdefault(b.id, set()).add(a.id)
        by_id = {r.id: r for r in ordered}
        for a, b in pairs:
            for cid in sorted(neighbors.get(a.id, set()) & neighbors.get(b.id, set())):
                if cid <= b.id or len(out) >= MAX_CANDIDATES:
                    continue
                g = build([a, b, by_id[cid]])
                if g:
                    out.append(Candidate(f"C{len(out) + 1}", g, _weight(g, priority)))
    return out


def _greedy(reqs: list[Req], cfg: Settings, dist: DistFn | None, priority: dict[int, float]) -> list[Candidate]:
    groups = matching.run_batch([_copy(r) for r in reqs], cfg, dist)
    return [Candidate(f"G{i + 1}", g, _weight(g, priority)) for i, g in enumerate(groups)]


def _copy(r: Req) -> Req:
    return Req(r.id, r.user_id, r.gender, r.origin, r.dest, r.window_start, r.window_end, r.seats,
               r.detour_ratio, set(r.excluded), r.solo_m)


def plan_groups(reqs: list[Req], cfg: Settings = settings, dist: DistFn | None = None,
                priority: dict[int, float] | None = None, use_solver: bool | None = None) -> PlanResult:
    """겹치지 않는 그룹 조합 중 전체 가중치가 가장 큰 계획."""
    started = time.perf_counter()
    priority = priority or {}
    use_solver = cfg.optimizer_enabled if use_solver is None else use_solver
    greedy = _greedy(reqs, cfg, dist, priority)
    greedy_obj = sum(c.weight for c in greedy)
    candidates = generate_candidates(reqs, cfg, dist, priority)

    def done(groups, method, note=""):
        return PlanResult(groups, method, sum(c.weight for c in groups), greedy_obj, candidates,
                          int((time.perf_counter() - started) * 1000), note)

    if not use_solver:
        return done(greedy, "greedy", "최적화가 꺼져 있어 탐욕 매칭을 썼습니다")
    if cp_model is None:
        return done(greedy, "greedy(fallback)", "OR-Tools를 불러오지 못해 탐욕 매칭으로 대체했습니다")
    if not candidates:
        return done([], "cp-sat")
    try:
        chosen = _solve_partition(candidates, cfg.optimizer_time_limit_sec)
    except Exception as exc:  # 풀이 중 오류도 서비스를 멈추지 않는다
        log.warning("CP-SAT 실패, 탐욕 매칭으로 대체: %s", exc)
        return done(greedy, "greedy(fallback)", f"최적화 오류로 탐욕 매칭으로 대체했습니다: {type(exc).__name__}")
    if chosen is None:
        return done(greedy, "greedy(fallback)", "시간 안에 해를 찾지 못해 탐욕 매칭으로 대체했습니다")
    if sum(c.weight for c in chosen) + 1e-6 < greedy_obj:
        return done(greedy, "greedy(fallback)", "시간 제한 때문에 탐욕 결과가 더 좋아 탐욕 결과를 썼습니다")
    return done(chosen, "cp-sat")


def _solve_partition(candidates: list[Candidate], time_limit: float) -> list[Candidate] | None:
    model = cp_model.CpModel()
    x = [model.NewBoolVar(c.key) for c in candidates]
    by_req: dict[int, list[int]] = {}
    for i, c in enumerate(candidates):
        for rid in c.ids:
            by_req.setdefault(rid, []).append(i)
    for idxs in by_req.values():
        if len(idxs) > 1:
            model.AddAtMostOne(x[i] for i in idxs)
    model.Maximize(sum(int(round(c.weight * 10)) * x[i] for i, c in enumerate(candidates)))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.1, float(time_limit))
    solver.parameters.num_search_workers = 4
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    return [c for i, c in enumerate(candidates) if solver.Value(x[i])]


# ---------------------------------------------------------------------------
# 기사-그룹 배정
# ---------------------------------------------------------------------------
@dataclass
class DriverSlot:
    driver_id: int
    pos: Point


@dataclass
class GroupSlot:
    group_id: int
    pickup: Point
    urgency: float = 0.0      # 0~1, 출발이 가까울수록 큼


@dataclass
class Assignment:
    driver_id: int
    group_id: int
    pickup_m: float


def assign_drivers(drivers: list[DriverSlot], groups: list[GroupSlot], banned: set[tuple[int, int]] | None = None,
                   max_pickup_m: float = 15000, time_limit: float = 1.0, use_solver: bool = True) -> tuple[list[Assignment], str]:
    banned = banned or set()
    pairs = []
    for d in drivers:
        for g in groups:
            if (d.driver_id, g.group_id) in banned:
                continue
            m = haversine_m(d.pos, g.pickup)
            if m <= max_pickup_m:
                pairs.append((d, g, m))
    if not pairs:
        return [], "none"
    if use_solver and cp_model is not None:
        try:
            model = cp_model.CpModel()
            x = [model.NewBoolVar(f"a{i}") for i in range(len(pairs))]
            for did in {d.driver_id for d, _, _ in pairs}:
                model.AddAtMostOne(x[i] for i, (d, _, _) in enumerate(pairs) if d.driver_id == did)
            for gid in {g.group_id for _, g, _ in pairs}:
                model.AddAtMostOne(x[i] for i, (_, g, _) in enumerate(pairs) if g.group_id == gid)
            big = int(max_pickup_m) + 10_000
            model.Maximize(sum((big - int(m) + int(g.urgency * 3000)) * x[i] for i, (_, g, m) in enumerate(pairs)))
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = time_limit
            status = solver.Solve(model)
            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                return [Assignment(d.driver_id, g.group_id, m) for i, (d, g, m) in enumerate(pairs) if solver.Value(x[i])], "cp-sat"
        except Exception as exc:
            log.warning("배차 최적화 실패, 가까운 순 배정으로 대체: %s", exc)
    used_d, used_g, out = set(), set(), []
    for d, g, m in sorted(pairs, key=lambda p: (p[2] - p[1].urgency * 3000)):
        if d.driver_id in used_d or g.group_id in used_g:
            continue
        used_d.add(d.driver_id)
        used_g.add(g.group_id)
        out.append(Assignment(d.driver_id, g.group_id, m))
    return out, "greedy"
