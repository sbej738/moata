"""업무 로직: 계정 → 요청(빠른/예약) → 매칭(OR-Tools + AI 판단) → 수락/거절 → 탑승 전 안내 → 배차 → 운행 → 정산.

API 계층(main.py)은 이 클래스의 메서드만 부른다. 상태 변경은 모두 Database.tx() 안에서
한 번에 하나씩 처리한다. AI 호출(수 초)은 DB 잠금 밖에서 하고, 결과를 쓸 때 상태가 바뀌지 않았는지 다시 확인한다.
"""
from __future__ import annotations

import json
import logging
import math
import random
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import auth, matching, optimize, safety
from .agent import DispatchAgent, MatchAgent, OpsSummarizer
from .config import Settings, settings
from .db import Database, dumps
from .demo_places import DEMO_PLACES
from .fare import Stop, apply_trust_surcharge, split_fare
from .geo import Point, haversine_m
from .llm import AIRouter, AllProvidersFailed, make_router, parse_order
from .predict import MatchPredictor

log = logging.getLogger("moata.service")

ACTIVE_REQUEST = ("waiting", "proposed", "confirmed", "riding", "dropped")
FMT = "%Y-%m-%dT%H:%M"
STAMP = "%Y-%m-%dT%H:%M:%S"
URGENT_MIN = 5            # 마감까지 이 시간(분) 이하인 승객은 AI가 '기다리기'로 둘 수 없다
WORSE_RATIO = 0.8         # AI 선택 절감액이 최적 계획의 80% 미만이면 경보
FALLBACK_WINDOW_MIN = 10  # 대체 비율을 보는 기간
FALLBACK_ALERT_RATIO = 0.5
ALERT_DEDUPE_MIN = 10     # 같은 종류의 열린 경보가 있으면 새로 만들지 않고 횟수만 올린다


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value).replace(second=0, microsecond=0, tzinfo=None)
    except (TypeError, ValueError) as exc:
        raise ServiceError(f"시간 형식이 올바르지 않습니다: {value}") from exc


def parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).replace(tzinfo=None) if value else None


