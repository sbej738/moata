"""환경 변수로 바꿀 수 있는 설정값 모음.

.env 파일이나 셸 환경 변수로 덮어쓸 수 있다. 값이 없으면 아래 기본값을 쓴다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """의존성 없이 .env 파일을 읽어 환경 변수에 채운다(이미 있는 값은 유지).
    MOATA_SKIP_DOTENV=1이면 읽지 않는다(테스트가 .env 설정에 휘둘리지 않게)."""
    if not path.exists() or os.environ.get("MOATA_SKIP_DOTENV") == "1":
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


BASE_DIR = Path(__file__).resolve().parent.parent
_load_dotenv(BASE_DIR / ".env")


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def _b(name: str, default: bool) -> bool:
    return (os.environ.get(name) or str(default)).lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class FareTable:
    """택시 요금표. 기본값은 서울 중형택시 기준 예시값이므로 지역 요금에 맞게 바꿔 쓴다."""

    base_fare: int = _i("FARE_BASE", 4800)            # 기본요금(원)
    base_distance_m: int = _i("FARE_BASE_DISTANCE_M", 1600)  # 기본요금 포함 거리
    unit_distance_m: int = _i("FARE_UNIT_DISTANCE_M", 131)   # 거리요금 단위
    unit_fare: int = _i("FARE_UNIT", 100)              # 단위당 요금
    time_unit_sec: int = _i("FARE_TIME_UNIT_SEC", 30)  # 저속 시간요금 단위(초)
    slow_ratio: float = _f("FARE_SLOW_RATIO", 0.15)    # 저속 주행으로 보는 시간 비율(추정용)


@dataclass(frozen=True)
class Settings:
    db_path: Path | str = _s("DB_PATH") or BASE_DIR / "moata.db"
    timezone: str = _s("TIMEZONE", "Asia/Seoul")

    # 카카오 키. 비어 있으면 데모 모드(Leaflet 지도 + 직선거리 기반 추정)로 동작한다.
    kakao_rest_key: str = _s("KAKAO_REST_API_KEY")
    kakao_js_key: str = _s("KAKAO_JS_KEY")

    # 매칭 파라미터 (기획서 4장). 같은 성별끼리만 묶는 규칙은 설정으로 끌 수 없다.
    capacity: int = _i("CAPACITY", 3)                         # 그룹당 최대 승객 수
    min_overlap_min: int = _i("MIN_OVERLAP_MIN", 5)           # 희망 시간대 최소 겹침(분)
    origin_radius_m: float = _f("ORIGIN_RADIUS_M", 1000)      # 출발지 간 최대 직선거리
    min_direction_cos: float = _f("MIN_DIRECTION_COS", 0.7)   # 진행 방향 코사인 유사도 하한
    default_detour_ratio: float = _f("DEFAULT_DETOUR_RATIO", 0.3)  # 기본 허용 우회율 α
    match_interval_sec: int = _i("MATCH_INTERVAL_SEC", 30)    # 주기 작업 간격(마감 처리, AI 자동 매칭, 배차)
    deadline_min: int = _i("DEADLINE_MIN", 10)                # 희망 시간대 끝 N분 전 마감

    # 매칭 종류
    quick_window_min: int = _i("QUICK_WINDOW_MIN", 15)        # 빠른 매칭: 지금부터 N분 시간대(마감은 N − DEADLINE_MIN분 뒤)
    reserve_min_lead_min: int = _i("RESERVE_MIN_LEAD_MIN", 40)  # 예약은 지금부터 N분 이후 출발만
    reserve_max_days: int = _i("RESERVE_MAX_DAYS", 14)        # 예약은 최대 N일 뒤까지(2주)
    default_detour_min: int = _i("DEFAULT_DETOUR_MIN", 5)     # 우회 허용 기본값(분)
    quick_proposal_sec: int = _i("QUICK_PROPOSAL_SEC", 120)   # 빠른 매칭 제안 응답 시간
    reserved_proposal_min: int = _i("RESERVED_PROPOSAL_MIN", 30)  # 예약 매칭 제안 응답 시간

    # AI 자동 매칭(운영 탭 스위치의 처음 값). 꺼지면 '지금 매칭 돌리기'로만 매칭한다
    ai_auto_match: bool = _b("AI_AUTO_MATCH", True)

    # 배차
    dispatch_offer_sec: int = _i("DISPATCH_OFFER_SEC", 30)    # 기사님 배차 수락 대기 시간
    dispatch_lead_min: int = _i("DISPATCH_LEAD_MIN", 20)      # 출발 N분 전부터 배차
    dispatch_delay_alert_min: int = _i("DISPATCH_DELAY_ALERT_MIN", 3)  # 배차 못 한 채 N분 지나면 경보

    # 추정 경로(데모 모드)
    road_factor: float = _f("ROAD_FACTOR", 1.3)       # 직선거리 → 도로거리 보정 계수
    avg_speed_kmh: float = _f("AVG_SPEED_KMH", 25.0)  # 평균 주행 속도

    # AI 제공자: Groq(무료 플랜) · Gemini. 작업별 순서는 아래 AI_ORDER_*, 모두 실패하면 통계 추정·최적화
    gemini_api_key: str = _s("GEMINI_API_KEY")
    gemini_model: str = _s("GEMINI_MODEL", "gemini-3.6-flash")
    gemini_fallback_model: str = _s("GEMINI_FALLBACK_MODEL")  # 주 모델이 혼잡할 때 한 번 시도할 모델(비우면 안 씀)
    groq_api_key: str = _s("GROQ_API_KEY")
    groq_reasoning_effort: str = _s("GROQ_REASONING_EFFORT", "low")   # gpt-oss 생각 강도(low/medium/high, 비우면 기본)
    groq_model: str = _s("GROQ_MODEL", "openai/gpt-oss-120b")  # 없으면 이 키로 쓸 수 있는 모델로 자동 전환
    # 작업별 AI 순서(쉼표 구분). 키가 없는 제공자는 건너뛰고, 모두 실패하면 통계·최적화 결과를 쓴다
    ai_order_predict: str = _s("AI_ORDER_PREDICT", "gemini,groq")     # 매칭 예상(확률·대기 시간)
    ai_order_match: str = _s("AI_ORDER_MATCH", "groq,gemini")         # 매칭 판단(제안/대기)
    ai_order_dispatch: str = _s("AI_ORDER_DISPATCH", "groq,gemini")   # 배차 판단·추천 이유
    ai_order_summary: str = _s("AI_ORDER_SUMMARY", "gemini,groq")     # 시간별 운영 요약
    dispatch_max_extra_m: float = _f("DISPATCH_MAX_EXTRA_M", 1500)    # AI 배차가 최적보다 더 멀어도 되는 픽업 거리(m)
    dispatch_max_extra_ratio: float = _f("DISPATCH_MAX_EXTRA_RATIO", 0.5)  # 또는 최적 픽업 거리의 이 비율까지
    ai_timeout_sec: float = _f("AI_TIMEOUT_SEC", 8.0)        # 한 번 호출의 최대 대기 시간
    ai_cooldown_sec: int = _i("AI_COOLDOWN_SEC", 60)         # 한도 초과·연속 실패 뒤 그 제공자를 쉬는 시간

    # 매칭 예측
    prior_rate_per_hour: float = _f("PRIOR_RATE_PER_HOUR", 1.5)  # 기록이 없을 때 가정하는 '같은 방향 요청' 시간당 도착 수
    prior_accept: float = _f("PRIOR_ACCEPT", 0.7)                # 기록이 없을 때 가정하는 제안 수락률
    history_days: int = _i("HISTORY_DAYS", 14)                   # 도착률 계산에 쓰는 과거 기간

    # 수리 최적화(OR-Tools CP-SAT)
    optimizer_enabled: bool = _b("OPTIMIZER_ENABLED", True)
    optimizer_time_limit_sec: float = _f("OPTIMIZER_TIME_LIMIT_SEC", 2.0)

    # 정산 파라미터 (기획서 5장)
    min_discount: float = _f("MIN_DISCOUNT", 0.10)    # 단독 대비 최소 할인율 β
    rounding_unit: int = _i("ROUNDING_UNIT", 10)      # 10원 단위 내림

    # 계정
    admin_id: str = _s("ADMIN_ID", "admin")           # 운영자 로그인 아이디(이메일이 아님)
    admin_password: str = _s("ADMIN_PASSWORD")        # 비우면 운영자 계정을 만들지 않는다
    demo_password: str = _s("DEMO_PASSWORD")          # 비우면 데모 학생·기사 계정을 만들지 않는다
    student_email_domain: str = _s("STUDENT_EMAIL_DOMAIN", "kangwon.ac.kr")
    session_days: int = _i("SESSION_DAYS", 14)
    cookie_secure: bool = _b("COOKIE_SECURE", False)  # HTTPS로 배포할 때 true
    demo_tools: bool = _b("DEMO_TOOLS", True)         # 시연용 버튼(데모 동승자 만들기 등)

    # 신뢰도·포인트(가상 금액)
    trust_start: int = _i("TRUST_START", 40)                  # 가입할 때 신뢰도
    trust_low: int = _i("TRUST_LOW", 20)                      # 이 점수 미만이면 합승 때 추가 부담(유일한 패널티)
    trust_low_surcharge: float = _f("TRUST_LOW_SURCHARGE", 0.15)   # 추가 부담 비율(그만큼 동승자 요금이 준다)
    trust_low_min_discount: float = _f("TRUST_LOW_MIN_DISCOUNT", 0.05)  # 추가 부담해도 혼자 탈 때보다 최소 이만큼 싸다
    trust_ride_gain: int = _i("TRUST_RIDE_GAIN", 1)           # 문제없이 끝낸 운행 1회
    trust_weekly_gain_max: int = _i("TRUST_WEEKLY_GAIN_MAX", 5)
    trust_recovery_gain: int = _i("TRUST_RECOVERY_GAIN", 3)   # 기준점 미만일 때 운행 1회(추가 부담을 감수한 회복)
    trust_milestone_every: int = _i("TRUST_MILESTONE_EVERY", 10)
    trust_milestone_bonus: int = _i("TRUST_MILESTONE_BONUS", 3)
    trust_late_cancel: int = _i("TRUST_LATE_CANCEL", -10)     # 출발 30분 이내 취소
    trust_no_show: int = _i("TRUST_NO_SHOW", -20)
    trust_report: int = _i("TRUST_REPORT", -15)               # 운영자가 사실로 확인한 신고
    start_points: int = _i("START_POINTS", 500000)            # 가입할 때 주는 포인트
    deposit_ratio: float = _f("DEPOSIT_RATIO", 1.0)           # 예약금 = 예상 몫 × 이 비율(모두 같음)
    no_show_wait_min: int = _i("NO_SHOW_WAIT_MIN", 3)         # 탑승 지점 도착 후 이만큼 기다려야 노쇼 처리 가능
    late_cancel_min: int = _i("LATE_CANCEL_MIN", 30)          # 출발 이 시간 안의 취소는 패널티

    fare: FareTable = field(default_factory=FareTable)

    @property
    def map_provider(self) -> str:
        return "kakao" if self.kakao_js_key else "leaflet"

    @property
    def route_provider(self) -> str:
        return "kakao" if self.kakao_rest_key else "estimate"


settings = Settings()
