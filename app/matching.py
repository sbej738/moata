"""시간대·방향 기반 동승 매칭 (기획서 4장).

1. 후보 필터(값싼 것부터): 정원·성별 → 시간대 겹침 → 출발지 근접 → 방향 유사도
2. 경유 순서: 탑승이 하차보다 먼저, 중간에 차가 비지 않는 순서를 전수 탐색해
   총 거리가 가장 짧고 모두의 우회율 제약을 지키는 순서를 고른다.
   (3명이면 탑승<하차 조건만으로 6!/2³ = 90가지, 차가 비는 순서를 빼면 60가지)
3. 그룹 선택: 대기 요청을 희망 시작 시각 순으로 보며, 기존 그룹에 넣었을 때
   절감 거리(단독 거리 합 − 그룹 경로 거리) 증가가 가장 큰 곳에 넣는다(탐욕적 삽입).

거리는 routing.estimate_distance_m()(직선거리 × 보정 계수)으로 계산한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .config import Settings, settings
from .fare import Stop
from .geo import Point, direction_cos, haversine_m
from .routing import estimate_distance_m

DistFn = Callable[[Point, Point], float]


@dataclass
class Req:
    id: int
    user_id: int
    gender: str
    origin: Point
    dest: Point
    window_start: datetime
    window_end: datetime
    seats: int = 1
    detour_ratio: float = 0.3
    excluded: set[int] = field(default_factory=set)
    solo_m: float = 0.0


@dataclass
class MatchGroup:
    members: list[Req]
    stops: list[Stop]
    legs_m: list[float]
    total_m: float

    @property
    def solo_sum_m(self) -> float:
        return sum(r.solo_m for r in self.members)

    @property
    def saving_m(self) -> float:
        return self.solo_sum_m - self.total_m

    @property
    def seats(self) -> int:
        return sum(r.seats for r in self.members)

    @property
    def departure(self) -> datetime:
        return max(r.window_start for r in self.members)

    def stop_point(self, st: Stop) -> Point:
        req = next(r for r in self.members if r.id == st.request_id)
        return req.origin if st.kind == "pickup" else req.dest


# ---------------------------------------------------------------------------
# 1. 후보 필터
# ---------------------------------------------------------------------------
def pair_reject_reason(a: Req, b: Req, cfg: Settings = settings) -> str | None:
    """두 요청이 함께 탈 수 없는 이유. 탈 수 있으면 None."""
    if a.user_id == b.user_id:
        return "같은 사용자"
    if a.id in b.excluded or b.id in a.excluded:
        return "이전에 거절한 상대"
    if a.seats + b.seats > cfg.capacity:
        return "정원 초과"
    if a.gender != b.gender:  # 같은 성별끼리만: 설정으로 끌 수 없다
        return "성별 조건(동성끼리만 동승)"
    overlap = min(a.window_end, b.window_end) - max(a.window_start, b.window_start)
    if overlap < timedelta(minutes=cfg.min_overlap_min):
        return "희망 시간대가 겹치지 않음"
    if haversine_m(a.origin, b.origin) > cfg.origin_radius_m:
        return "출발지가 멀다"
    if direction_cos(a.origin, a.dest, b.origin, b.dest) < cfg.min_direction_cos:
        return "진행 방향이 다르다"
    return None


def group_time_ok(members: list[Req], cfg: Settings = settings) -> bool:
    start = max(r.window_start for r in members)
    end = min(r.window_end for r in members)
    return end - start >= timedelta(minutes=cfg.min_overlap_min)


# ---------------------------------------------------------------------------
# 2. 경유 순서
# ---------------------------------------------------------------------------
def enumerate_orders(reqs: list[Req]):
    """탑승<하차, 중간에 차가 비지 않는 모든 정차 순서를 만든다."""
    ids = [r.id for r in reqs]
    n = len(ids)

    def rec(order: list[Stop], picked: set[int], dropped: set[int]):
        if len(dropped) == n:
            yield list(order)
            return
        onboard = picked - dropped
        for rid in ids:
            if rid not in picked:
                order.append(Stop(rid, "pickup"))
                picked.add(rid)
                yield from rec(order, picked, dropped)
                picked.discard(rid)
                order.pop()
        for rid in ids:
            if rid in onboard:
                # 마지막 탑승자가 내려 차가 비는 것은 모두 탄 뒤에만 허용
                if len(onboard) == 1 and len(picked) < n:
                    continue
                order.append(Stop(rid, "dropoff"))
                dropped.add(rid)
                yield from rec(order, picked, dropped)
                dropped.discard(rid)
                order.pop()

    yield from rec([], set(), set())


def best_order(reqs: list[Req], dist: DistFn | None = None, cfg: Settings = settings):
    """총 거리가 가장 짧고 모두의 우회율 제약을 지키는 정차 순서. 없으면 None."""
    dist = dist or (lambda a, b: estimate_distance_m(a, b, cfg))
    by_id = {r.id: r for r in reqs}
    cache: dict[tuple, float] = {}

    def d(a: Point, b: Point) -> float:
        k = (a.key(), b.key())
        if k not in cache:
            cache[k] = dist(a, b)
        return cache[k]

    def point(st: Stop) -> Point:
        r = by_id[st.request_id]
        return r.origin if st.kind == "pickup" else r.dest

    best = None
    for order in enumerate_orders(reqs):
        pts = [point(st) for st in order]
        legs = [d(a, b) for a, b in zip(pts, pts[1:])]
        total = sum(legs)
        if best is not None and total >= best[2]:
            continue
        # 우회율 제약: 각자의 탑승 거리 ≤ (1 + α) × 단독 거리
        ride = {rid: 0.0 for rid in by_id}
        onboard: set[int] = set()
        for k, st in enumerate(order[:-1]):
            if st.kind == "pickup":
                onboard.add(st.request_id)
            else:
                onboard.discard(st.request_id)
            for rid in onboard:
                ride[rid] += legs[k]
        if all(ride[rid] <= (1 + by_id[rid].detour_ratio) * by_id[rid].solo_m + 1.0 for rid in by_id):
            best = (order, legs, total)
    return best


# ---------------------------------------------------------------------------
# 3. 배치 매칭(탐욕적 삽입)
# ---------------------------------------------------------------------------
def run_batch(reqs: list[Req], cfg: Settings = settings, dist: DistFn | None = None) -> list[MatchGroup]:
    dist = dist or (lambda a, b: estimate_distance_m(a, b, cfg))
    for r in reqs:
        if not r.solo_m:
            r.solo_m = dist(r.origin, r.dest)

    groups: list[MatchGroup] = []
    for r in sorted(reqs, key=lambda x: (x.window_start, x.id)):
        best_group, best_gain, best_plan = None, 0.0, None
        for g in groups:
            if g.seats + r.seats > cfg.capacity:
                continue
            if any(pair_reject_reason(m, r, cfg) for m in g.members):
                continue
            members = g.members + [r]
            if not group_time_ok(members, cfg):
                continue
            plan = best_order(members, dist, cfg)
            if plan is None:
                continue
            gain = (g.solo_sum_m + r.solo_m - plan[2]) - g.saving_m
            if gain > best_gain:
                best_group, best_gain, best_plan = g, gain, plan
        if best_group is not None:
            best_group.members.append(r)
            best_group.stops, best_group.legs_m, best_group.total_m = best_plan
        else:
            groups.append(MatchGroup([r], [Stop(r.id, "pickup"), Stop(r.id, "dropoff")], [r.solo_m], r.solo_m))
    return [g for g in groups if len(g.members) >= 2]


def group_reject_reason(members: list[Req], cfg: Settings = settings, dist: DistFn | None = None) -> str | None:
    """그룹 전체가 규칙을 지키는지 다시 확인한다(AI가 고른 그룹의 최종 검증용). 지키면 None."""
    if len(members) < 2:
        return "동승자가 2명 이상이어야 합니다"
    if len({r.id for r in members}) != len(members):
        return "같은 요청이 두 번 들어 있다"
    if sum(r.seats for r in members) > cfg.capacity:
        return "정원 초과"
    if len({r.gender for r in members}) != 1:
        return "성별 조건(동성끼리만 동승)"
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            reason = pair_reject_reason(a, b, cfg)
            if reason:
                return reason
    if not group_time_ok(members, cfg):
        return "희망 시간대가 함께 겹치지 않음"
    dist = dist or (lambda x, y: estimate_distance_m(x, y, cfg))
    for r in members:
        if not r.solo_m:
            r.solo_m = dist(r.origin, r.dest)
    if best_order(members, dist, cfg) is None:
        return "우회율 제한을 넘는다"
    return None


def explain_pair(a: Req, b: Req, cfg: Settings = settings) -> str:
    """디버깅용: 두 요청이 왜 묶이지 않는지 설명."""
    reason = pair_reject_reason(a, b, cfg)
    if reason:
        return reason
    for r in (a, b):
        if not r.solo_m:
            r.solo_m = estimate_distance_m(r.origin, r.dest, cfg)
    if best_order([a, b], cfg=cfg) is None:
        return "우회율 제한을 넘는다"
    return "매칭 가능"