class TaxiShareService:
    def __init__(self, db: Database, cfg: Settings = settings, provider=None, clock=None, predictor=None,
                 router: AIRouter | None = None):
        from .routing import make_provider
        self.db = db
        self.cfg = cfg
        self.provider = provider or make_provider(cfg)
        self.router = router if router is not None else (predictor.router if predictor else make_router(cfg))
        self.predictor = predictor or MatchPredictor(cfg, self.router)
        self.match_agent = MatchAgent(self.router, order=parse_order(cfg.ai_order_match))
        self.dispatch_agent = DispatchAgent(self.router, order=parse_order(cfg.ai_order_dispatch))
        self.summarizer = OpsSummarizer(self.router, order=parse_order(cfg.ai_order_summary))
        self._clock = clock or (lambda: datetime.now(ZoneInfo(cfg.timezone)).replace(tzinfo=None))
        self._match_lock = threading.Lock()
        self._dispatch_lock = threading.Lock()
        self._all_declined_since: dict[int, datetime] = {}   # 운행 가능한 기사님이 모두 거절한 그룹 → 처음 본 시각
        self.last_match: dict | None = None

    # =====================================================================
    # 시간 · 설정
    # =====================================================================
    def now(self) -> datetime:
        return self._clock().replace(second=0, microsecond=0)

    def ts(self) -> datetime:
        return self._clock().replace(microsecond=0)

    def _stamp(self) -> str:
        return self._clock().strftime(STAMP)

    def get_setting(self, key: str, default):
        row = self.db.one("SELECT value FROM app_settings WHERE key = ?", key)
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        with self.db.tx() as c:
            c.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, dumps(value)))

    @property
    def auto_match(self) -> bool:
        return bool(self.get_setting("ai_auto_match", self.cfg.ai_auto_match))

    # =====================================================================
    # 계정
    # =====================================================================
    def _insert_user(self, nickname: str, gender: str | None, role: str = "passenger", email: str | None = None,
                     password: str | None = None, is_demo: bool = False, login_id: str | None = None) -> int:
        nickname = (nickname or "").strip()
        if not nickname:
            raise ServiceError("닉네임을 입력하세요")
        if role == "passenger" and gender not in ("F", "M"):
            raise ServiceError("성별을 선택하세요", 422)
        if email and self.db.one("SELECT id FROM users WHERE email = ?", email):
            raise ServiceError("이미 가입된 이메일이에요", 409)
        if login_id and self.db.one("SELECT id FROM users WHERE login_id = ?", login_id):
            raise ServiceError("이미 쓰고 있는 아이디예요", 409)
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO users (email, login_id, password_hash, role, nickname, gender, is_demo, trust, points, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,0,?)",
                (email, login_id, auth.hash_password(password) if password else None, role, nickname[:20],
                 gender if role == "passenger" else None, int(is_demo), self.cfg.trust_start, self._stamp()))
            uid = cur.lastrowid
            if role == "passenger":
                self._wallet(c, uid, self.cfg.start_points, "grant", "가입 축하 포인트")
                c.execute("INSERT INTO trust_events (user_id, delta, trust_after, kind, reason, created_at) VALUES (?,?,?,?,?,?)",
                          (uid, 0, self.cfg.trust_start, "join", "가입", self._stamp()))
        return uid

    # =====================================================================
    # 포인트(가상 금액) · 신뢰도
    # =====================================================================
    def _wallet(self, c, user_id: int, amount: int, kind: str, note: str = "", group_id: int | None = None,
                request_id: int | None = None) -> int:
        """포인트 잔액을 바꾸고 내역을 남긴다. 돌려주는 값: 바뀐 잔액."""
        c.execute("UPDATE users SET points = points + ? WHERE id = ?", (int(amount), user_id))
        bal = c.execute("SELECT points FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        c.execute("INSERT INTO wallet_tx (user_id, amount, kind, balance_after, note, group_id, request_id, created_at) "
                  "VALUES (?,?,?,?,?,?,?,?)", (user_id, int(amount), kind, bal, note[:120], group_id, request_id, self._stamp()))
        return bal

    def _trust(self, c, user_id: int, delta: int, kind: str, reason: str = "", group_id: int | None = None) -> int:
        """신뢰도를 0~100 안에서 바꾸고 내역을 남긴다. 돌려주는 값: 실제로 바뀐 양."""
        cur = c.execute("SELECT trust FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        new = max(0, min(100, cur + int(delta)))
        if new == cur and delta != 0 and kind not in ("late_cancel", "no_show", "report"):
            return 0
        c.execute("UPDATE users SET trust = ? WHERE id = ?", (new, user_id))
        c.execute("INSERT INTO trust_events (user_id, delta, trust_after, kind, reason, group_id, created_at) VALUES (?,?,?,?,?,?,?)",
                  (user_id, new - cur, new, kind, reason[:120], group_id, self._stamp()))
        return new - cur

    def _ride_trust(self, c, user_id: int, group_id: int) -> None:
        """문제없이 끝낸 운행: 기준점 미만이면 회복(+3, 추가 부담을 감수했으니), 아니면 +1(주 최대 +5). 10회마다 보너스."""
        u = c.execute("SELECT trust, rides_done FROM users WHERE id = ?", (user_id,)).fetchone()
        rides = u[1] + 1
        c.execute("UPDATE users SET rides_done = ? WHERE id = ?", (rides, user_id))
        if u[0] < self.cfg.trust_low:
            self._trust(c, user_id, self.cfg.trust_recovery_gain, "recovery", "추가 부담을 감수하고 운행을 마쳐 회복", group_id)
        else:
            since = (self.ts() - timedelta(days=7)).strftime(STAMP)
            week = c.execute("SELECT COALESCE(SUM(delta), 0) FROM trust_events WHERE user_id = ? AND kind = 'ride' "
                             "AND created_at >= ?", (user_id, since)).fetchone()[0]
            gain = min(self.cfg.trust_ride_gain, max(0, self.cfg.trust_weekly_gain_max - week))
            if gain > 0:
                self._trust(c, user_id, gain, "ride", "문제없이 운행을 마침", group_id)
        if self.cfg.trust_milestone_every and rides % self.cfg.trust_milestone_every == 0:
            self._trust(c, user_id, self.cfg.trust_milestone_bonus, "milestone", f"{rides}번째 운행", group_id)

    def _surcharge_rate(self, m: dict) -> float:
        """수락한 사람은 수락 때 정한 비율, 아직이면 지금 신뢰도로 계산한 비율."""
        if m.get("base_share") is not None:
            return float(m.get("surcharge_rate") or 0)
        return self.cfg.trust_low_surcharge if (m.get("trust") if m.get("trust") is not None else 100) < self.cfg.trust_low else 0.0

    def wallet(self, user: dict) -> dict:
        u = self.get_user(user["id"])
        held = self.db.scalar(f"SELECT COALESCE(SUM(hold_amount), 0) FROM requests WHERE user_id = ? AND status IN {ACTIVE_REQUEST}",
                              user["id"]) or 0
        return {
            "points": u["points"], "held": held, "trust": u["trust"], "rides_done": u["rides_done"],
            "trust_low": self.cfg.trust_low, "low": u["trust"] < self.cfg.trust_low,
            "surcharge": self.cfg.trust_low_surcharge,
            "tx": self.db.all("SELECT * FROM wallet_tx WHERE user_id = ? ORDER BY id DESC LIMIT 30", user["id"]),
            "trust_events": self.db.all("SELECT * FROM trust_events WHERE user_id = ? ORDER BY id DESC LIMIT 20", user["id"]),
        }

    def topup(self, user: dict, amount: int = 100000) -> dict:
        if user["role"] != "passenger":
            raise ServiceError("승객만 포인트를 충전할 수 있어요", 403)
        with self.db.tx() as c:
            self._wallet(c, user["id"], amount, "topup", "데모 충전")
        return self.wallet(user)

    def create_user(self, nickname: str, gender: str, is_demo: bool = False) -> dict:
        """로그인 없는 승객(데모 동승자, 테스트용)."""
        if gender not in ("F", "M"):
            raise ServiceError("성별은 F 또는 M 이어야 합니다", 422)
        return self.get_user(self._insert_user(nickname, gender, is_demo=is_demo))

    def signup_student(self, email: str, password: str, nickname: str, gender: str) -> dict:
        email = auth.normalize_email(email)
        if not auth.student_email_ok(email, self.cfg.student_email_domain):
            raise ServiceError(f"학교 이메일(학번@{self.cfg.student_email_domain}) 형식으로 입력하세요", 422)
        problem = auth.password_problem(password)
        if problem:
            raise ServiceError(problem, 422)
        if gender not in ("F", "M"):
            raise ServiceError("성별을 선택하세요", 422)
        return self.public_user(self.get_user(self._insert_user(nickname, gender, "passenger", email, password)))

    def signup_driver(self, login_id: str, password: str, name: str, car_number: str, car_model: str = "",
                      car_color: str = "", lat: float | None = None, lng: float | None = None) -> dict:
        login_id = auth.normalize_login_id(login_id)
        if not auth.login_id_ok(login_id):
            raise ServiceError("아이디는 영문 소문자·숫자·밑줄(_) 4~20자로 정해 주세요", 422)
        problem = auth.password_problem(password)
        if problem:
            raise ServiceError(problem, 422)
        if not name.strip() or not car_number.strip():
            raise ServiceError("이름과 차량 번호를 입력하세요", 422)
        uid = self._insert_user(name, None, "driver", None, password, login_id=login_id)
        center = DEMO_PLACES[0]
        with self.db.tx() as c:
            c.execute("INSERT INTO drivers (user_id, name, car_number, car_model, car_color, available, lat, lng, updated_at) "
                      "VALUES (?,?,?,?,?,0,?,?,?)",
                      (uid, name.strip()[:20], car_number.strip()[:20], car_model.strip()[:30], car_color.strip()[:20],
                       lat if lat is not None else center["lat"], lng if lng is not None else center["lng"], self._stamp()))
        return self.public_user(self.get_user(uid))

    def login(self, email: str, password: str, tab: str) -> tuple[dict, str]:
        """tab: student | driver. 돌려주는 값: (사용자, 앱 이름).

        학생은 학교 이메일로, 기사님과 운영자는 아이디로 로그인한다. 운영자는 학생 탭에서 아이디로 로그인하면
        운영 페이지로 간다.
        """
        ident = (email or "").strip()
        if "@" in ident:
            if tab == "driver":
                raise ServiceError("기사님은 이메일이 아니라 아이디로 로그인해요", 422)
            user = self.db.one("SELECT * FROM users WHERE email = ?", auth.normalize_email(ident))
        else:
            user = self.db.one("SELECT * FROM users WHERE login_id = ?", auth.normalize_login_id(ident))
        if not user or not auth.verify_password(password, user["password_hash"]):
            raise ServiceError("아이디(이메일) 또는 비밀번호가 맞지 않아요", 401)
        if tab == "driver":
            if user["role"] != "driver":
                raise ServiceError("기사님 계정이 아니에요. 학생 탭에서 로그인하세요", 403)
            return self.public_user(user), "driver"
        if user["role"] == "driver":
            raise ServiceError("기사님 계정은 기사님 탭에서 로그인하세요", 403)
        return self.public_user(user), ("admin" if user["role"] == "admin" else "passenger")

    def create_session(self, user_id: int, app: str) -> str:
        token = auth.new_token()
        expires = self.ts() + timedelta(days=self.cfg.session_days)
        with self.db.tx() as c:
            c.execute("INSERT INTO sessions (token, user_id, app, created_at, expires_at) VALUES (?,?,?,?,?)",
                      (token, user_id, app, self._stamp(), expires.strftime(STAMP)))
        return token

    def session_user(self, token: str | None, app: str) -> dict | None:
        if not token:
            return None
        s = self.db.one("SELECT * FROM sessions WHERE token = ? AND app = ?", token, app)
        if not s or parse_ts(s["expires_at"]) < self.ts():
            return None
        return self.db.one("SELECT * FROM users WHERE id = ?", s["user_id"])

    def logout(self, token: str | None) -> None:
        if token:
            with self.db.tx() as c:
                c.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def get_user(self, user_id: int) -> dict:
        user = self.db.one("SELECT * FROM users WHERE id = ?", user_id)
        if not user:
            raise ServiceError("사용자를 찾을 수 없습니다", 404)
        return user

    @staticmethod
    def public_user(user: dict) -> dict:
        return {k: user[k] for k in ("id", "email", "login_id", "role", "nickname", "gender", "penalty", "is_demo",
                                     "trust", "points", "rides_done", "created_at")}

    def me(self, user: dict) -> dict:
        out = self.public_user(user)
        if user["role"] == "driver":
            out["driver"] = self.get_driver(user["id"])
        return out

    def update_profile(self, user: dict, nickname: str | None = None, gender: str | None = None) -> dict:
        if gender is not None and gender != user["gender"]:
            raise ServiceError("성별은 가입 후 직접 바꿀 수 없어요. 운영팀에 정정을 요청하세요", 403)
        if nickname is not None:
            if not nickname.strip():
                raise ServiceError("닉네임을 입력하세요")
            with self.db.tx() as c:
                c.execute("UPDATE users SET nickname = ? WHERE id = ?", (nickname.strip()[:20], user["id"]))
        return self.public_user(self.get_user(user["id"]))

    def admin_set_gender(self, user_id: int, gender: str) -> dict:
        user = self.get_user(user_id)
        if user["role"] != "passenger":
            raise ServiceError("승객 계정만 성별을 정정할 수 있어요")
        if gender not in ("F", "M"):
            raise ServiceError("성별은 F 또는 M 이어야 합니다", 422)
        if self.db.one(f"SELECT id FROM requests WHERE user_id = ? AND status IN {ACTIVE_REQUEST}", user_id):
            raise ServiceError("진행 중인 요청이 끝난 뒤에 정정할 수 있어요", 409)
        with self.db.tx() as c:
            c.execute("UPDATE users SET gender = ? WHERE id = ?", (gender, user_id))
        return self.public_user(self.get_user(user_id))

    def list_users(self) -> list[dict]:
        return [self.public_user(u) for u in self.db.all("SELECT * FROM users ORDER BY role, is_demo, id")]

    def seed_accounts(self) -> dict:
        """처음 실행할 때 운영자·데모 계정을 만든다. 비밀번호는 .env(ADMIN_PASSWORD, DEMO_PASSWORD)에서 읽는다."""
        made = {"admin": 0, "students": 0, "drivers": 0}
        admin_id = auth.normalize_login_id(self.cfg.admin_id)
        if self.cfg.admin_password and not self.db.one("SELECT id FROM users WHERE login_id = ?", admin_id):
            self._insert_user(admin_id, None, "admin", None, self.cfg.admin_password, login_id=admin_id)
            made["admin"] = 1
        if self.cfg.demo_password:
            d = self.cfg.student_email_domain
            # 데모 계정은 이름과 아이디를 같게 둔다(학생 user1~4, 기사님 driver1~2). 학생은 학교 이메일로도 로그인된다
            for n, (g, local) in enumerate((("F", "20260001"), ("F", "20260002"), ("M", "20260003"), ("M", "20260004")),
                                           start=1):
                email = f"{local}@{d}"
                if not self.db.one("SELECT id FROM users WHERE email = ?", email):
                    uid = f"user{n}" if not self.db.one("SELECT id FROM users WHERE login_id = ?", f"user{n}") else None
                    self._insert_user(uid or f"user{n}", g, "passenger", email, self.cfg.demo_password, is_demo=True,
                                      login_id=uid)
                    made["students"] += 1
            places = {p["name"]: p for p in DEMO_PLACES}
            for login_id, name, car, model, color, place in (
                    ("driver1", "driver1", "강원 31바 1234", "쏘나타", "흰색", "삼척종합버스터미널"),
                    ("driver2", "driver2", "강원 32사 5678", "K5", "은색", "삼척역")):
                if not self.db.one("SELECT id FROM users WHERE login_id = ?", login_id):
                    p = places[place]
                    self.signup_driver(login_id, self.cfg.demo_password, name, car, model, color, p["lat"], p["lng"])
                    with self.db.tx() as c:
                        c.execute("UPDATE users SET is_demo = 1 WHERE login_id = ?", (login_id,))
                    made["drivers"] += 1
        return made

    # =====================================================================
    # 견적 · 요청
    # =====================================================================
    def quote(self, origin: Point, dest: Point) -> dict:
        r = self.provider.route([origin, dest])
        return {"distance_m": round(r.total_m), "duration_s": round(r.duration_s), "taxi_fare": r.taxi_fare,
                "toll": r.toll, "provider": r.provider, "polyline": r.polyline}

    def create_request(self, user_id: int, origin: Point, dest: Point, window_start: str | None = None,
                       window_end: str | None = None, seats: int = 1, detour_ratio: float | None = None,
                       fallback: str = "solo", type: str = "reserved", companion_same_gender: bool = False,
                       detour_min: int | None = None) -> dict:
        user = self.get_user(user_id)
        if user["role"] != "passenger":
            raise ServiceError("승객 계정만 동승을 요청할 수 있어요", 403)
        now = self.now()
        if type == "quick":
            ws, we = now, now + timedelta(minutes=self.cfg.quick_window_min)
        elif type == "reserved":
            if not window_start or not window_end:
                raise ServiceError("예약 매칭은 희망 시간대를 정해야 해요")
            ws, we = parse_dt(window_start), parse_dt(window_end)
        else:
            raise ServiceError("매칭 종류는 quick 또는 reserved 입니다")
        if we <= ws:
            raise ServiceError("희망 시간대의 끝이 시작보다 늦어야 합니다")
        if we - ws < timedelta(minutes=self.cfg.min_overlap_min):
            raise ServiceError(f"희망 시간대는 최소 {self.cfg.min_overlap_min}분 이상이어야 합니다")
        if we <= now + timedelta(minutes=self.cfg.deadline_min):
            raise ServiceError(f"희망 시간대 끝이 지금으로부터 {self.cfg.deadline_min}분 이후여야 매칭할 시간이 있습니다")
        if type == "reserved":
            if ws < now + timedelta(minutes=self.cfg.reserve_min_lead_min):
                raise ServiceError(f"예약은 {self.cfg.reserve_min_lead_min}분 뒤 출발부터 할 수 있어요. "
                                   "곧 출발하려면 빠른 매칭을 쓰세요")
            if ws > now + timedelta(days=self.cfg.reserve_max_days):
                raise ServiceError(f"예약은 최대 {self.cfg.reserve_max_days}일 뒤까지 할 수 있어요")
            if we - ws > timedelta(hours=3):
                raise ServiceError("희망 시간대는 3시간 이내로 정해 주세요")
        if not 1 <= seats <= self.cfg.capacity - 1:
            raise ServiceError(f"인원은 1~{self.cfg.capacity - 1}명까지 가능합니다")
        if seats > 1 and not companion_same_gender:
            raise ServiceError("동행인도 같은 성별인지 확인해 주세요(같은 성별끼리만 동승할 수 있어요)")
        if fallback not in ("solo", "cancel"):
            raise ServiceError("미매칭 시 처리 방식은 solo 또는 cancel 입니다")
        if haversine_m(origin, dest) < 200:
            raise ServiceError("출발지와 도착지가 너무 가깝습니다")
        if detour_min is not None and not 1 <= int(detour_min) <= 60:
            raise ServiceError("우회 허용은 1~60분 사이로 골라 주세요")
        detour = self.cfg.default_detour_ratio if detour_ratio is None else float(detour_ratio)
        if detour_min is None and not 0.05 <= detour <= 1.0:
            raise ServiceError("허용 우회율은 5%~100% 사이여야 합니다")
        if self.db.one(f"SELECT id FROM requests WHERE user_id = ? AND status IN {ACTIVE_REQUEST}", user_id):
            raise ServiceError("이미 진행 중인 요청이 있습니다. 먼저 취소하거나 운행을 마치세요")

        solo = self.provider.route([origin, dest])
        if detour_min is not None:
            # 분 단위 우회 허용 → 단독 소요 시간 대비 비율. 매칭 규칙(d ≤ (1+α)·D)은 그대로 쓴다
            detour = min(1.0, max(0.05, int(detour_min) * 60 / max(solo.duration_s, 60)))
        with self.db.tx() as c:
            cur = c.execute(
                """INSERT INTO requests (user_id, type, origin, dest, window_start, window_end, seats, detour_ratio,
                   detour_min, fallback, status, solo_m, solo_fare, solo_duration_s, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'waiting', ?,?,?,?)""",
                (user_id, type, dumps(origin.to_dict()), dumps(dest.to_dict()), ws.strftime(FMT), we.strftime(FMT),
                 seats, detour, detour_min, fallback, solo.total_m, solo.taxi_fare, solo.duration_s, self._stamp()))
        return self.get_request(cur.lastrowid)

    def get_request(self, request_id: int) -> dict:
        r = self.db.one("SELECT r.*, u.nickname, u.gender, u.trust FROM requests r JOIN users u ON u.id = r.user_id "
                        "WHERE r.id = ?", request_id)
        if not r:
            raise ServiceError("요청을 찾을 수 없습니다", 404)
        r["deadline"] = (parse_dt(r["window_end"]) - timedelta(minutes=self.cfg.deadline_min)).strftime(FMT)
        return r

    def own_request(self, user: dict, request_id: int) -> dict:
        r = self.get_request(request_id)
        if r["user_id"] != user["id"]:
            raise ServiceError("내 요청이 아니에요", 403)
        return r

    def predict_request(self, request_id: int) -> dict:
        """매칭 대기 중인 요청의 성사 확률과 예상 대기 시간(통계 추정 + AI 보정)."""
        r = self.get_request(request_id)
        if r["status"] != "waiting":
            raise ServiceError("매칭을 기다리는 요청만 예측할 수 있습니다")
        return self.predictor.predict(self, r).to_dict()

    def cancel_request(self, request_id: int) -> dict:
        with self.db.tx() as c:
            r = self.get_request(request_id)
            status = r["status"]
            if status in ("riding", "dropped"):
                raise ServiceError("운행 중에는 취소할 수 없습니다")
            if status not in ACTIVE_REQUEST:
                raise ServiceError("이미 끝난 요청입니다")
            if status == "proposed":
                self._dissolve(r["group_id"], rejecters=[request_id], reason="동승자 취소")
            elif status == "confirmed":
                if self.get_group_row(r["group_id"])["status"] == "riding":
                    raise ServiceError("기사님이 오고 있어 앱에서 취소할 수 없어요. 신고하기에서 운영팀에 알려 주세요", 409)
                self._leave_confirmed_group(r)
            c.execute("UPDATE requests SET status = 'cancelled', group_id = NULL WHERE id = ?", (request_id,))
        return self.get_request(request_id)

    # =====================================================================
    # 매칭: 마감 처리 → OR-Tools 최적 계획 → AI 판단 → 검증 → 제안
    # =====================================================================
    def run_matching(self, batch: bool = True, use_ai: bool | None = None, trigger: str = "manual") -> dict:
        """batch=False면 마감 처리(제안 해산·단독 전환·만료)만 하고 새 그룹은 만들지 않는다."""
        result = {"at": self._stamp(), "trigger": trigger, "batch": batch, "created": [], "solo": [], "expired": [],
                  "dissolved": [], "waiting": 0, "method": None, "ai": None, "alerts": []}
        self._process_deadlines(result)
        rows = self._waiting_rows()
        result["waiting"] = len(rows)
        if not batch or len(rows) < 2:
            if batch:
                self.last_match = result
            return result
        if not self._match_lock.acquire(timeout=15):
            result["skipped"] = "다른 매칭이 진행 중입니다"
            return result
        try:
            self._match_batch(rows, result, use_ai)
        finally:
            self._match_lock.release()
        self.last_match = result
        if result["created"] or result["solo"]:
            self.dispatch_cycle()
        return result

    def _waiting_rows(self) -> list[dict]:
        return self.db.all("SELECT r.*, u.gender FROM requests r JOIN users u ON u.id = r.user_id "
                           "WHERE r.status = 'waiting' ORDER BY r.id")

    def _process_deadlines(self, result: dict) -> None:
        with self.db.tx() as c:
            now, ts = self.now(), self.ts()
            deadline = timedelta(minutes=self.cfg.deadline_min)
            # 1) 응답 시간이 지났거나 마감까지 전원이 수락하지 않은 제안은 해산
            for g in self.db.all("SELECT * FROM groups WHERE status = 'proposed'"):
                members = [self.get_request(rid) for rid in g["member_ids"]]
                timeout = g["expires_at"] and ts >= parse_ts(g["expires_at"])
                if timeout or now >= min(parse_dt(m["window_end"]) for m in members) - deadline:
                    silent = [rid for rid in g["member_ids"] if rid not in g["accepted"]]
                    self._dissolve(g["id"], rejecters=silent if timeout else [],
                                   reason="응답 시간이 지나 제안이 취소됐어요" if timeout else "마감까지 수락이 끝나지 않음")
                    result["dissolved"].append(g["id"])
            # 2) 마감이 지난 대기 요청은 미리 고른 방식(단독 전환/자동 취소) 적용
            for r in self.db.all("SELECT * FROM requests WHERE status = 'waiting'"):
                if now >= parse_dt(r["window_end"]) - deadline:
                    if r["fallback"] == "solo":
                        gid = self._create_group([self.get_request(r["id"])],
                                                 [Stop(r["id"], "pickup"), Stop(r["id"], "dropoff")],
                                                 status="confirmed", is_solo=True, method="solo",
                                                 reason="마감까지 동승자를 찾지 못해 단독 탑승으로 전환")
                        result["solo"].append(gid)
                    else:
                        c.execute("UPDATE requests SET status = 'expired', note = '마감까지 동승자를 찾지 못함' "
                                  "WHERE id = ?", (r["id"],))
                        result["expired"].append(r["id"])
            # 3) 확정됐지만 희망 시간대가 끝나고 DEADLINE_MIN분이 더 지나도록 기사님을 못 찾은 그룹은 취소한다.
            #    그러지 않으면 승객은 계속 '기사님 찾는 중'에 머물고 예약금도 잡힌 채로 남는다
            for g in self.db.all("SELECT * FROM groups WHERE status = 'confirmed' AND driver_id IS NULL"):
                members = [self.get_request(rid) for rid in g["member_ids"]]
                if now < max(parse_dt(m["window_end"]) for m in members) + deadline:
                    continue
                c.execute("UPDATE groups SET status = 'cancelled', offer_driver_id = NULL, offer_expires_at = NULL, "
                          "updated_at = ? WHERE id = ?", (self._stamp(), g["id"]))
                for m in members:
                    self._release_hold(c, m["id"], "기사님을 찾지 못해 예약금 돌려받음")
                    c.execute("UPDATE requests SET status = 'expired', note = ? WHERE id = ?",
                              ("기사님을 찾지 못해 운행이 취소됐어요. 예약금은 모두 돌려드렸어요", m["id"]))
                self._all_declined_since.pop(g["id"], None)
                result.setdefault("no_driver", []).append(g["id"])
                self.raise_alert("dispatch_failed", "medium", "기사님을 끝내 찾지 못해 운행을 취소했어요",
                                 f"그룹 #{g['id']}({g['departure'][11:]} 출발 예정)을 취소하고 예약금을 돌려줬어요",
                                 group_id=g["id"], dedupe=False)

    def _priority(self, row: dict) -> float:
        mins = self._minutes_to_deadline(row)
        urgency = max(0.0, 1.0 - mins / 30.0)
        return round(urgency + min(1.0, 0.3 * len(row["excluded"])), 3)

    def _minutes_to_deadline(self, row: dict) -> float:
        end = parse_dt(row["window_end"]) - timedelta(minutes=self.cfg.deadline_min)
        return max(0.0, (end - self.ts()).total_seconds() / 60)

    def _match_batch(self, rows: list[dict], result: dict, use_ai: bool | None) -> None:
        by_id = {r["id"]: r for r in rows}
        reqs = [self._to_match_req(r) for r in rows]
        priority = {r["id"]: self._priority(r) for r in rows}
        plan = optimize.plan_groups(reqs, self.cfg, priority=priority)
        result["method"] = plan.method
        result["optimizer"] = {"method": plan.method, "objective": round(plan.objective, 1),
                               "greedy_objective": round(plan.greedy_objective, 1), "candidates": len(plan.candidates),
                               "saving_m": round(plan.saving_m), "elapsed_ms": plan.elapsed_ms, "note": plan.note}
        if not plan.candidates and not plan.groups:
            return

        pool = {c.key: c for c in plan.candidates}
        pool.update({c.key: c for c in plan.groups})
        use_ai = self.router.enabled if use_ai is None else (use_ai and self.router.enabled)
        chosen: list[tuple[optimize.Candidate, str, str]] = []   # (후보, 방법, 이유)
        waited: list[optimize.Candidate] = []
        violations: list[str] = []
        overridden: list[str] = []
        decision = None
        error = None
        payload = self._agent_payload(plan, pool, by_id) if use_ai else None

        if use_ai:
            try:
                signature = (tuple(sorted(tuple(sorted(c.ids)) for c in pool.values())),
                             int(min(self._minutes_to_deadline(r) for r in rows) // 2))
                decision = self.match_agent.decide(payload, signature)
            except (AllProvidersFailed, ValueError, KeyError, TypeError) as exc:
                error = str(exc)[:300]
                log.warning("AI 매칭 판단 실패, 최적 계획을 그대로 씁니다: %s", error)

        used: set[int] = set()
        if decision:
            for cid in decision.propose:
                c = pool.get(cid)
                if c is None:
                    violations.append(f"후보 목록에 없는 그룹({cid})을 제안함")
                    continue
                if set(c.ids) & used:
                    violations.append(f"{cid}가 다른 제안과 승객이 겹침")
                    continue
                chosen.append((c, "ai", decision.reasons.get(cid, "")))
                used |= set(c.ids)
            for cid in decision.wait:
                c = pool.get(cid)
                if c is None:
                    violations.append(f"후보 목록에 없는 그룹({cid})을 기다리기로 함")
                    continue
                if set(c.ids) & used:
                    continue
                if any(self._minutes_to_deadline(by_id[i]) <= URGENT_MIN for i in c.ids):
                    overridden.append(cid)
                    chosen.append((c, "ai", "마감이 임박해 기다리지 않고 제안"))
                    used |= set(c.ids)
                else:
                    waited.append(c)
            mentioned = set(decision.propose) | set(decision.wait)
            for c in plan.groups:
                if c.key not in mentioned and not set(c.ids) & used:
                    chosen.append((c, plan.method, "AI가 판단하지 않은 최적 계획 그룹"))
                    used |= set(c.ids)
        else:
            for c in plan.groups:
                chosen.append((c, plan.method, "최적화 계획" if plan.method == "cp-sat" else "탐욕 매칭"))

        # 실행: 상태가 그대로인지, 규칙을 지키는지 마지막으로 확인하고 그룹을 만든다
        created_saving = 0.0
        grouped = 0
        with self.db.tx():
            for c, method, reason in chosen:
                fresh = [self.db.one("SELECT r.*, u.gender FROM requests r JOIN users u ON u.id = r.user_id "
                                     "WHERE r.id = ?", rid) for rid in c.ids]
                if any(f is None or f["status"] != "waiting" for f in fresh):
                    continue
                problem = matching.group_reject_reason([self._to_match_req(f) for f in fresh], self.cfg)
                if problem:
                    violations.append(f"{c.key}: {problem}")
                    continue
                gid = self._create_group([self.get_request(rid) for rid in c.ids], c.group.stops, status="proposed",
                                         method=method, reason=reason)
                result["created"].append(gid)
                created_saving += c.group.saving_m
                grouped += len(c.ids)
        result["waiting"] = len(rows) - grouped

        ai_info = {"used": bool(decision), "reused": bool(decision and decision.reused),
                   "provider": decision.provider if decision else None, "model": decision.model if decision else None,
                   "summary": decision.summary if decision else "", "waited": [c.key for c in waited],
                   "violations": violations, "overridden": overridden, "error": error}
        result["ai"] = ai_info

        # 판단 이상 경보
        if violations:
            result["alerts"].append(self.raise_alert("ai_violation", "high", "AI가 규칙에 어긋난 제안을 했어요",
                                                     "실행하지 않은 제안: " + "; ".join(violations)))
        if overridden:
            result["alerts"].append(self.raise_alert("ai_overwait", "medium", "AI가 마감 임박 승객을 기다리게 하려 했어요",
                                                     f"마감 {URGENT_MIN}분 이내라 바로 제안했어요: {', '.join(overridden)}"))
        if use_ai and error:
            result["alerts"].append(self.raise_alert("ai_failed", "medium", "AI 응답 실패로 최적화 계획을 그대로 썼어요",
                                                     error))
        if decision and plan.saving_m > 0:
            ai_total = created_saving + sum(c.group.saving_m for c in waited)
            if ai_total < WORSE_RATIO * plan.saving_m:
                result["alerts"].append(self.raise_alert(
                    "ai_worse", "low", "AI 선택이 최적 계획보다 절감액이 작아요",
                    f"최적 계획 {plan.saving_m / 1000:.1f}km 절감, AI 선택 {ai_total / 1000:.1f}km 절감"))
        result["alerts"] = [a for a in result["alerts"] if a]

        self._log_decision("match", bool(decision), decision.provider if decision else "",
                           decision.model if decision else "", plan.method,
                           decision.summary if decision else (f"AI 없이 {plan.method} 계획 실행" if not use_ai else "AI 실패, 계획 실행"),
                           payload or {"optimal_plan": [c.key for c in plan.groups]},
                           decision.raw if decision else {},
                           {"proposed": [[*c.ids] for c, _, _ in chosen], "created": result["created"],
                            "waited": [[*c.ids] for c in waited], "violations": violations, "overridden": overridden,
                            "optimal_saving_m": round(plan.saving_m), "final_saving_m": round(created_saving),
                            "objective": round(plan.objective, 1), "greedy_objective": round(plan.greedy_objective, 1),
                            "candidates": len(plan.candidates), "attempted_ai": use_ai},
                           error)
        if use_ai:
            self._check_fallback_ratio()

    def _agent_payload(self, plan: optimize.PlanResult, pool: dict, by_id: dict) -> dict:
        def member(rid: int) -> dict:
            r = by_id[rid]
            return {"rid": f"R{rid}", "type": r["type"], "minutes_to_deadline": round(self._minutes_to_deadline(r), 1),
                    "rejected_count": len(r["excluded"]), "seats": r["seats"]}

        def info(c: optimize.Candidate) -> dict:
            solo = c.group.solo_sum_m or 1
            return {"id": c.key, "members": [member(i) for i in c.ids], "saving_m": round(c.group.saving_m),
                    "saving_pct": round(100 * c.group.saving_m / solo), "route_km": round(c.group.total_m / 1000, 2)}

        plan_keys = {c.key for c in plan.groups}
        others = sorted((c for k, c in pool.items() if k not in plan_keys), key=lambda c: -c.weight)[:25]
        in_plan = {i for c in plan.groups for i in c.ids}
        return {
            "now": self.now().strftime(FMT),
            "optimal_plan": [info(c) for c in plan.groups],
            "candidates": [info(c) for c in plan.groups] + [info(c) for c in others],
            "unmatched_waiting": [member(i) for i in by_id if i not in in_plan],
            "optimizer": {"method": plan.method, "total_saving_m": round(plan.saving_m)},
        }

    def _check_fallback_ratio(self) -> None:
        since = (self.ts() - timedelta(minutes=FALLBACK_WINDOW_MIN)).strftime(STAMP)
        rows = self.db.all("SELECT used_ai, result FROM ai_decisions WHERE kind = 'match' AND created_at >= ?", since)
        tried = [r for r in rows if r["result"].get("attempted_ai")]
        if len(tried) >= 4:
            ratio = sum(1 for r in tried if not r["used_ai"]) / len(tried)
            if ratio > FALLBACK_ALERT_RATIO:
                self.raise_alert("ai_fallback_ratio", "high", "AI가 계속 실패해 대체 매칭이 늘고 있어요",
                                 f"최근 {FALLBACK_WINDOW_MIN}분 매칭 {len(tried)}번 중 {ratio:.0%}가 AI 없이 처리됐어요")

    def _log_decision(self, kind: str, used_ai: bool, provider: str, model: str, method: str, summary: str,
                      payload: dict, output: dict, result: dict, error: str | None) -> int:
        with self.db.tx() as c:
            cur = c.execute("INSERT INTO ai_decisions (kind, used_ai, provider, model, method, summary, payload, output, "
                            "result, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (kind, int(used_ai), provider or "", model or "", method or "", summary or "",
                             dumps(payload), dumps(output), dumps(result), error, self._stamp()))
        return cur.lastrowid

    def _to_match_req(self, r: dict) -> matching.Req:
        return matching.Req(
            id=r["id"], user_id=r["user_id"], gender=r["gender"],
            origin=Point.from_dict(r["origin"]), dest=Point.from_dict(r["dest"]),
            window_start=parse_dt(r["window_start"]), window_end=parse_dt(r["window_end"]),
            seats=r["seats"], detour_ratio=r["detour_ratio"], excluded=set(r["excluded"]))

    def _create_group(self, members: list[dict], stops: list[Stop], status: str, is_solo: bool = False,
                      method: str = "", reason: str = "") -> int:
        if len({m["gender"] for m in members}) > 1:  # 동성 매칭 마지막 안전장치
            self.raise_alert("gender_guard", "high", "다른 성별을 한 그룹으로 묶으려는 시도를 막았어요",
                             f"요청 {[m['id'] for m in members]}")
            raise ServiceError("같은 성별끼리만 동승할 수 있습니다", 409)
        by_id = {m["id"]: m for m in members}
        points = [Point.from_dict(by_id[s.request_id]["origin" if s.kind == "pickup" else "dest"]) for s in stops]
        route = self.provider.route(points)
        departure = max(max(parse_dt(m["window_start"]) for m in members), self.now())
        ids = [m["id"] for m in members]
        stamp = self._stamp()
        demo = set()
        for m in members:
            u = self.db.one("SELECT is_demo, email FROM users WHERE id = ?", m["user_id"])
            if u["is_demo"] and u["email"] is None:
                demo.add(m["id"])
        # 로그인하지 않는 데모 동승자는 제안을 자동 수락하고 안내도 확인한다(혼자서도 전체 흐름을 시연할 수 있게)
        accepted = ids if is_solo else [i for i in ids if i in demo]
        if status == "proposed" and set(accepted) == set(ids):
            status = "confirmed"
        expires = None
        if status == "proposed":
            quick = any(m["type"] == "quick" for m in members)
            ttl = timedelta(seconds=self.cfg.quick_proposal_sec) if quick else timedelta(minutes=self.cfg.reserved_proposal_min)
            expires = (self.ts() + ttl).strftime(STAMP)
        with self.db.tx() as c:
            cur = c.execute(
                """INSERT INTO groups (status, is_solo, member_ids, accepted, stops, legs_m, total_m, duration_s,
                   est_fare, est_toll, polyline, provider, departure, expires_at, confirmed_at, match_method,
                   match_reason, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (status, int(is_solo), dumps(ids), dumps(accepted),
                 dumps([{"request_id": s.request_id, "kind": s.kind} for s in stops]),
                 dumps(route.legs_m), route.total_m, route.duration_s, route.taxi_fare, route.toll,
                 dumps(route.polyline), route.provider, departure.strftime(FMT), expires,
                 stamp if status == "confirmed" else None, method, reason[:200], stamp, stamp))
            gid = cur.lastrowid
            req_status = "confirmed" if status == "confirmed" else "proposed"
            note = "동승자를 찾지 못해 단독 탑승으로 전환" if is_solo else ""
            for rid in ids:
                c.execute("UPDATE requests SET status = ?, group_id = ?, note = ? WHERE id = ?",
                          (req_status, gid, note, rid))
                if rid in demo:
                    c.execute("UPDATE requests SET notice_ack_at = ? WHERE id = ?", (stamp, rid))
            if not is_solo:   # 자동 수락한 데모 동승자도 예약금을 잡아 둔다
                for rid in ids:
                    if rid in demo:
                        self._hold_deposit(c, gid, rid)
        return gid

    # -- 예약금 ------------------------------------------------------------
    def _hold_deposit(self, c, group_id: int, request_id: int) -> int:
        """수락할 때 예상 몫 × DEPOSIT_RATIO만큼 포인트를 잡아 두고, 그때의 몫과 추가 부담 비율을 기록한다."""
        r = self.get_request(request_id)
        if r["hold_amount"]:
            return r["hold_amount"]
        view = self.group_view(group_id)
        me = next(m for m in view["members"] if m["request_id"] == request_id)
        share = int(me["share"])
        hold = int(math.ceil(share * self.cfg.deposit_ratio / 10.0) * 10)
        points = c.execute("SELECT points FROM users WHERE id = ?", (r["user_id"],)).fetchone()[0]
        if points < hold:
            raise ServiceError(f"포인트가 부족해요. 예약금 {hold:,}P가 필요한데 {points:,}P 있어요", 402)
        self._wallet(c, r["user_id"], -hold, "hold", "동승 예약금", group_id, request_id)
        c.execute("UPDATE requests SET hold_amount = ?, base_share = ?, surcharge_rate = ? WHERE id = ?",
                  (hold, share, self._surcharge_rate(r), request_id))
        return hold

    def _release_hold(self, c, request_id: int, note: str = "예약금 돌려받음") -> int:
        row = c.execute("SELECT user_id, hold_amount, group_id FROM requests WHERE id = ?", (request_id,)).fetchone()
        if not row or not row[1]:
            return 0
        self._wallet(c, row[0], row[1], "release", note, row[2], request_id)
        c.execute("UPDATE requests SET hold_amount = 0 WHERE id = ?", (request_id,))
        return row[1]

    def _compensate(self, c, leaver: dict, group_id: int, why: str) -> int:
        """빠진 사람 때문에 남은 사람 몫이 오른 만큼을 빠진 사람의 예약금에서 보상한다(예약금 한도 안에서).
        남은 사람은 수락할 때의 몫(base_share) 그대로 내는 셈이 된다."""
        view = self.group_view(group_id)
        budget = int(leaver.get("hold_amount") or 0)
        paid = 0
        for m in view["members"]:
            req = self.get_request(m["request_id"])
            if req["base_share"] is None:
                continue
            need = max(0, int(m["share"]) - int(req["base_share"]))
            give = min(need, budget - paid)
            if give <= 0:
                continue
            self._wallet(c, req["user_id"], give, "comp_in", f"{why} 보상", group_id, req["id"])
            paid += give
        if paid:
            self._release_hold(c, leaver["id"], "예약금 정산")
            self._wallet(c, leaver["user_id"], -paid, "comp_out", f"{why}로 동승자 보상", group_id, leaver["id"])
        return paid

    def _dissolve(self, group_id: int, rejecters: list[int] | None = None, reason: str = "") -> None:
        g = self.get_group_row(group_id)
        rejecters = rejecters or []
        with self.db.tx() as c:
            c.execute("UPDATE groups SET status = 'cancelled', updated_at = ? WHERE id = ?", (self._stamp(), group_id))
            for rid in g["member_ids"]:
                r = self.get_request(rid)
                self._release_hold(c, rid, "제안이 취소돼 예약금 돌려받음")
                if r["status"] not in ("proposed", "confirmed"):
                    continue
                excluded = set(r["excluded"])
                if rid in rejecters:
                    excluded |= {x for x in g["member_ids"] if x != rid}
                else:
                    excluded |= set(rejecters)
                c.execute("UPDATE requests SET status = 'waiting', group_id = NULL, excluded = ?, note = ? WHERE id = ?",
                          (dumps(sorted(excluded)), reason, rid))

    def _reroute_without(self, c, g: dict, leaver_id: int) -> list[int]:
        """한 사람을 빼고 같은 순서로 경로를 다시 잡는다. 지나온 정차 지점은 바뀌지 않는다."""
        remaining = [rid for rid in g["member_ids"] if rid != leaver_id]
        stops = [Stop(s["request_id"], s["kind"]) for s in g["stops"] if s["request_id"] != leaver_id]
        members = {rid: self.get_request(rid) for rid in remaining}
        points = [Point.from_dict(members[s.request_id]["origin" if s.kind == "pickup" else "dest"]) for s in stops]
        route = self.provider.route(points)
        c.execute("""UPDATE groups SET member_ids = ?, accepted = ?, stops = ?, legs_m = ?, total_m = ?,
                     duration_s = ?, est_fare = ?, est_toll = ?, polyline = ?, updated_at = ? WHERE id = ?""",
                  (dumps(remaining), dumps(remaining), dumps([{"request_id": s.request_id, "kind": s.kind} for s in stops]),
                   dumps(route.legs_m), route.total_m, route.duration_s, route.taxi_fare, route.toll,
                   dumps(route.polyline), self._stamp(), g["id"]))
        return remaining

    def _leave_confirmed_group(self, r: dict) -> None:
        g = self.get_group_row(r["group_id"])
        now = self.now()
        with self.db.tx() as c:
            # 출발 30분 전 이후 취소: 패널티(신뢰도 감점) + 남은 사람 보상
            late = now >= parse_dt(g["departure"]) - timedelta(minutes=self.cfg.late_cancel_min) and not g["is_solo"]
            if late:
                c.execute("UPDATE users SET penalty = penalty + 1 WHERE id = ?", (r["user_id"],))
                self._trust(c, r["user_id"], self.cfg.trust_late_cancel, "late_cancel", "출발 30분 이내 취소", g["id"])
            remaining = [rid for rid in g["member_ids"] if rid != r["id"]]
            if g["is_solo"] or len(remaining) < 2:
                c.execute("UPDATE groups SET status = 'cancelled', offer_driver_id = NULL, updated_at = ? WHERE id = ?",
                          (self._stamp(), g["id"]))
                for rid in remaining:
                    self._release_hold(c, rid, "동승자가 취소해 예약금 돌려받음")
                    c.execute("UPDATE requests SET status = 'waiting', group_id = NULL, notice_ack_at = NULL, base_share = NULL, "
                              "note = ? WHERE id = ?", ("동승자가 취소해 다시 매칭을 기다려요. 혼자 탈지 다시 고를 수 있어요", rid))
                self._release_hold(c, r["id"], "취소해 예약금 돌려받음")
                return
            self._reroute_without(c, g, r["id"])
            paid = self._compensate(c, self.get_request(r["id"]), g["id"], "동승자 직전 취소") if late else 0
            self._release_hold(c, r["id"], "취소해 예약금 돌려받음")
            msg = (f"동승자 한 명이 직전에 취소했어요. 늘어난 요금은 그 사람 예약금에서 보상해 드려요"
                   if paid else "동승자 한 명이 취소해 경로와 요금을 다시 계산했어요")
            for rid in remaining:
                c.execute("UPDATE requests SET note = ? WHERE id = ?", (msg, rid))

    # =====================================================================
    # 제안 수락/거절 · 탑승 전 안내 · 송금 완료 표시
    # =====================================================================
    def accept(self, group_id: int, request_id: int) -> dict:
        confirmed = False
        with self.db.tx() as c:
            g = self.get_group_row(group_id)
            if g["status"] != "proposed":
                raise ServiceError("수락할 수 있는 제안이 아닙니다")
            if request_id not in g["member_ids"]:
                raise ServiceError("이 그룹의 동승자가 아닙니다", 403)
            self._hold_deposit(c, group_id, request_id)   # 포인트가 모자라면 여기서 멈춘다
            accepted = sorted(set(g["accepted"]) | {request_id})
            c.execute("UPDATE groups SET accepted = ?, updated_at = ? WHERE id = ?", (dumps(accepted), self._stamp(), group_id))
            if set(accepted) == set(g["member_ids"]):
                confirmed = True
                c.execute("UPDATE groups SET status = 'confirmed', confirmed_at = ? WHERE id = ?", (self._stamp(), group_id))
                for rid in g["member_ids"]:
                    c.execute("UPDATE requests SET status = 'confirmed', note = '' WHERE id = ?", (rid,))
        if confirmed:
            self.dispatch_cycle()
        return self.group_view(group_id)

    def reject(self, group_id: int, request_id: int) -> dict:
        with self.db.tx():
            g = self.get_group_row(group_id)
            if g["status"] != "proposed":
                raise ServiceError("거절할 수 있는 제안이 아닙니다")
            if request_id not in g["member_ids"]:
                raise ServiceError("이 그룹의 동승자가 아닙니다", 403)
            self._dissolve(group_id, rejecters=[request_id], reason="제안이 거절되어 다시 매칭을 기다립니다")
        return self.get_request(request_id)

    def ack_notice(self, user: dict, request_id: int) -> dict:
        r = self.own_request(user, request_id)
        if r["status"] not in ("confirmed", "riding") or not r["group_id"]:
            raise ServiceError("동승이 확정된 뒤에 안내를 확인할 수 있어요")
        with self.db.tx() as c:
            c.execute("UPDATE requests SET notice_ack_at = COALESCE(notice_ack_at, ?) WHERE id = ?",
                      (self._stamp(), request_id))
        return self.get_request(request_id)

    def mark_paid(self, user: dict, request_id: int) -> dict:
        """예전 '송금 완료' 표시. 이제 운행이 끝나면 포인트로 자동 결제되므로 이미 결제된 요청을 돌려준다."""
        r = self.own_request(user, request_id)
        if r["status"] != "done":
            raise ServiceError("정산이 끝난 뒤에 결제돼요")
        with self.db.tx() as c:
            c.execute("UPDATE requests SET paid_at = COALESCE(paid_at, ?) WHERE id = ?", (self._stamp(), request_id))
        return self.get_request(request_id)

    # =====================================================================
    # 배차: OR-Tools 배정 → 기사님 수락/거절 → 운행
    # =====================================================================
    def get_driver(self, user_id: int) -> dict:
        d = self.db.one("SELECT d.*, u.email, u.login_id, u.nickname FROM drivers d JOIN users u ON u.id = d.user_id "
                        "WHERE d.user_id = ?",
                        user_id)
        if not d:
            raise ServiceError("기사님 정보를 찾을 수 없습니다", 404)
        d["available"] = bool(d["available"])
        return d

    def set_available(self, driver: dict, available: bool, lat: float | None = None, lng: float | None = None) -> dict:
        with self.db.tx() as c:
            c.execute("UPDATE drivers SET available = ?, updated_at = ? WHERE user_id = ?",
                      (int(available), self._stamp(), driver["id"]))
            if lat is not None and lng is not None:
                c.execute("UPDATE drivers SET lat = ?, lng = ? WHERE user_id = ?", (lat, lng, driver["id"]))
            if not available:  # 운행 가능을 끄면 받고 있던 제안은 돌려보낸다
                for g in self.db.all("SELECT id, declined_drivers FROM groups WHERE offer_driver_id = ? AND status = 'confirmed'",
                                     driver["id"]):
                    c.execute("UPDATE groups SET offer_driver_id = NULL, offer_expires_at = NULL, declined_drivers = ? "
                              "WHERE id = ?", (dumps(sorted(set(g["declined_drivers"]) | {driver["id"]})), g["id"]))
        self.dispatch_cycle()
        return self.get_driver(driver["id"])

    def update_location(self, driver: dict, lat: float, lng: float) -> dict:
        with self.db.tx() as c:
            c.execute("UPDATE drivers SET lat = ?, lng = ?, updated_at = ? WHERE user_id = ?",
                      (lat, lng, self._stamp(), driver["id"]))
        return self.get_driver(driver["id"])

    def _is_reservation(self, g: dict) -> bool:
        """출발이 배차 시작 시점(DISPATCH_LEAD_MIN분 전)보다 뒤면 미리 잡는 예약 배차다."""
        return parse_dt(g["departure"]) > self.now() + timedelta(minutes=self.cfg.dispatch_lead_min)

    def dispatch_cycle(self) -> dict:
        """만료된 배차 제안 정리 → 배차가 필요한 그룹과 운행 가능한 기사님을 최적으로 짝지어 제안한다.

        곧 출발하는 그룹은 바로 운행할 기사님에게, 출발이 먼 예약 그룹은 '예약 배차'로 미리 제안한다.
        예약을 받아 둔 기사님도 출발 시각이 가까워지기 전까지는 다른 배차를 받을 수 있다.
        """
        ts = self.ts()
        out = {"offers": [], "expired": [], "method": None, "ai": None}
        lead_dt = self.now() + timedelta(minutes=self.cfg.dispatch_lead_min)
        if not self._dispatch_lock.acquire(blocking=False):   # 다른 배차 판단이 진행 중이면 다음 차례에
            out["skipped"] = True
            return out
        try:
            plan = self._dispatch_plan(out, ts, lead_dt)            # 1) OR-Tools 배정(짧은 DB 잠금)
            if plan:
                chosen = self._dispatch_judge(plan, out)            # 2) AI 판단(DB 잠금 밖)
                self._dispatch_write(plan, chosen, out, ts, lead_dt)   # 3) 다시 확인하고 제안 쓰기
        finally:
            self._dispatch_lock.release()
        self._dispatch_alerts(ts)
        return out

    def _dispatch_plan(self, out: dict, ts: datetime, lead_dt: datetime) -> dict | None:
        with self.db.tx() as c:
            for g in self.db.all("SELECT * FROM groups WHERE status = 'confirmed' AND offer_driver_id IS NOT NULL"):
                if parse_ts(g["offer_expires_at"]) <= ts:
                    c.execute("UPDATE groups SET offer_driver_id = NULL, offer_expires_at = NULL, declined_drivers = ? "
                              "WHERE id = ?", (dumps(sorted(set(g["declined_drivers"]) | {g["offer_driver_id"]})), g["id"]))
                    out["expired"].append(g["id"])
            need = self.db.all("SELECT * FROM groups WHERE status = 'confirmed' AND driver_id IS NULL "
                               "AND offer_driver_id IS NULL ORDER BY departure, id")
            busy = {r["driver_id"] for r in self.db.all("SELECT driver_id FROM groups WHERE status = 'riding' AND driver_id IS NOT NULL")}
            busy |= {r["offer_driver_id"] for r in self.db.all("SELECT offer_driver_id FROM groups WHERE status = 'confirmed' "
                                                                  "AND offer_driver_id IS NOT NULL")}
            drivers = [d for d in self.db.all("SELECT * FROM drivers WHERE available = 1") if d["user_id"] not in busy]
            # 기사님마다 받아 둔 예약(출발 시각)
            booked: dict[int, list[datetime]] = {}
            for r in self.db.all("SELECT driver_id, departure FROM groups WHERE status = 'confirmed' AND driver_id IS NOT NULL"):
                booked.setdefault(r["driver_id"], []).append(parse_dt(r["departure"]))
            if need and drivers:
                slots = []
                # 운행 가능한 기사님이 모두 거절(또는 무응답)한 그룹은 3분 뒤 거절 기록을 지우고 다시 차례로 제안한다.
                # 그러지 않으면 기사님이 적을 때 그 그룹은 영영 배차를 못 받는다
                avail = {d["user_id"] for d in drivers}
                for g in need:
                    if avail and avail <= set(g["declined_drivers"]):
                        since = self._all_declined_since.setdefault(g["id"], ts)
                        if ts - since >= timedelta(minutes=3):
                            g["declined_drivers"] = []
                            c.execute("UPDATE groups SET declined_drivers = '[]' WHERE id = ?", (g["id"],))
                            self._all_declined_since.pop(g["id"], None)
                    else:
                        self._all_declined_since.pop(g["id"], None)
                banned = {(d, g["id"]) for g in need for d in g["declined_drivers"]}
                for g in need:
                    first = g["stops"][0]
                    r = self.get_request(first["request_id"])
                    dep = parse_dt(g["departure"])
                    mins = (dep - self.now()).total_seconds() / 60
                    reserve = dep > lead_dt
                    slots.append(optimize.GroupSlot(g["id"], Point.from_dict(r["origin"]),
                                                    urgency=0.0 if reserve else max(0.0, min(1.0, 1 - mins / max(1, self.cfg.dispatch_lead_min)))))
                    for d in drivers:
                        times = booked.get(d["user_id"], [])
                        if reserve:   # 이미 받은 예약과 1시간 안에 겹치면 보내지 않는다
                            if any(abs((dep - t).total_seconds()) < 3600 for t in times):
                                banned.add((d["user_id"], g["id"]))
                        elif any(t <= lead_dt for t in times):  # 곧 시작할 예약이 있는 기사님은 지금 운행을 못 받는다
                            banned.add((d["user_id"], g["id"]))
                pairs, method = optimize.assign_drivers([optimize.DriverSlot(d["user_id"], Point(d["lat"], d["lng"]))
                                                         for d in drivers], slots, banned,
                                                        max_pickup_m=60000)
                out["method"] = method
                if not pairs:
                    return None
                return {"need": need, "drivers": drivers, "slots": slots, "banned": banned, "pairs": pairs, "booked": booked}
        return None

    def _rule_reason(self, g: dict, pickup_m: float, lead_dt: datetime) -> str:
        dep = parse_dt(g["departure"])
        if dep > lead_dt:
            return (f"예약 배차예요. {dep.month}/{dep.day} {dep:%H:%M} 출발, 첫 탑승 지점은 지금 위치에서 약 "
                    f"{pickup_m / 1000:.1f}km예요. 승객 {len(g['member_ids'])}명, 총 {g['total_m'] / 1000:.1f}km, "
                    f"예상 {g['est_fare']:,}원이에요.")
        return (f"픽업 지점까지 약 {pickup_m / 1000:.1f}km로 지금 운행 가능한 기사님 중 배정이 가장 효율적이에요. "
                f"승객 {len(g['member_ids'])}명, 총 {g['total_m'] / 1000:.1f}km, 예상 {g['est_fare']:,}원이에요.")

    def _dispatch_judge(self, plan: dict, out: dict) -> dict[int, dict]:
        """OR-Tools 배정을 기본으로, 고를 여지가 있으면 AI(배차 순서: AI_ORDER_DISPATCH)가 기사님을 고른다.
        돌려주는 값: {group_id: {"driver_id", "pickup_m", "reason"(없으면 규칙 문장), "source"}}"""
        need = {g["id"]: g for g in plan["need"]}
        slot = {s.group_id: s for s in plan["slots"]}
        drivers = {d["user_id"]: d for d in plan["drivers"]}
        optimal = {a.group_id: {"driver_id": a.driver_id, "pickup_m": a.pickup_m, "reason": None, "source": "rule"}
                   for a in plan["pairs"]}
        # 그룹마다 후보 기사님 최대 3명(가까운 순, 거절·예약 겹침 제외)
        cands: dict[int, list[tuple[int, float]]] = {}
        for gid in optimal:
            pts = [(did, haversine_m(Point(d["lat"], d["lng"]), slot[gid].pickup)) for did, d in drivers.items()
                   if (did, gid) not in plan["banned"]]
            cands[gid] = sorted(pts, key=lambda x: x[1])[:3]
        if not self.router.enabled or not any(len(v) > 1 for v in cands.values()):
            return optimal    # 고를 게 없으면 AI를 부르지 않는다

        did_code = {did: f"D{i + 1}" for i, did in enumerate(drivers)}
        code_did = {v: k for k, v in did_code.items()}
        payload = {"now": self.now().strftime(FMT), "drivers": [self._driver_features(did, plan["booked"]) | {"id": did_code[did]}
                                                                for did in drivers],
                   "groups": [], "optimal": []}
        for gid, g in need.items():
            if gid not in optimal:
                continue
            dep = parse_dt(g["departure"])
            last = g["stops"][-1]
            last_req = self.get_request(last["request_id"])
            drop = Point.from_dict(last_req["dest"])
            payload["groups"].append({
                "id": f"G{gid}", "passengers": len(g["member_ids"]), "route_km": round(g["total_m"] / 1000, 1),
                "est_fare": g["est_fare"], "minutes_to_departure": round((dep - self.now()).total_seconds() / 60),
                "candidates": [{"driver": did_code[did], "pickup_km": round(m / 1000, 2),
                                "drop_to_next_reservation_km": self._drop_to_next(did, drop, plan["booked"])}
                               for did, m in cands[gid]]})
            payload["optimal"].append({"group": f"G{gid}", "driver": did_code[optimal[gid]["driver_id"]],
                                       "pickup_km": round(optimal[gid]["pickup_m"] / 1000, 2)})
        try:
            ai = self.dispatch_agent.judge(payload)
        except (AllProvidersFailed, KeyError, TypeError, ValueError) as exc:
            err = str(exc)[:300]
            out["ai"] = {"used": False, "error": err}
            self.raise_alert("ai_dispatch_failed", "medium", "AI 배차 판단 실패로 최적화 배정을 그대로 썼어요", err)
            self._log_decision("dispatch", False, "", "", "judge", "AI 실패, 최적화 배정 사용", payload, {},
                               {"chosen": {str(k): v["driver_id"] for k, v in optimal.items()}}, err)
            return optimal

        chosen: dict[int, dict] = {}
        used: set[int] = set()
        violations, too_far = [], []
        for item in ai.data.get("assignments", []):
            gcode, dcode = str(item.get("group", "")), str(item.get("driver", ""))
            gid = int(gcode[1:]) if gcode[1:].isdigit() else None
            did = code_did.get(dcode)
            if gid not in optimal or did is None or did not in [x[0] for x in cands[gid]]:
                violations.append(f"{gcode}→{dcode}: 후보 밖")
                continue
            if did in used or gid in chosen:
                violations.append(f"{gcode}→{dcode}: 겹침")
                continue
            pickup = dict(cands[gid])[did]
            opt_m = optimal[gid]["pickup_m"]
            if pickup > opt_m + max(self.cfg.dispatch_max_extra_m, self.cfg.dispatch_max_extra_ratio * opt_m):
                too_far.append(f"{gcode}: {dcode} {pickup / 1000:.1f}km (최적 {opt_m / 1000:.1f}km)")
                continue
            chosen[gid] = {"driver_id": did, "pickup_m": pickup, "reason": str(item.get("reason", ""))[:240] or None,
                           "source": ai.provider}
            used.add(did)
        for gid, o in optimal.items():          # AI가 빠뜨렸거나 되돌린 그룹은 최적 배정으로
            if gid not in chosen and o["driver_id"] not in used:
                chosen[gid] = dict(o)
                used.add(o["driver_id"])
        if violations:
            self.raise_alert("ai_dispatch_violation", "high", "AI가 후보 밖이거나 겹치는 배차를 골랐어요",
                             "실행하지 않음: " + "; ".join(violations))
        if too_far:
            self.raise_alert("ai_dispatch_far", "medium", "AI 배차가 너무 멀어 최적화 배정으로 되돌렸어요", "; ".join(too_far))
        out["ai"] = {"used": True, "provider": ai.provider, "model": ai.model, "violations": violations, "too_far": too_far}
        self._log_decision("dispatch", True, ai.provider, ai.model, "judge", str(ai.data.get("summary", ""))[:300], payload,
                           ai.data, {"chosen": {str(k): v["driver_id"] for k, v in chosen.items()},
                                     "optimal": {str(k): v["driver_id"] for k, v in optimal.items()},
                                     "violations": violations, "too_far": too_far}, None)
        return chosen

    def _driver_features(self, did: int, booked: dict) -> dict:
        today = self.now().strftime("%Y-%m-%d")
        done = self.db.all("SELECT final_fare, updated_at FROM groups WHERE driver_id = ? AND status = 'completed' "
                           "AND updated_at >= ? ORDER BY updated_at DESC", did, today)
        idle = round((self.ts() - parse_ts(done[0]["updated_at"])).total_seconds() / 60) if done else None
        future = sorted(t for t in booked.get(did, []) if t > self.now())
        return {"today_trips": len(done), "today_fare": sum(g["final_fare"] or 0 for g in done),
                "idle_min": idle, "next_reservation_min": round((future[0] - self.now()).total_seconds() / 60) if future else None}

    def _drop_to_next(self, did: int, drop: Point, booked: dict) -> float | None:
        g = self.db.one("SELECT stops FROM groups WHERE driver_id = ? AND status = 'confirmed' ORDER BY departure LIMIT 1", did)
        if not g:
            return None
        first = self.get_request(g["stops"][0]["request_id"])
        return round(haversine_m(drop, Point.from_dict(first["origin"])) / 1000, 2)

    def _dispatch_write(self, plan: dict, chosen: dict[int, dict], out: dict, ts: datetime, lead_dt: datetime) -> None:
        need = {g["id"]: g for g in plan["need"]}
        with self.db.tx() as c:
            busy = {r["driver_id"] for r in self.db.all("SELECT driver_id FROM groups WHERE status = 'riding' AND driver_id IS NOT NULL")}
            busy |= {r["offer_driver_id"] for r in self.db.all("SELECT offer_driver_id FROM groups WHERE status = 'confirmed' "
                                                                  "AND offer_driver_id IS NOT NULL")}
            for gid, ch in chosen.items():
                g = self.db.one("SELECT * FROM groups WHERE id = ?", gid)
                d = self.db.one("SELECT available FROM drivers WHERE user_id = ?", ch["driver_id"])
                if (not g or g["status"] != "confirmed" or g["driver_id"] or g["offer_driver_id"]
                        or not d or not d["available"] or ch["driver_id"] in busy):
                    continue   # 그사이 상태가 바뀌었다
                dep = parse_dt(g["departure"])
                reason = ch["reason"] or self._rule_reason(need[gid], ch["pickup_m"], lead_dt)
                until = (ts + timedelta(seconds=self.cfg.dispatch_offer_sec if dep <= lead_dt
                                        else max(self.cfg.dispatch_offer_sec, 120))).strftime(STAMP)  # 예약은 2분 여유
                c.execute("UPDATE groups SET offer_driver_id = ?, offer_expires_at = ?, offer_reason = ?, offer_source = ?, "
                          "updated_at = ? WHERE id = ?", (ch["driver_id"], until, reason, ch["source"], self._stamp(), gid))
                busy.add(ch["driver_id"])
                out["offers"].append({"group_id": gid, "driver_id": ch["driver_id"], "pickup_m": round(ch["pickup_m"]),
                                      "reservation": dep > lead_dt, "source": ch["source"]})

    def _dispatch_alerts(self, ts: datetime) -> None:
        # 배차 지연 경보
        limit = (ts - timedelta(minutes=self.cfg.dispatch_delay_alert_min)).strftime(STAMP)
        for g in self.db.all("SELECT id, confirmed_at FROM groups WHERE status = 'confirmed' AND driver_id IS NULL "
                             "AND offer_driver_id IS NULL AND confirmed_at IS NOT NULL AND confirmed_at <= ? AND departure <= ?",
                             limit, (self.now() + timedelta(minutes=self.cfg.dispatch_lead_min)).strftime(FMT)):
            self.raise_alert("dispatch_delay", "medium", "배차를 기다리는 그룹이 있어요",
                             f"그룹 #{g['id']}가 {self.cfg.dispatch_delay_alert_min}분 넘게 기사님을 찾지 못했어요. "
                             "운행 가능한 기사님이 있는지 확인하세요", group_id=g["id"])
        # 예약 배차를 받은 기사님이 출발 시각이 지나도록 운행을 시작하지 않으면 경보
        for g in self.db.all("SELECT id, driver_id, departure FROM groups WHERE status = 'confirmed' AND driver_id IS NOT NULL "
                             "AND departure < ?", self.now().strftime(FMT)):
            self.raise_alert("reservation_not_started", "medium", "예약 운행이 아직 시작되지 않았어요",
                             f"그룹 #{g['id']}: {g['departure'][11:]} 출발 예정, 기사님 #{g['driver_id']}에게 확인하세요",
                             group_id=g["id"])

    def _offer_for(self, driver_id: int) -> dict | None:
        return self.db.one("SELECT * FROM groups WHERE status = 'confirmed' AND offer_driver_id = ?", driver_id)

    def _explain_offer(self, g: dict, driver: dict) -> None:
        """규칙 문장으로 만든 배차 이유를 AI가 기사님용 문장으로 다시 쓴다(한 번만 시도)."""
        if g["offer_source"] != "rule" or not self.router.enabled:
            return
        first = self.get_request(g["stops"][0]["request_id"])
        pickup_m = haversine_m(Point(driver["lat"], driver["lng"]), Point.from_dict(first["origin"]))
        payload = {"pickup_km": round(pickup_m / 1000, 2), "passengers": len(g["member_ids"]),
                   "stops": len(g["stops"]), "route_km": round(g["total_m"] / 1000, 2), "est_fare": g["est_fare"],
                   "departure": g["departure"], "rule_reason": g["offer_reason"]}
        try:
            ai = self.dispatch_agent.explain(payload)
            reason, source, err = str(ai.data["reason"])[:240], ai.provider, None
        except (AllProvidersFailed, KeyError, TypeError) as exc:
            reason, source, err = g["offer_reason"], "rule:failed", str(exc)[:300]
        with self.db.tx() as c:
            c.execute("UPDATE groups SET offer_reason = ?, offer_source = ? WHERE id = ? AND offer_driver_id = ?",
                      (reason, source, g["id"], driver["user_id"]))
        self._log_decision("dispatch", err is None, source if err is None else "", ai.model if err is None else "",
                           "assign", reason, payload, {"reason": reason}, {"group_id": g["id"], "driver_id": driver["user_id"]},
                           err)

    def driver_state(self, driver_user: dict) -> dict:
        d = self.get_driver(driver_user["id"])
        offer = self._offer_for(d["user_id"])
        expired = bool(offer and parse_ts(offer["offer_expires_at"]) <= self.ts())
        if (d["available"] and (not offer or expired) and not self.db.one(
                "SELECT id FROM groups WHERE driver_id = ? AND status = 'riding'", d["user_id"])) or expired:
            self.dispatch_cycle()   # 주기 작업(30초)을 기다리지 않고 바로 배차를 확인한다(시간 지난 제안도 정리)
            offer = self._offer_for(d["user_id"])
        if offer:
            self._explain_offer(offer, d)
            offer = self._offer_for(d["user_id"])
        trip = self.db.one("SELECT id FROM groups WHERE driver_id = ? AND status = 'riding' ORDER BY id DESC LIMIT 1",
                           d["user_id"])
        today = self.now().strftime("%Y-%m-%d")
        done = self.db.all("SELECT id, final_fare FROM groups WHERE driver_id = ? AND status = 'completed' "
                           "AND updated_at >= ? ORDER BY id DESC", d["user_id"], today)
        last = self.db.one("SELECT id FROM groups WHERE driver_id = ? AND status = 'completed' ORDER BY updated_at DESC LIMIT 1",
                           d["user_id"])
        lead = self.now() + timedelta(minutes=self.cfg.dispatch_lead_min)
        reservations = [{**self.group_view(r["id"], driver_view=True), "startable": parse_dt(r["departure"]) <= lead}
                        for r in self.db.all("SELECT id, departure FROM groups WHERE driver_id = ? AND status = 'confirmed' "
                                             "ORDER BY departure", d["user_id"])]
        return {
            "driver": d,
            "reservations": reservations,
            "offer": {**self.group_view(offer["id"], driver_view=True), "offer_expires_at": offer["offer_expires_at"],
                      "offer_reason": offer["offer_reason"], "offer_source": offer["offer_source"]} if offer else None,
            "trip": self.group_view(trip["id"], driver_view=True) if trip else None,
            "last_completed": self.group_view(last["id"], driver_view=True) if last else None,
            "today": {"trips": len(done), "fare": sum(g["final_fare"] or 0 for g in done)},
            "now": self._stamp(),
        }

    def _driver_group(self, driver: dict, group_id: int) -> dict:
        g = self.get_group_row(group_id)
        if g["driver_id"] != driver["id"]:
            raise ServiceError("내가 맡은 운행이 아니에요", 403)
        return g

    def accept_offer(self, driver: dict, group_id: int) -> dict:
        with self.db.tx() as c:
            g = self.get_group_row(group_id)
            if g["status"] != "confirmed" or g["offer_driver_id"] != driver["id"]:
                raise ServiceError("수락할 수 있는 배차 요청이 아니에요")
            if parse_ts(g["offer_expires_at"]) <= self.ts():
                raise ServiceError("수락 시간이 지났어요", 409)
            if self._is_reservation(g):
                # 예약 배차: 기사님만 정해 두고, 출발 때 '운행 시작'으로 운행을 시작한다
                c.execute("UPDATE groups SET driver_id = ?, offer_driver_id = NULL, offer_expires_at = NULL, updated_at = ? "
                          "WHERE id = ?", (driver["id"], self._stamp(), group_id))
            else:
                if self.db.one("SELECT id FROM groups WHERE driver_id = ? AND status = 'riding'", driver["id"]):
                    raise ServiceError("진행 중인 운행을 먼저 끝내세요", 409)
                c.execute("UPDATE groups SET status = 'riding', driver_id = ?, offer_driver_id = NULL, offer_expires_at = NULL, "
                          "progress = -1, odometer = '[]', updated_at = ? WHERE id = ?", (driver["id"], self._stamp(), group_id))
        return self.group_view(group_id, driver_view=True)

    def start_trip(self, driver: dict, group_id: int) -> dict:
        """예약 배차로 받아 둔 그룹의 운행을 시작한다(첫 탑승 지점으로 출발)."""
        with self.db.tx() as c:
            g = self._driver_group(driver, group_id)
            if g["status"] != "confirmed":
                raise ServiceError("시작할 수 있는 예약 운행이 아니에요")
            if self.db.one("SELECT id FROM groups WHERE driver_id = ? AND status = 'riding'", driver["id"]):
                raise ServiceError("진행 중인 운행을 먼저 끝내세요", 409)
            c.execute("UPDATE groups SET status = 'riding', progress = -1, odometer = '[]', updated_at = ? WHERE id = ?",
                      (self._stamp(), group_id))
        return self.group_view(group_id, driver_view=True)

    def decline_offer(self, driver: dict, group_id: int) -> dict:
        with self.db.tx() as c:
            g = self.get_group_row(group_id)
            if g["offer_driver_id"] != driver["id"]:
                raise ServiceError("거절할 수 있는 배차 요청이 아니에요")
            c.execute("UPDATE groups SET offer_driver_id = NULL, offer_expires_at = NULL, declined_drivers = ? WHERE id = ?",
                      (dumps(sorted(set(g["declined_drivers"]) | {driver["id"]})), group_id))
        self.dispatch_cycle()
        return {"ok": True}

    def advance(self, driver: dict, group_id: int) -> dict:
        """다음 정차 지점 도착. 실제 앱에서는 GPS 누적거리를 쓰고, 여기서는 계획 구간 거리를 쓴다."""
        with self.db.tx() as c:
            g = self._driver_group(driver, group_id)
            if g["status"] != "riding":
                raise ServiceError("운행 중인 그룹이 아닙니다")
            k = g["progress"] + 1
            if k >= len(g["stops"]):
                raise ServiceError("이미 마지막 지점입니다. 최종 요금을 입력해 운행을 끝내세요")
            stop = g["stops"][k]
            r = self.get_request(stop["request_id"])
            if stop["kind"] == "pickup" and not r["notice_ack_at"]:
                raise ServiceError("이 승객이 아직 탑승 전 안내를 확인하지 않았어요. 승객 앱에서 확인한 뒤 탑승 처리할 수 있어요", 409)
            odometer = g["odometer"] + [round(sum(g["legs_m"][:k]))]
            c.execute("UPDATE requests SET status = ? WHERE id = ?",
                      ("riding" if stop["kind"] == "pickup" else "dropped", stop["request_id"]))
            c.execute("UPDATE groups SET progress = ?, odometer = ?, arriving_at = NULL, wait_started_at = NULL, "
                      "wait_request_id = NULL, updated_at = ? WHERE id = ?", (k, dumps(odometer), self._stamp(), group_id))
            pt = r["origin"] if stop["kind"] == "pickup" else r["dest"]
            c.execute("UPDATE drivers SET lat = ?, lng = ?, updated_at = ? WHERE user_id = ?",
                      (pt["lat"], pt["lng"], self._stamp(), driver["id"]))
        return self.group_view(group_id, driver_view=True)

    def notify_arriving(self, driver: dict, group_id: int) -> dict:
        with self.db.tx() as c:
            g = self._driver_group(driver, group_id)
            if g["status"] != "riding":
                raise ServiceError("운행 중일 때만 알림을 보낼 수 있어요")
            c.execute("UPDATE groups SET arriving_at = ? WHERE id = ?", (self._stamp(), group_id))
        return {"ok": True}

    # -- 노쇼: 기사님이 탑승 지점에 도착해 NO_SHOW_WAIT_MIN분을 기다려도 승객이 안 오면 처리한다 ----
    def arrive_pickup(self, driver: dict, group_id: int) -> dict:
        with self.db.tx() as c:
            g = self._driver_group(driver, group_id)
            if g["status"] != "riding" or g["progress"] + 1 >= len(g["stops"]):
                raise ServiceError("운행 중일 때만 쓸 수 있어요")
            stop = g["stops"][g["progress"] + 1]
            if stop["kind"] != "pickup":
                raise ServiceError("다음 정차가 탑승 지점일 때만 쓸 수 있어요")
            if g["wait_request_id"] != stop["request_id"]:
                c.execute("UPDATE groups SET wait_started_at = ?, wait_request_id = ?, arriving_at = ? WHERE id = ?",
                          (self._stamp(), stop["request_id"], self._stamp(), group_id))
        return self.group_view(group_id, driver_view=True)

    def no_show(self, driver: dict, group_id: int) -> dict:
        with self.db.tx() as c:
            g = self._driver_group(driver, group_id)
            if g["status"] != "riding" or g["progress"] + 1 >= len(g["stops"]):
                raise ServiceError("운행 중일 때만 노쇼를 처리할 수 있어요")
            stop = g["stops"][g["progress"] + 1]
            if stop["kind"] != "pickup" or g["wait_request_id"] != stop["request_id"] or not g["wait_started_at"]:
                raise ServiceError("탑승 지점에 도착했다고 먼저 알려 주세요")
            waited = (self.ts() - parse_ts(g["wait_started_at"])).total_seconds() / 60
            if waited < self.cfg.no_show_wait_min:
                left = math.ceil(self.cfg.no_show_wait_min - waited)
                raise ServiceError(f"도착하고 {self.cfg.no_show_wait_min}분을 기다린 뒤에 노쇼로 처리할 수 있어요({left}분 남음)", 409)
            r = self.get_request(stop["request_id"])
            if len(g["member_ids"]) < 2:
                raise ServiceError("마지막 남은 승객은 노쇼로 뺄 수 없어요. 운영팀에 알려 주세요", 409)
            self._trust(c, r["user_id"], self.cfg.trust_no_show, "no_show", "탑승 지점에 나오지 않음(기사님 확인)", group_id)
            c.execute("UPDATE users SET penalty = penalty + 1 WHERE id = ?", (r["user_id"],))
            remaining = self._reroute_without(c, g, r["id"])
            c.execute("UPDATE groups SET wait_started_at = NULL, wait_request_id = NULL, arriving_at = NULL, is_solo = ? "
                      "WHERE id = ?", (1 if len(remaining) == 1 else 0, group_id))
            c.execute("UPDATE requests SET status = 'cancelled', no_show = 1, group_id = NULL, note = ? WHERE id = ?",
                      ("탑승 지점에 나오지 않아 노쇼로 처리됐어요", r["id"]))
            r["group_id"] = group_id
            paid = self._compensate(c, r, group_id, "동승자 노쇼")
            self._release_hold(c, r["id"], "노쇼 처리 뒤 남은 예약금")
            for rid in remaining:
                c.execute("UPDATE requests SET note = ? WHERE id = ?",
                          (f"동승자 한 명이 나오지 않아 빼고 출발했어요{'. 늘어난 요금은 그 사람 예약금에서 보상해 드려요' if paid else ''}", rid))
        self.raise_alert("no_show", "low", "노쇼가 처리됐어요", f"그룹 #{group_id}에서 요청 #{r['id']}가 노쇼로 빠졌어요",
                         group_id=group_id, dedupe=False)
        return self.group_view(group_id, driver_view=True)

    def _settle(self, g: dict, final_fare: int | None, toll: int, toll_leg: int | None) -> tuple[int, dict]:
        fare = int(final_fare if final_fare else g["est_fare"])
        if fare <= 0:
            raise ServiceError("최종 요금은 0원보다 커야 합니다")
        if toll < 0:
            raise ServiceError("통행료는 0원 이상이어야 합니다")
        odo = g["odometer"] if len(g["odometer"]) == len(g["stops"]) else \
            [round(sum(g["legs_m"][:k])) for k in range(len(g["stops"]))]
        legs = [float(b - a) for a, b in zip(odo, odo[1:])]
        leg_tolls = [0] * len(legs)
        if toll:
            idx = toll_leg if toll_leg is not None else max(range(len(legs)), key=lambda i: legs[i])
            if not 0 <= idx < len(legs):
                raise ServiceError("통행료 구간 번호가 올바르지 않습니다")
            leg_tolls[idx] = int(toll)
        members = {rid: self.get_request(rid) for rid in g["member_ids"]}
        result = split_fare(
            [Stop(s["request_id"], s["kind"]) for s in g["stops"]], legs,
            {rid: m["seats"] for rid, m in members.items()}, fare,
            leg_tolls=leg_tolls,
            solo_fares={rid: m["solo_fare"] for rid, m in members.items()},
            solo_distances={rid: m["solo_m"] for rid, m in members.items()},
            est_group_fare=g["est_fare"],
            min_discount=0.0 if g["is_solo"] else self.cfg.min_discount,
            rounding_unit=self.cfg.rounding_unit)
        settlement = result.to_dict()
        self._apply_trust(settlement, members, g)
        settlement["deviation"] = round((fare - g["est_fare"]) / g["est_fare"], 3) if g["est_fare"] else 0
        if abs(settlement["deviation"]) >= 0.3:
            settlement["notes"].append("예상 요금과 30% 이상 차이 나 영수증 확인이 필요합니다(기획서 9장)")
        return fare, settlement

    def _apply_trust(self, settlement: dict, members: dict[int, dict], g: dict) -> None:
        """정산 결과(dict)에 신뢰도 추가 부담을 반영한다. 혼자 타는 그룹은 손대지 않는다."""
        if g["is_solo"] or len(members) < 2:
            return
        final = {int(k): v for k, v in settlement["final"].items()}
        rates = {rid: self._surcharge_rate(m) for rid, m in members.items()}
        new, delta, notes = apply_trust_surcharge(final, rates, {rid: m["solo_fare"] for rid, m in members.items()},
                                                  self.cfg.trust_low_min_discount, self.cfg.rounding_unit)
        settlement["final"] = {str(k): v for k, v in new.items()}
        settlement["trust_adjust"] = {str(k): v for k, v in delta.items() if v}
        settlement["notes"] = list(settlement.get("notes", [])) + notes

    def fare_preview(self, driver: dict, group_id: int, final_fare: int | None, toll: int = 0,
                     toll_leg: int | None = None) -> dict:
        g = self._driver_group(driver, group_id)
        fare, settlement = self._settle(g, final_fare, toll, toll_leg)
        return {"final_fare": fare, "settlement": settlement}

    def complete(self, driver: dict, group_id: int, final_fare: int | None = None, toll: int = 0,
                 toll_leg: int | None = None) -> dict:
        with self.db.tx() as c:
            g = self._driver_group(driver, group_id)
            if g["status"] != "riding" or g["progress"] != len(g["stops"]) - 1:
                raise ServiceError("마지막 하차 지점에 도착한 뒤에 운행을 끝낼 수 있습니다")
            fare, settlement = self._settle(g, final_fare, toll, toll_leg)
            c.execute("UPDATE groups SET status = 'completed', final_fare = ?, settlement = ?, updated_at = ? WHERE id = ?",
                      (fare, dumps(settlement), self._stamp(), group_id))
            for rid in g["member_ids"]:
                c.execute("UPDATE requests SET status = 'done' WHERE id = ?", (rid,))
                # 포인트로 자동 결제: 예약금을 풀고 실제 몫을 낸다. 문제없이 탔으니 신뢰도도 오른다
                req = self.get_request(rid)
                self._release_hold(c, rid, "운행 끝, 예약금 풀림")
                share = int(settlement["final"][str(rid)])
                self._wallet(c, req["user_id"], -share, "pay", "동승 요금 결제", group_id, rid)
                c.execute("UPDATE requests SET paid_at = ? WHERE id = ?", (self._stamp(), rid))
                self._ride_trust(c, req["user_id"], group_id)
            total = sum(settlement["final"].values())
            expected = fare + int(settlement.get("total_toll", 0))
            if total != expected:
                self.raise_alert("settlement_mismatch", "high", "정산 합계가 최종 요금과 달라요",
                                 f"그룹 #{group_id}: 합계 {total:,}원, 요금+통행료 {expected:,}원", group_id=group_id)
        self.dispatch_cycle()
        return self.group_view(group_id, driver_view=True)

    # =====================================================================
    # 신고
    # =====================================================================
    def report(self, user: dict, group_id: int | None, kind: str, category: str, note: str = "",
               target_request_id: int | None = None, lat: float | None = None, lng: float | None = None) -> dict:
        if kind not in safety.CATEGORIES or category not in safety.CATEGORIES[kind]:
            raise ServiceError("신고 탭이나 카테고리가 올바르지 않아요", 422)
        g = self.get_group_row(group_id) if group_id else None
        my_request = None
        if g:
            if user["role"] == "passenger":
                mine = [rid for rid in g["member_ids"] if self.get_request(rid)["user_id"] == user["id"]]
                if not mine:
                    raise ServiceError("내가 탄 운행만 신고할 수 있어요", 403)
                my_request = mine[0]
            elif user["role"] == "driver" and g["driver_id"] != user["id"]:
                raise ServiceError("내가 맡은 운행만 신고할 수 있어요", 403)
        if kind == "companion":
            if not g:
                raise ServiceError("동승자 신고는 운행을 골라야 해요", 422)
            others = [rid for rid in g["member_ids"] if rid != my_request]
            if target_request_id is None and len(others) == 1:
                target_request_id = others[0]
            if target_request_id not in others:
                raise ServiceError("신고할 동승자를 같은 운행의 동승자 중에서 골라 주세요", 422)
        elif target_request_id is not None:
            target_request_id = None

        now = self.ts()
        if kind != "emergency":
            since = (now - timedelta(seconds=safety.DEDUPE_SEC)).strftime(STAMP)
            dup = self.db.one("SELECT * FROM reports WHERE reporter_id = ? AND category = ? AND "
                              "COALESCE(group_id, 0) = ? AND created_at >= ? ORDER BY id DESC LIMIT 1",
                              user["id"], category, group_id or 0, since)
            if dup:
                return {**self._report_view(dup), "duplicate": True}

        source = "none"
        if lat is not None and lng is not None:
            source = "gps"
        elif g and g["progress"] >= 0:
            s = g["stops"][g["progress"]]
            r = self.get_request(s["request_id"])
            pt = r["origin"] if s["kind"] == "pickup" else r["dest"]
            lat, lng, source = pt["lat"], pt["lng"], "last_stop"
        snapshot = {"reported_at": now.strftime(STAMP), "reporter": {"id": user["id"], "role": user["role"]}}
        if g:
            snapshot.update(self._group_snapshot(g))
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO reports (group_id, request_id, reporter_id, kind, category, note, target_request_id, lat, lng, "
                "location_source, snapshot, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'received', ?, ?)",
                (group_id, my_request, user["id"], kind, category, (note or "")[:500], target_request_id, lat, lng,
                 source, dumps(snapshot), now.strftime(STAMP), now.strftime(STAMP)))
            rid = cur.lastrowid
        if kind == "emergency":
            label = safety.category_label(kind, category)
            where = f"위치 {lat:.5f}, {lng:.5f}({'GPS' if source == 'gps' else '마지막 정차 지점'})" if lat is not None else "위치 정보 없음"
            self.raise_alert("emergency_report", "high", f"긴급 신고: {label}",
                             f"{snapshot.get('car_number') or '차량 정보 없음'} · {where}" + (f" · {note[:80]}" if note else ""),
                             group_id=group_id, report_id=rid, dedupe=False)
        return {**self._report_view(self.db.one("SELECT * FROM reports WHERE id = ?", rid)), "duplicate": False}

    def _group_snapshot(self, g: dict) -> dict:
        d = self.db.one("SELECT * FROM drivers WHERE user_id = ?", g["driver_id"]) if g["driver_id"] else None
        names = []
        for s in g["stops"]:
            r = self.get_request(s["request_id"])
            names.append((r["origin"] if s["kind"] == "pickup" else r["dest"]).get("name", ""))
        return {"group_id": g["id"], "group_status": g["status"], "progress": g["progress"],
                "car_number": d["car_number"] if d else None, "driver_name": d["name"] if d else None,
                "driver_id": g["driver_id"], "route": names, "members": len(g["member_ids"]),
                "est_fare": g["est_fare"], "final_fare": g["final_fare"]}

    def _report_view(self, r: dict) -> dict:
        return {**r, "kind_label": safety.KIND_LABEL.get(r["kind"], r["kind"]),
                "category_label": safety.category_label(r["kind"], r["category"])}

    def confirm_report(self, report_id: int) -> dict:
        """운영자가 동승자 신고를 사실로 확인하면 신고 대상의 신뢰도를 깎는다(허위 신고로 남을 깎지 못하게 확인 후에만)."""
        r = self.db.one("SELECT * FROM reports WHERE id = ?", report_id)
        if not r:
            raise ServiceError("신고를 찾을 수 없어요", 404)
        if r["kind"] != "companion" or not r["target_request_id"]:
            raise ServiceError("동승자 신고만 신뢰도에 반영돼요", 409)
        if r["confirmed"]:
            return self._report_view(r)
        target = self.get_request(r["target_request_id"])
        with self.db.tx() as c:
            label = safety.category_label(r["kind"], r["category"])
            self._trust(c, target["user_id"], self.cfg.trust_report, "report", f"사실로 확인된 신고: {label}", r["group_id"])
            c.execute("UPDATE reports SET confirmed = 1, status = 'resolved', updated_at = ? WHERE id = ?",
                      (self._stamp(), report_id))
        return self._report_view(self.db.one("SELECT * FROM reports WHERE id = ?", report_id))

    def update_report(self, report_id: int, status: str) -> dict:
        if status not in ("received", "in_progress", "resolved"):
            raise ServiceError("처리 상태가 올바르지 않아요", 422)
        with self.db.tx() as c:
            c.execute("UPDATE reports SET status = ?, updated_at = ? WHERE id = ?", (status, self._stamp(), report_id))
        r = self.db.one("SELECT * FROM reports WHERE id = ?", report_id)
        if not r:
            raise ServiceError("신고를 찾을 수 없어요", 404)
        return self._report_view(r)

    # =====================================================================
    # 경보
    # =====================================================================
    def raise_alert(self, kind: str, severity: str, title: str, detail: str = "", request_id: int | None = None,
                    group_id: int | None = None, report_id: int | None = None, dedupe: bool = True) -> int | None:
        stamp = self._stamp()
        with self.db.tx() as c:
            if dedupe:
                since = (self.ts() - timedelta(minutes=ALERT_DEDUPE_MIN)).strftime(STAMP)
                open_ = self.db.one("SELECT id FROM alerts WHERE kind = ? AND acked_at IS NULL AND COALESCE(group_id, 0) = ? "
                                    "AND updated_at >= ? ORDER BY id DESC LIMIT 1", kind, group_id or 0, since)
                if open_:
                    c.execute("UPDATE alerts SET count = count + 1, detail = ?, updated_at = ? WHERE id = ?",
                              (detail[:600], stamp, open_["id"]))
                    return open_["id"]
            cur = c.execute("INSERT INTO alerts (kind, severity, title, detail, request_id, group_id, report_id, created_at, "
                            "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (kind, severity, title[:120], detail[:600], request_id, group_id, report_id, stamp, stamp))
        log.warning("경보[%s] %s: %s", severity, title, detail[:200])
        return cur.lastrowid

    def ack_alert(self, alert_id: int, admin: dict) -> dict:
        with self.db.tx() as c:
            c.execute("UPDATE alerts SET acked_at = COALESCE(acked_at, ?), acked_by = ? WHERE id = ?",
                      (self._stamp(), admin["id"], alert_id))
        a = self.db.one("SELECT * FROM alerts WHERE id = ?", alert_id)
        if not a:
            raise ServiceError("경보를 찾을 수 없어요", 404)
        return a

    # =====================================================================
    # 조회용 뷰
    # =====================================================================
    def get_group_row(self, group_id: int) -> dict:
        g = self.db.one("SELECT * FROM groups WHERE id = ?", group_id)
        if not g:
            raise ServiceError("그룹을 찾을 수 없습니다", 404)
        return g

    def group_view(self, group_id: int, viewer_request_id: int | None = None, driver_view: bool = False) -> dict:
        """승객 화면(viewer_request_id): 동승자끼리는 닉네임·성별·신뢰도만 공유한다(사용자 id 등은 숨김).
        기사님 화면(driver_view): 승객을 '익명 A/B/C'로만 보여 준다."""
        g = self.get_group_row(group_id)
        members = {rid: self.get_request(rid) for rid in g["member_ids"]}
        stops = [Stop(s["request_id"], s["kind"]) for s in g["stops"]]
        estimate = split_fare(
            stops, g["legs_m"], {rid: m["seats"] for rid, m in members.items()}, g["est_fare"],
            solo_fares={rid: m["solo_fare"] for rid, m in members.items()},
            solo_distances={rid: m["solo_m"] for rid, m in members.items()},
            est_group_fare=g["est_fare"],
            min_discount=0.0 if g["is_solo"] else self.cfg.min_discount,
            rounding_unit=self.cfg.rounding_unit)
        est_dict = estimate.to_dict()
        self._apply_trust(est_dict, members, g)          # 신뢰도 추가 부담을 예상 몫에도 반영
        est_final = {int(k): v for k, v in est_dict["final"].items()}
        settlement = g["settlement"]
        final = settlement["final"] if settlement else None
        adjust = (settlement or est_dict).get("trust_adjust", {})
        pickup_order = [s.request_id for s in stops if s.kind == "pickup"]
        labels = {rid: f"익명 {chr(65 + i)}" for i, rid in enumerate(pickup_order)}
        hide = viewer_request_id is not None or driver_view

        def name(rid: int) -> str:
            if driver_view:
                return labels[rid]
            if viewer_request_id is not None and rid == viewer_request_id:
                return "나"
            return members[rid]["nickname"]

        member_views = []
        for rid, m in members.items():
            order = [i for i, s in enumerate(stops) if s.request_id == rid]
            share = final[str(rid)] if final else est_final[rid]
            ride_m = 0.0
            for i in range(order[0], order[-1]):
                ride_m += g["legs_m"][i] if i < len(g["legs_m"]) else 0
            rate = self._surcharge_rate(m)
            member_views.append({
                "request_id": rid, "user_id": None if hide else m["user_id"], "nickname": name(rid),
                "label": labels[rid], "is_me": rid == viewer_request_id, "gender": m["gender"],
                "trust": None if driver_view else m["trust"],
                "low_trust": (m["trust"] < self.cfg.trust_low) if not driver_view else False,
                "surcharge": rate if not g["is_solo"] else 0, "trust_adjust": int(adjust.get(str(rid), 0)),
                "hold": m["hold_amount"] if (viewer_request_id is None or rid == viewer_request_id) and not driver_view else None,
                "base_share": m["base_share"],
                "seats": m["seats"], "status": m["status"], "type": m["type"], "origin": m["origin"], "dest": m["dest"],
                "solo_fare": m["solo_fare"], "solo_m": round(m["solo_m"]), "ride_m": round(ride_m),
                "share": share, "share_is_final": bool(final), "saving": m["solo_fare"] - share,
                "accepted": rid in g["accepted"], "pickup_index": order[0], "dropoff_index": order[-1],
                "notice_ack": bool(m["notice_ack_at"]), "paid": bool(m["paid_at"]),
            })

        speed = self.cfg.avg_speed_kmh * 1000 / 60  # m/분
        dep = parse_dt(g["departure"])
        cum = 0.0
        stop_views = []
        for i, s in enumerate(stops):
            m = members[s.request_id]
            pt = m["origin"] if s.kind == "pickup" else m["dest"]
            eta = dep + timedelta(minutes=round(cum / speed) if g["total_m"] else 0)
            stop_views.append({"index": i, "request_id": s.request_id, "kind": s.kind, "nickname": name(s.request_id),
                               "label": labels[s.request_id], "name": pt.get("name", ""), "lat": pt["lat"], "lng": pt["lng"],
                               "at_m": round(cum), "eta": eta.strftime(FMT), "reached": i <= g["progress"],
                               "notice_ack": bool(m["notice_ack_at"])})
            if i < len(g["legs_m"]):
                cum += g["legs_m"][i]

        driver = None
        if g["driver_id"]:
            d = self.db.one("SELECT * FROM drivers WHERE user_id = ?", g["driver_id"])
            if d:
                driver = {"name": d["name"], "car_number": d["car_number"], "car_model": d["car_model"],
                          "car_color": d["car_color"], "lat": d["lat"], "lng": d["lng"]}
        return {
            "id": g["id"], "status": g["status"], "is_solo": bool(g["is_solo"]), "departure": g["departure"],
            "expires_at": g["expires_at"], "provider": g["provider"], "total_m": round(g["total_m"]),
            "duration_s": round(g["duration_s"]), "est_fare": g["est_fare"], "est_toll": g["est_toll"],
            "final_fare": g["final_fare"], "progress": g["progress"],
            "ready_to_complete": g["status"] == "riding" and g["progress"] == len(stops) - 1,
            "members": member_views, "stops": stop_views, "legs_m": [round(x) for x in g["legs_m"]],
            "polyline": g["polyline"], "estimate": est_dict, "settlement": settlement,
            "solo_sum_m": round(sum(m["solo_m"] for m in members.values())),
            "driver": driver, "waiting_driver": g["status"] == "confirmed" and not g["driver_id"],
            "reservation_assigned": g["status"] == "confirmed" and bool(g["driver_id"]),
            "wait_started_at": g["wait_started_at"], "wait_request_id": g["wait_request_id"],
            "no_show_wait_min": self.cfg.no_show_wait_min,   # 화면이 예전 설정을 들고 있어도 서버 기준으로 센다
            "offer_pending": bool(g["offer_driver_id"]), "arriving_at": g["arriving_at"],
            "match_method": g["match_method"], "match_reason": g["match_reason"] if not hide else "",
            "updated_at": g["updated_at"],
        }

    def user_state(self, user_id: int) -> dict:
        user = self.get_user(user_id)
        active = self.db.one(f"SELECT id FROM requests WHERE user_id = ? AND status IN {ACTIVE_REQUEST} "
                             "ORDER BY id DESC LIMIT 1", user_id)
        recent = self.db.all("SELECT id FROM requests WHERE user_id = ? AND status NOT IN "
                             f"{ACTIVE_REQUEST} ORDER BY id DESC LIMIT 5", user_id)

        def bundle(rid: int) -> dict:
            r = self.get_request(rid)
            return {"request": r, "group": self.group_view(r["group_id"], viewer_request_id=rid) if r["group_id"] else None}

        return {"user": self.public_user(user), "active": bundle(active["id"]) if active else None,
                "history": [bundle(r["id"]) for r in recent], "now": self.now().strftime(FMT), "ts": self._stamp(),
                "auto_match": self.auto_match}

    def overview(self) -> dict:
        reqs = self.db.all(f"SELECT r.*, u.nickname, u.gender, u.is_demo FROM requests r JOIN users u ON u.id = r.user_id "
                           f"WHERE r.status IN {ACTIVE_REQUEST} ORDER BY r.id")
        groups = self.db.all("SELECT id FROM groups WHERE status IN ('proposed', 'confirmed', 'riding') ORDER BY id")
        done = self.db.all("SELECT id FROM groups WHERE status = 'completed' ORDER BY id DESC LIMIT 5")
        counts = {s: 0 for s in ACTIVE_REQUEST}
        for r in reqs:
            counts[r["status"]] += 1
            r["deadline"] = (parse_dt(r["window_end"]) - timedelta(minutes=self.cfg.deadline_min)).strftime(FMT)
        return {"requests": reqs, "groups": [self.group_view(g["id"]) for g in groups],
                "completed": [self.group_view(g["id"]) for g in done], "counts": counts,
                "last_match": self.last_match, "now": self.now().strftime(FMT)}

    def admin_overview(self) -> dict:
        ov = self.overview()
        alerts = self.db.all("SELECT * FROM alerts WHERE acked_at IS NULL ORDER BY "
                             "CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END, updated_at DESC LIMIT 50")
        recent_acked = self.db.all("SELECT * FROM alerts WHERE acked_at IS NOT NULL ORDER BY acked_at DESC LIMIT 10")
        reports = self.db.all("SELECT * FROM reports ORDER BY CASE WHEN status = 'resolved' THEN 1 ELSE 0 END, "
                              "CASE kind WHEN 'emergency' THEN 0 ELSE 1 END, id DESC LIMIT 40")
        decisions = self.db.all("SELECT id, kind, used_ai, provider, model, method, summary, result, error, created_at "
                                "FROM ai_decisions WHERE kind != 'summary' ORDER BY id DESC LIMIT 20")
        summary = self.db.one("SELECT * FROM ai_decisions WHERE kind = 'summary' ORDER BY id DESC LIMIT 1")
        drivers = []
        for d in self.db.all("SELECT * FROM drivers ORDER BY user_id"):
            riding = self.db.one("SELECT id FROM groups WHERE driver_id = ? AND status = 'riding' ORDER BY id DESC LIMIT 1",
                                 d["user_id"])
            offer = self._offer_for(d["user_id"])
            drivers.append({**d, "available": bool(d["available"]),
                            "reservations": self.db.scalar("SELECT COUNT(*) FROM groups WHERE driver_id = ? AND status = 'confirmed'",
                                                           d["user_id"]),
                            "trip_id": riding["id"] if riding else None,          # 운영 지도에서 기사님을 누르면 이 운행을 보여 준다
                            "offer_group_id": offer["id"] if offer else None,
                            "state": ("운행 중" if riding else "배차 제안 중" if offer
                                      else "대기" if d["available"] else "쉬는 중")})
        notice = []
        for g in ov["groups"]:
            if g["status"] in ("confirmed", "riding"):
                notice.append({"group_id": g["id"], "acked": sum(m["notice_ack"] for m in g["members"]),
                               "total": len(g["members"])})
        return {**ov, "alerts": alerts, "recent_acked_alerts": recent_acked,
                "reports": [self._report_view(r) for r in reports], "ai_decisions": decisions,
                "ai_summary": summary, "ai_status": self.router.status(), "ai_enabled": self.router.enabled,
                "ai_orders": {task: [p.name for p in self.router.ordered(parse_order(getattr(self.cfg, f"ai_order_{task}")))]
                              for task in ("dispatch", "match", "predict", "summary")},
                "drivers": drivers, "notice": notice, "auto_match": self.auto_match, "metrics": self.metrics()}

    def metrics(self) -> dict:
        now = self.ts()
        since = now - timedelta(hours=24)
        s = since.strftime(STAMP)
        reqs = self.db.all("SELECT id, type, status, created_at FROM requests WHERE created_at >= ?", s)
        groups = self.db.all("SELECT id, is_solo, status, member_ids, provider, created_at, final_fare, settlement "
                             "FROM groups WHERE created_at >= ?", s)
        hours = [(since + timedelta(hours=i + 1)).replace(minute=0, second=0) for i in range(24)]
        buckets = [{"hour": h.strftime("%H:00"), "requests": 0, "matched": 0, "expired": 0, "solo": 0} for h in hours]

        def idx(stamp: str) -> int:
            t = parse_ts(stamp)
            return min(23, max(0, int((t - since).total_seconds() // 3600)))

        for r in reqs:
            buckets[idx(r["created_at"])]["requests"] += 1
            if r["status"] == "expired":
                buckets[idx(r["created_at"])]["expired"] += 1
        waits = []
        for g in groups:
            b = buckets[idx(g["created_at"])]
            if g["is_solo"]:
                b["solo"] += 1
                continue
            b["matched"] += 1
            created = [self.db.one("SELECT created_at FROM requests WHERE id = ?", rid) for rid in g["member_ids"]]
            first = min(parse_ts(c["created_at"]) for c in created if c)
            waits.append((parse_ts(g["created_at"]) - first).total_seconds() / 60)
        mismatch = 0
        for g in groups:
            st = g["settlement"]
            if g["status"] == "completed" and st and sum(st["final"].values()) != g["final_fare"] + int(st.get("total_toll", 0)):
                mismatch += 1
        decisions = self.db.all("SELECT used_ai, provider, result FROM ai_decisions WHERE kind = 'match' AND created_at >= ?", s)
        tried = [d for d in decisions if d["result"].get("attempted_ai")]
        route_total = len(groups)
        route_fallback = sum(1 for g in groups if "fallback" in (g["provider"] or ""))
        return {
            "hourly": buckets,
            "requests_24h": len(reqs), "quick_24h": sum(1 for r in reqs if r["type"] == "quick"),
            "reserved_24h": sum(1 for r in reqs if r["type"] == "reserved"),
            "matched_groups_24h": sum(1 for g in groups if not g["is_solo"]),
            "expired_24h": sum(1 for r in reqs if r["status"] == "expired"),
            "avg_wait_min": round(sum(waits) / len(waits), 1) if waits else None,
            "ai_match_runs_24h": len(tried),
            "ai_fallback_ratio": round(sum(1 for d in tried if not d["used_ai"]) / len(tried), 3) if tried else None,
            "route_fallback_ratio": round(route_fallback / route_total, 3) if route_total else None,
            "settlement_mismatch": mismatch,
            "open_alerts": self.db.scalar("SELECT COUNT(*) FROM alerts WHERE acked_at IS NULL"),
            "open_reports": self.db.scalar("SELECT COUNT(*) FROM reports WHERE status != 'resolved'"),
        }

    def summarize_ops(self, force: bool = False) -> dict | None:
        """한 시간마다 AI가 운영 상황을 요약한다(경보 판정은 규칙으로 따로 한다)."""
        if not self.router.enabled:
            return None
        last = self.db.one("SELECT created_at FROM ai_decisions WHERE kind = 'summary' ORDER BY id DESC LIMIT 1")
        if not force and last and parse_ts(last["created_at"]) > self.ts() - timedelta(hours=1):
            return None
        m = self.metrics()
        payload = {k: v for k, v in m.items() if k != "hourly"}
        payload["last_hour"] = m["hourly"][-1]
        try:
            ai = self.summarizer.summarize(payload)
            text = str(ai.data.get("summary", ""))[:500]
            concerns = [str(x)[:120] for x in ai.data.get("concerns", [])][:5]
            did = self._log_decision("summary", True, ai.provider, ai.model, "summary", text, payload,
                                     {"summary": text, "concerns": concerns}, {}, None)
        except AllProvidersFailed as exc:
            did = self._log_decision("summary", False, "", "", "summary", "AI 요약 실패", payload, {}, {}, str(exc)[:300])
        return self.db.one("SELECT * FROM ai_decisions WHERE id = ?", did)

    # =====================================================================
    # 데모 도우미
    # =====================================================================
    def seed_demo(self, count: int = 4, gender: str = "F", origin: Point | None = None, dest: Point | None = None,
                  window_start: str | None = None, window_end: str | None = None, seed: int | None = None,
                  type: str = "quick") -> list[dict]:
        """시연용 요청을 만든다. 출발·도착을 정하지 않으면 삼척시 안의 지점에서 무작위로 고른다.
        매칭이 되도록 사람들을 2~3명씩 '같은 방향 무리'로 나누고, 무리마다 출발·도착·시간을 따로 뽑는다."""
        rng = random.Random(seed)
        now = self.now()
        created = []
        for size in _split_counts(count, rng):
            if origin is not None and dest is not None:
                o0, d0 = origin, dest
            else:
                o0, d0 = _random_trip(rng)
            if type == "reserved" and not window_start:
                lead = self.cfg.reserve_min_lead_min + 10 + rng.randrange(0, 19) * 10   # 50분~3시간 40분 뒤
                start = now + timedelta(minutes=lead)
                ws, we = start.strftime(FMT), (start + timedelta(minutes=rng.choice([20, 30, 60]))).strftime(FMT)
            elif type == "reserved":
                ws = window_start
                we = window_end or (parse_dt(ws) + timedelta(minutes=30)).strftime(FMT)
            else:
                ws, we = window_start, window_end
            for _ in range(size):
                g = gender if rng.random() < 0.85 else ("M" if gender == "F" else "F")
                user = self.create_user(self._demo_name(), g, is_demo=True)
                o = _jitter(o0, rng, 300)
                d = _toward(o0, d0, rng)
                created.append(self.create_request(
                    user["id"], Point(o.lat, o.lng, f"{o0.name} 근처" if o0.name else "출발지 근처"),
                    Point(d.lat, d.lng, f"{d0.name} 방면" if d0.name else "도착지 방면"),
                    ws, we, seats=1, detour_ratio=round(rng.uniform(0.3, 0.6), 2), fallback="cancel", type=type))
        return created

    def seed_near(self, user: dict, count: int = 2) -> list[dict]:
        """시연용: 내 요청과 같은 방향·같은 성별·같은 시간대의 데모 동승자를 만든다."""
        r = self.db.one(f"SELECT * FROM requests WHERE user_id = ? AND status = 'waiting' ORDER BY id DESC LIMIT 1", user["id"])
        if not r:
            raise ServiceError("매칭을 기다리는 요청이 있을 때 만들 수 있어요")
        o, d = Point.from_dict(r["origin"]), Point.from_dict(r["dest"])
        created = []
        rng = random.Random(r["id"])
        for _ in range(count):
            u = self.create_user(self._demo_name(), user["gender"], is_demo=True)
            oo, dd = _jitter(o, rng, 150), _jitter(d, rng, 150)
            created.append(self.create_request(u["id"], Point(oo.lat, oo.lng, f"{o.name or '출발지'} 근처"),
                                               Point(dd.lat, dd.lng, f"{d.name or '도착지'} 방면"),
                                               r["window_start"], r["window_end"], 1, 0.4, "cancel", "reserved"
                                               if r["type"] == "reserved" else "quick"))
        return created

    def _demo_name(self) -> str:
        """시연용 데모 동승자 이름: demo + 다음 사용자 번호(로그인 계정 user1~n, driver1~n과 같은 방식)."""
        return f"demo{(self.db.scalar('SELECT COALESCE(MAX(id), 0) FROM users') or 0) + 1}"

    def reset(self) -> None:
        self.db.reset(keep_accounts=True)
        with self.db.tx() as c:
            c.execute("UPDATE drivers SET available = 0")
        self.last_match = None


def _jitter(p: Point, rng: random.Random, radius_m: float) -> Point:
    r = radius_m * math.sqrt(rng.random())
    t = rng.random() * 2 * math.pi
    dlat = r * math.cos(t) / 111_320
    dlng = r * math.sin(t) / (111_320 * math.cos(math.radians(p.lat)))
    return Point(p.lat + dlat, p.lng + dlng, p.name)


# 데모 요청에 쓰는 삼척시 안 지점(동해시에 있는 동해역은 뺀다)
SAMCHEOK_PLACES = [p for p in DEMO_PLACES if p["name"] != "동해역"]


def _split_counts(count: int, rng: random.Random) -> list[int]:
    """count명을 2~3명씩 무리로 나눈다(1명이 남으면 혼자 무리)."""
    sizes, left = [], max(0, count)
    while left > 0:
        n = min(left, rng.choice([2, 3]))
        if left - n == 1:              # 1명만 남지 않게 (3명 남으면 3, 4명 남으면 2+2)
            n = 3 if n == 2 else 2
        sizes.append(n)
        left -= n
    return sizes


def _random_trip(rng: random.Random) -> tuple[Point, Point]:
    """삼척시 지점 두 곳을 무작위로 골라 출발·도착으로 쓴다(1.2km 이상 떨어진 곳)."""
    while True:
        a, b = rng.sample(SAMCHEOK_PLACES, 2)
        o, d = Point(**a), Point(**b)
        if haversine_m(o, d) >= 1200:
            return o, d


def _toward(origin: Point, dest: Point, rng: random.Random) -> Point:
    """출발지→도착지 방향으로 60%~120% 지점, 옆으로 조금 흔든 도착지."""
    u = rng.uniform(0.6, 1.2)
    base = Point(origin.lat + (dest.lat - origin.lat) * u, origin.lng + (dest.lng - origin.lng) * u)
    return _jitter(base, rng, 0.12 * haversine_m(origin, dest))
