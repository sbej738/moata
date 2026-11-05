"""매칭 확률·예상 대기 시간 예측.

1단계 — 통계 기본 추정치(항상 계산):
    · 지금 대기 중인 요청 중 함께 탈 수 있는 사람 수 n
    · 과거 기록에서 '같은 시간대·같은 방향' 요청의 도착률 λ (감마-포아송 평활)
    · 과거 제안의 수락률 p (베타 평활)
    · 마감까지 남은 시간 T(분)

        λ' = λ · p                                  (수락까지 이어지는 도착률)
        P_now = 1 − (1 − p)^n                       (지금 있는 사람과 성사될 확률)
        P = 1 − (1 − P_now) · e^(−λ'T)              (마감 전 성사 확률)
        E[t | t < T] = 1/λ' − T·e^(−λ'T) / (1 − e^(−λ'T))   (새 사람을 기다릴 때 예상 시간)

2단계 — AI 보정(Gemini → Groq, 키가 있을 때):
    위 특징값과 기본 추정치를 AI에 주고, 구조화 출력(JSON)으로 확률·예상 시간·이유·제안을 받는다.
    숫자는 서버가 범위를 검증한다(확률은 기본값 ±0.25, 시간은 0~T). 모두 실패하면 기본 추정치를 쓴다.

호출 줄이기: 같은 상황(남은 시간은 5분 단위로 묶음)이면 3분 동안 다시 부르지 않고,
실패도 30초 동안 기억해 30초마다 다시 두드리지 않는다.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from . import matching
from .config import Settings, settings
from .geo import Point, direction_cos, haversine_m
from .llm import AIRouter, AllProvidersFailed, make_router, parse_order

log = logging.getLogger("moata.predict")

MAX_ADJUST = 0.25     # AI가 기본 확률에서 움직일 수 있는 최대 폭
CACHE_TTL_SEC = 180   # 같은 상황이면 3분 동안 AI를 다시 부르지 않는다
FAIL_TTL_SEC = 30     # 실패한 결과도 30초는 그대로 보여 준다
MINUTE_BUCKET = 5     # 캐시 키의 남은 시간 단위(분)


@dataclass
class Features:
    minutes_left: float            # 마감까지 남은 분
    compatible_now: int            # 지금 함께 탈 수 있는 대기자 수
    nearest_now_m: float | None    # 그중 출발지가 가장 가까운 사람까지 거리
    history_matches: int           # 과거 같은 시간대·같은 방향 요청 수
    history_minutes: float         # 그 기록을 모은 총 관측 시간(분)
    arrival_per_hour: float        # 평활한 도착률(시간당)
    accept_rate: float             # 평활한 수락률
    proposals_seen: int            # 수락률 계산에 쓴 과거 제안 수
    hour: int
    weekday: str
    seats: int
    detour_ratio: float
    match_type: str = "quick"      # quick | reserved


@dataclass
class Prediction:
    probability: float
    expected_minutes: float | None
    reason: str
    tip: str
    source: str                    # "gemini" | "groq" | "baseline"
    baseline: dict = field(default_factory=dict)
    features: dict = field(default_factory=dict)
    model: str | None = None
    error: str | None = None       # AI를 못 쓴 이유(키 노출 없이 요약)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["probability"] = round(self.probability, 3)
        d["expected_minutes"] = None if self.expected_minutes is None else round(self.expected_minutes, 1)
        return d


# ---------------------------------------------------------------------------
# 1단계: 통계 기본 추정치
# ---------------------------------------------------------------------------
def baseline(f: Features, match_interval_sec: int) -> tuple[float, float | None]:
    p = f.accept_rate
    lam = f.arrival_per_hour / 60.0 * p           # 분당, 수락까지 이어지는 도착률
    T = max(0.0, f.minutes_left)
    p_now = 1 - (1 - p) ** f.compatible_now
    p_later = 1 - math.exp(-lam * T) if lam > 0 else 0.0
    prob = 1 - (1 - p_now) * (1 - p_later)

    t_now = match_interval_sec / 60.0             # 다음 매칭 주기까지
    if lam > 0 and p_later > 1e-9:
        t_later = 1 / lam - T * math.exp(-lam * T) / p_later
    else:
        t_later = None
    if prob <= 1e-9:
        return prob, None
    parts = p_now * t_now + ((1 - p_now) * p_later * t_later if t_later is not None else 0.0)
    return prob, parts / prob


def compute_features(svc, req: dict, now: datetime) -> Features:
    cfg: Settings = svc.cfg
    me = svc._to_match_req({**req, "gender": svc.get_user(req["user_id"])["gender"]})
    deadline = matching_deadline(req, cfg)
    minutes_left = max(0.0, (deadline - now).total_seconds() / 60)

    # 지금 함께 탈 수 있는 대기자
    rows = svc.db.all("SELECT r.*, u.gender FROM requests r JOIN users u ON u.id = r.user_id "
                      "WHERE r.status = 'waiting' AND r.id != ?", req["id"])
    compatible = [r for r in rows if matching.pair_reject_reason(me, svc._to_match_req(r), cfg) is None]
    nearest = min((haversine_m(me.origin, Point.from_dict(r["origin"])) for r in compatible), default=None)

    # 과거 같은 시간대(±60분)·같은 방향·같은 성별 요청 수 → 도착률
    since = now - timedelta(days=cfg.history_days)
    hist = svc.db.all("SELECT r.origin, r.dest, r.created_at, u.gender FROM requests r JOIN users u ON u.id = r.user_id "
                      "WHERE r.user_id != ? AND r.created_at >= ?", req["user_id"], since.strftime("%Y-%m-%dT%H:%M:%S"))
    first = None
    count = 0
    for h in hist:
        created = datetime.fromisoformat(h["created_at"])
        first = created if first is None or created < first else first
        diff = abs((created.hour * 60 + created.minute) - (now.hour * 60 + now.minute))
        if min(diff, 1440 - diff) > 60:
            continue
        if h["gender"] != me.gender:
            continue
        o, d = Point.from_dict(h["origin"]), Point.from_dict(h["dest"])
        if haversine_m(o, me.origin) <= cfg.origin_radius_m and direction_cos(o, d, me.origin, me.dest) >= cfg.min_direction_cos:
            count += 1
    # 관측 시간: 하루마다 ±60분(120분) 창을 실제로 지켜본 만큼만 센다. 기록이 없으면 0 → 사전값만 쓴다
    span_min = 0.0 if first is None else min(cfg.history_days * 1440.0, (now - first).total_seconds() / 60)
    exposure = (span_min // 1440) * 120.0 + min(span_min % 1440, 120.0)
    prior_minutes = 120.0                                    # 사전 정보 = 하루치 관측과 같은 무게
    prior_count = cfg.prior_rate_per_hour / 60.0 * prior_minutes
    rate_per_min = (count + prior_count) / (exposure + prior_minutes)

    # 과거 제안의 수락률(베타 평활, 사전 = 10번 중 prior_accept 비율)
    groups = svc.db.all("SELECT status FROM groups WHERE is_solo = 0")
    accepted = sum(1 for g in groups if g["status"] in ("confirmed", "riding", "completed"))
    decided = sum(1 for g in groups if g["status"] != "proposed")
    accept_rate = (accepted + 10 * cfg.prior_accept) / (decided + 10)

    return Features(
        minutes_left=round(minutes_left, 1), compatible_now=len(compatible),
        nearest_now_m=None if nearest is None else round(nearest),
        history_matches=count, history_minutes=round(exposure), arrival_per_hour=round(rate_per_min * 60, 3),
        accept_rate=round(accept_rate, 3), proposals_seen=decided, hour=now.hour,
        weekday="월화수목금토일"[now.weekday()], seats=req["seats"], detour_ratio=req["detour_ratio"],
        match_type=req.get("type") or "quick",
    )


def matching_deadline(req: dict, cfg: Settings) -> datetime:
    end = datetime.fromisoformat(req["window_end"])
    return end - timedelta(minutes=cfg.deadline_min)


def baseline_text(f: Features, prob: float, minutes: float | None) -> tuple[str, str]:
    if f.compatible_now:
        reason = f"지금 같은 방향 대기자가 {f.compatible_now}명 있어 다음 매칭 때 묶일 가능성이 높아요."
    elif f.minutes_left <= 0:
        reason = "마감 시각이 지나 더 기다릴 수 없어요."
    else:
        reason = (f"지금 같은 방향 대기자는 없고, 이 시간대에는 같은 방향 요청이 시간당 약 {f.arrival_per_hour:.1f}건 "
                  f"들어와요. 마감까지 {f.minutes_left:.0f}분 남았어요.")
    tip = ""
    if prob < 0.5 and f.minutes_left > 0:
        tip = ("예약 매칭으로 시간대를 넓히면 확률이 올라가요." if f.match_type == "quick"
               else "희망 시간대를 늘리거나 허용 우회를 넓히면 확률이 올라가요.")
    return reason, tip


# ---------------------------------------------------------------------------
# 2단계: AI 보정
# ---------------------------------------------------------------------------
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "probability": {"type": "number", "description": "마감 전에 동승 매칭이 성사될 확률(0~1)"},
        "expected_minutes": {"type": "number", "description": "성사된다면 지금부터 걸릴 예상 시간(분)"},
        "reason": {"type": "string", "description": "근거를 담은 한국어 한두 문장"},
        "tip": {"type": "string", "description": "확률을 높일 한국어 제안 한 문장. 없으면 빈 문자열"},
    },
    "required": ["probability", "expected_minutes", "reason", "tip"],
    "additionalProperties": False,
}

SYSTEM = """너는 택시 동승 매칭 서비스 '모아타'의 예측 분석가다.
서버가 계산한 특징값과 통계 기본 추정치를 받아, 승객에게 보여 줄 매칭 확률과 예상 대기 시간을 정한다.
규칙:
- 근거는 주어진 숫자에서만 찾는다. 주어지지 않은 사실(날씨, 행사 등)을 지어내지 않는다.
- 기본 추정치를 출발점으로 삼고, 특징값이 분명한 이유를 줄 때만 조정한다(예: 과거 기록이 매우 적으면 불확실성을 언급).
- match_type이 quick이면 몇 분 안의 빠른 매칭, reserved면 미리 잡는 예약 매칭이다.
- probability는 0~1, expected_minutes는 0 이상 minutes_left 이하.
- reason은 승객이 이해할 수 있는 쉬운 한국어 한두 문장, 숫자 근거를 포함한다.
- tip은 승객이 바로 할 수 있는 행동 하나(시간대 넓히기, 우회 허용 늘리기 등). 확률이 이미 높으면 빈 문자열."""

DEFINITIONS = {
    "compatible_now": "지금 대기 중이며 성별·시간대·출발지·방향 조건을 모두 만족하는 사람 수",
    "arrival_per_hour": "같은 시간대·같은 방향 요청의 시간당 도착 수(과거 기록 + 사전값 평활)",
    "accept_rate": "과거 동승 제안이 확정까지 간 비율(사전값 평활)",
    "history_matches": "평활 전에 실제로 관측된 과거 요청 수. 적을수록 불확실",
}


def describe_error(exc: Exception) -> str:
    if isinstance(exc, AllProvidersFailed):
        return " / ".join(exc.errors)[:300] or "AI 제공자가 없습니다"
    return f"응답 형식 오류: {type(exc).__name__}: {str(exc)[:200]}"


class MatchPredictor:
    def __init__(self, cfg: Settings = settings, router: AIRouter | None = None):
        self.cfg = cfg
        self.router = router if router is not None else make_router(cfg)
        self._cache: dict[tuple, tuple[float, float, Prediction]] = {}   # 키 → (저장 시각, 유효 시간, 결과)

    @property
    def enabled(self) -> bool:
        return self.router.enabled

    def predict(self, svc, req: dict) -> Prediction:
        now = svc.now()
        f = compute_features(svc, req, now)
        prob, minutes = baseline(f, self.cfg.match_interval_sec)
        base = {"probability": round(prob, 3), "expected_minutes": None if minutes is None else round(minutes, 1)}

        key = (req["id"], f.compatible_now, f.history_matches, f.proposals_seen, int(f.minutes_left // MINUTE_BUCKET))
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < hit[1]:
            return hit[2]

        result = None
        error = None
        if self.router.enabled and f.minutes_left > 0:
            try:
                payload = {"features": asdict(f), "baseline": base, "definitions": DEFINITIONS}
                ai = self.router.generate_json(SYSTEM, payload, RESPONSE_SCHEMA, parse_order(self.cfg.ai_order_predict))
                g = ai.data
                gp = float(g["probability"])
                gm = g.get("expected_minutes")
                gp = min(max(gp, prob - MAX_ADJUST, 0.0), prob + MAX_ADJUST, 1.0)
                gm = None if gm is None or gp <= 0.001 else min(max(float(gm), 0.0), f.minutes_left)
                result = Prediction(gp, gm, str(g.get("reason", ""))[:300], str(g.get("tip", ""))[:200],
                                    ai.provider, base, asdict(f), ai.model)
            except (AllProvidersFailed, KeyError, ValueError, TypeError) as exc:
                error = describe_error(exc)
                log.warning("AI 예측 실패, 통계 추정치로 대체: %s", error)
        if result is None:
            reason, tip = baseline_text(f, prob, minutes)
            result = Prediction(prob, minutes, reason, tip, "baseline", base, asdict(f), error=error)
        ttl = FAIL_TTL_SEC if error else CACHE_TTL_SEC
        if len(self._cache) > 2000:
            self._cache.clear()
        self._cache[key] = (time.time(), ttl, result)
        return result
