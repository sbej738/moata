"""테스트는 외부 네트워크 없이, .env와 상관없이 기본 설정으로 돈다.

.env를 아예 읽지 않게 하고(MOATA_SKIP_DOTENV), 셸에 남아 있을 수 있는 실제 키도 비워 둔다.
"""
import os

os.environ["MOATA_SKIP_DOTENV"] = "1"
for key in ("KAKAO_JS_KEY", "KAKAO_REST_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY",
            "ADMIN_PASSWORD", "DEMO_PASSWORD", "GEMINI_FALLBACK_MODEL"):
    os.environ[key] = ""
os.environ["DB_PATH"] = ":memory:"

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.auth as auth  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.geo import Point  # noqa: E402
from app.llm import AIRouter  # noqa: E402
from app.main import create_app  # noqa: E402
from app.service import TaxiShareService  # noqa: E402

auth.ITERATIONS = 1000  # 테스트 속도용(실서비스는 20만 번)

# 삼척 지점(demo_places 좌표)
GATE = {"lat": 37.45140, "lng": 129.16172, "name": "강원대학교 삼척캠퍼스 정문"}
GATE_2 = {"lat": 37.45170, "lng": 129.16220, "name": "정문 건너편"}
TERMINAL = {"lat": 37.44015, "lng": 129.16904, "name": "삼척종합버스터미널"}
MARKET = {"lat": 37.44209, "lng": 129.16519, "name": "삼척중앙시장"}
STATION = {"lat": 37.43007, "lng": 129.17874, "name": "삼척역"}
PASSWORD = "testpass123"


def pt(d: dict) -> Point:
    return Point(d["lat"], d["lng"], d["name"])


class Clock:
    def __init__(self, t=datetime(2026, 11, 5, 21, 40)):
        self.t = t

    def __call__(self):
        return self.t


class Env:
    """서비스 하나를 여러 사람(각자 쿠키를 가진 TestClient)이 함께 쓰는 시험 환경."""

    def __init__(self, cfg: Settings | None = None, router: AIRouter | None = None):
        self.clock = Clock()
        self.cfg = cfg or Settings(admin_password="adminpass123")
        self.svc = TaxiShareService(Database(":memory:"), self.cfg, clock=self.clock,
                                    router=router if router is not None else AIRouter([]))
        self.app = create_app(service=self.svc, start_matcher=False)
        self._n = 0

    def client(self, app_name: str = "passenger") -> TestClient:
        c = TestClient(self.app)
        c.headers[ "X-Moata-App"] = app_name
        return c

    def student(self, nick: str = "가람", gender: str = "F") -> TestClient:
        self._n += 1
        c = self.client("passenger")
        r = c.post("/api/auth/signup", json={"email": f"2026{self._n:04d}@kangwon.ac.kr", "password": PASSWORD,
                                             "nickname": nick, "gender": gender})
        assert r.status_code == 200, r.text
        c.user = r.json()["user"]
        return c

    def driver(self, name: str = "김기사", lat: float = 37.44015, lng: float = 129.16904, available: bool = True) -> TestClient:
        self._n += 1
        c = self.client("driver")
        r = c.post("/api/auth/signup/driver", json={"login_id": f"driver{self._n}", "password": PASSWORD, "name": name,
                                                    "car_number": f"강원 3{self._n}바 12{self._n:02d}", "car_model": "쏘나타",
                                                    "car_color": "흰색"})
        assert r.status_code == 200, r.text
        c.user = r.json()["user"]
        if available:
            assert c.post("/api/driver/availability", json={"available": True, "lat": lat, "lng": lng}).status_code == 200
        return c

    def admin(self) -> TestClient:
        c = self.client("admin")
        r = c.post("/api/auth/login", json={"login": self.cfg.admin_id, "password": "adminpass123", "tab": "student"})
        assert r.status_code == 200, r.text
        assert r.json()["redirect"] == "/admin"
        return c


def ask(c: TestClient, origin=GATE, dest=TERMINAL, type="quick", **kw) -> dict:
    r = c.post("/api/requests", json={"type": type, "origin": origin, "dest": dest, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def state(c: TestClient) -> dict:
    r = c.get("/api/me/state")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture()
def env() -> Env:
    return Env()
