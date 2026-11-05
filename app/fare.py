"""거리 비례 요금 분배 (기획서 5장).

추천 방식인 '구간 분할':
    경로를 정차 지점마다 구간으로 자르고, 구간 k의 요금 c_k = F × ℓ_k / L 을
    그 구간에 타고 있던 사람들이 좌석 수 비율로 나눈다.
        s_i = Σ_{k ∈ K_i} c_k × seats_i / Σ_{j ∈ onboard_k} seats_j

보정 규칙:
    - 상한 보장: 각자 몫 ≤ 단독 예상 요금 × (실제요금/예상요금) × (1 − β).
      넘는 금액은 상한에 걸리지 않은 사람에게 몫 비례로 넘긴다(물 채우기 방식).
    - 통행료: 통행료가 생긴 구간의 탑승자끼리만 나눈다.
    - 반올림: 각자 rounding_unit 원 단위로 내림하고, 남는 차액은 대표 결제자에게 붙인다.

비교용으로 '탑승 거리 비례'(①)와 '단독 직행 거리 비례'(③)도 함께 계산한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Stop:
    request_id: int
    kind: str  # "pickup" | "dropoff"


@dataclass
class Segment:
    index: int
    from_m: float
    to_m: float
    cost: float
    toll: float
    riders: list[int]
    seats_total: int
    shares: dict[int, float]

    @property
    def length_m(self) -> float:
        return self.to_m - self.from_m

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "from_m": round(self.from_m),
            "to_m": round(self.to_m),
            "length_m": round(self.length_m),
            "cost": round(self.cost),
            "toll": round(self.toll),
            "riders": self.riders,
            "seats_total": self.seats_total,
            "shares": {str(k): round(v) for k, v in self.shares.items()},
        }


@dataclass
class SplitResult:
    total_fare: int
    total_toll: int
    segments: list[Segment]
    raw: dict[int, float]          # 구간 분할 결과(보정 전, 요금 부분만)
    capped: dict[int, float]       # 상한 보정 후(요금 부분만)
    tolls: dict[int, float]        # 통행료 몫
    final: dict[int, int]          # 반올림 후 최종 금액(요금 + 통행료)
    caps: dict[int, float] = field(default_factory=dict)
    payer: int | None = None
    notes: list[str] = field(default_factory=list)
    compare: dict[str, dict[int, int]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        def s(d):
            return {str(k): round(v) for k, v in d.items()}

        return {
            "total_fare": self.total_fare,
            "total_toll": self.total_toll,
            "segments": [seg.to_dict() for seg in self.segments],
            "raw": s(self.raw),
            "capped": s(self.capped),
            "tolls": s(self.tolls),
            "final": {str(k): v for k, v in self.final.items()},
            "caps": s(self.caps),
            "payer": self.payer,
            "notes": self.notes,
            "compare": {name: {str(k): v for k, v in d.items()} for name, d in self.compare.items()},
        }


def onboard_after_each_stop(stops: list[Stop]) -> list[list[int]]:
    """i번째 정차 직후 차 안에 있는 요청 id 목록(= i번째 구간의 탑승자)."""
    onboard: list[int] = []
    states: list[list[int]] = []
    for st in stops:
        if st.kind == "pickup":
            if st.request_id not in onboard:
                onboard.append(st.request_id)
        else:
            if st.request_id in onboard:
                onboard.remove(st.request_id)
        states.append(list(onboard))
    return states


def ride_distance_m(stops: list[Stop], legs_m: list[float]) -> dict[int, float]:
    """각 요청이 실제로 차 안에 있었던 거리."""
    states = onboard_after_each_stop(stops)
    dist: dict[int, float] = {st.request_id: 0.0 for st in stops}
    for k, length in enumerate(legs_m):
        for rid in states[k]:
            dist[rid] += length
    return dist


def split_fare(
    stops: list[Stop],
    legs_m: list[float],
    seats: dict[int, int],
    total_fare: int,
    *,
    leg_tolls: list[int] | None = None,
    solo_fares: dict[int, int] | None = None,
    solo_distances: dict[int, float] | None = None,
    est_group_fare: int | None = None,
    min_discount: float = 0.10,
    rounding_unit: int = 10,
) -> SplitResult:
    if len(legs_m) != len(stops) - 1:
        raise ValueError("legs_m 길이는 정차 지점 수 - 1 이어야 합니다")
    members = list(dict.fromkeys(st.request_id for st in stops))
    leg_tolls = leg_tolls or [0] * len(legs_m)
    notes: list[str] = []

    total_len = float(sum(legs_m))
    states = onboard_after_each_stop(stops)
    raw = {rid: 0.0 for rid in members}
    tolls = {rid: 0.0 for rid in members}
    segments: list[Segment] = []
    pos = 0.0

    def split_by_seats(amount: float, riders: list[int]) -> dict[int, float]:
        total_seats = sum(seats.get(r, 1) for r in riders)
        return {r: amount * seats.get(r, 1) / total_seats for r in riders}

    for k, length in enumerate(legs_m):
        riders = states[k]
        cost = total_fare * length / total_len if total_len > 0 else 0.0
        toll = float(leg_tolls[k])
        if (cost > 0 or toll > 0) and not riders:
            notes.append(f"{k + 1}번 구간에 탑승자가 없어 전원에게 나눴습니다")
            riders = members
        shares = split_by_seats(cost, riders) if riders else {}
        for r, v in shares.items():
            raw[r] += v
        if toll:
            for r, v in split_by_seats(toll, riders).items():
                tolls[r] += v
        if length > 0 or toll > 0:
            segments.append(Segment(len(segments), pos, pos + length, cost, toll, list(riders),
                                    sum(seats.get(r, 1) for r in riders), shares))
        pos += length

    if total_len <= 0:  # 전원이 같은 자리에서 내린 특수한 경우: 좌석 수로 균등 분할
        raw = split_by_seats(float(total_fare), members)
        notes.append("주행거리가 0이라 좌석 수로 똑같이 나눴습니다")

    # --- 상한 보정 ------------------------------------------------------
    capped = dict(raw)
    caps: dict[int, float] = {}
    if solo_fares and all(r in solo_fares for r in members):
        scale = (total_fare / est_group_fare) if est_group_fare else 1.0
        caps = {r: solo_fares[r] * scale * (1 - min_discount) for r in members}
        capped = _water_fill(raw, caps, float(total_fare), notes)

    # --- 반올림 ---------------------------------------------------------
    payer = stops[0].request_id if stops else None
    final: dict[int, int] = {}
    for r in members:
        amount = capped[r] + tolls[r]
        final[r] = int(amount // rounding_unit * rounding_unit)
    remainder = total_fare + int(round(sum(leg_tolls))) - sum(final.values())
    if payer is not None and remainder:
        final[payer] += remainder
        notes.append(f"반올림 차액 {remainder}원은 대표 결제자에게 더했습니다")

    # --- 비교용 다른 방식 -------------------------------------------------
    compare: dict[str, dict[int, int]] = {}
    ride = ride_distance_m(stops, legs_m)
    compare["ride_distance"] = _proportional(total_fare, {r: ride[r] * seats.get(r, 1) for r in members})
    if solo_distances and all(r in solo_distances for r in members):
        compare["solo_distance"] = _proportional(total_fare, {r: solo_distances[r] * seats.get(r, 1) for r in members})
    compare["segment"] = {r: int(round(raw[r])) for r in members}

    return SplitResult(
        total_fare=total_fare,
        total_toll=int(round(sum(leg_tolls))),
        segments=segments,
        raw=raw,
        capped=capped,
        tolls=tolls,
        final=final,
        caps=caps,
        payer=payer,
        notes=notes,
        compare=compare,
    )


def _water_fill(shares: dict[int, float], caps: dict[int, float], total: float, notes: list[str]) -> dict[int, float]:
    """상한을 넘는 몫을 잘라 상한에 안 걸린 사람들에게 몫 비례로 나눈다."""
    result = dict(shares)
    fixed: set[int] = set()
    while True:
        over = [r for r in result if r not in fixed and result[r] > caps[r] + 1e-6]
        if not over:
            break
        excess = 0.0
        for r in over:
            cut = result[r] - caps[r]
            excess += cut
            result[r] = caps[r]
            fixed.add(r)
            notes.append(f"요청 {r}: 단독 대비 상한에 걸려 {round(cut)}원을 다른 동승자에게 넘겼습니다")
        free = [r for r in result if r not in fixed]
        if not free:
            cap_sum = sum(caps.values())
            notes.append("전원이 상한에 걸려 상한 비율로 나눴습니다")
            return {r: total * caps[r] / cap_sum for r in result}
        base = sum(result[r] for r in free)
        for r in free:
            result[r] += excess * (result[r] / base if base > 0 else 1 / len(free))
    return result


def _proportional(total: int, weights: dict[int, float]) -> dict[int, int]:
    w = sum(weights.values())
    if w <= 0:
        n = len(weights)
        return {r: int(round(total / n)) for r in weights}
    return {r: int(round(total * v / w)) for r, v in weights.items()}


# ---------------------------------------------------------------------------
# 신뢰도 추가 부담: 신뢰도가 기준점 미만인 사람이 몫의 일정 비율을 더 내고, 그만큼 동승자 몫을 줄인다
# ---------------------------------------------------------------------------
def apply_trust_surcharge(final: dict[int, int], rates: dict[int, float], solo_fares: dict[int, int],
                          min_discount: float = 0.05, unit: int = 10) -> tuple[dict[int, int], dict[int, int], list[str]]:
    """돌려주는 값: (바뀐 금액, 사람별 변화량(+더 냄/−덜 냄), 안내 문구).

    - 더 내는 금액 = 몫 × 비율(unit 단위 내림). 단, 혼자 탈 때 요금 × (1 − min_discount)를 넘지 않는다
      → 신뢰도가 0점이어도 혼자 타는 것보다는 항상 싸다.
    - 더 낸 금액은 나머지 사람에게 몫 비율로 나눠 깎아 준다. 합계는 바뀌지 않는다.
    - 모두가 기준점 미만이거나 아무도 아니면 그대로 둔다.
    """
    out = dict(final)
    delta = {k: 0 for k in final}
    low = [k for k in final if rates.get(k, 0) > 0]
    others = [k for k in final if rates.get(k, 0) <= 0]
    if not low or not others:
        return out, delta, []
    total = 0
    for k in low:
        cap = int(solo_fares.get(k, 0) * (1 - min_discount)) // unit * unit
        extra = int(final[k] * rates[k]) // unit * unit
        extra = max(0, min(extra, cap - final[k]))
        out[k] += extra
        delta[k] += extra
        total += extra
    if total <= 0:
        return final.copy(), {k: 0 for k in final}, []
    base = sum(final[k] for k in others) or 1
    given = 0
    for k in others:
        cut = min(final[k], int(total * final[k] / base) // unit * unit)
        out[k] -= cut
        delta[k] -= cut
        given += cut
    rest = total - given
    for k in sorted(others, key=lambda x: -out[x]):   # 남는 금액은 몫이 큰 사람부터
        if rest <= 0:
            break
        cut = min(rest, out[k])
        out[k] -= cut
        delta[k] -= cut
        rest -= cut
    notes = [f"신뢰도 회복 중인 동승자가 {total:,}원을 더 내서 나머지 동승자 요금이 그만큼 줄었어요"]
    return out, delta, notes
