"""로그인·회원가입·역할: 학생/기사님/운영자 이동, 성별 필수·변경 불가, 서버 권한 확인."""
from app import auth
from tests.conftest import GATE, GATE_2, PASSWORD, STATION, TERMINAL, ask, state


def test_password_hash_roundtrip():
    h = auth.hash_password("abc12345")
    assert h.startswith("pbkdf2_sha256$") and "abc12345" not in h
    assert auth.verify_password("abc12345", h)
    assert not auth.verify_password("abc12346", h)


def test_signup_requires_school_email_and_gender(env):
    c = env.client()
    base = {"email": "20260099@kangwon.ac.kr", "password": PASSWORD, "nickname": "가람"}
    assert c.post("/api/auth/signup", json=base).status_code == 422                       # 성별 빠짐
    assert c.post("/api/auth/signup", json={**base, "gender": "X"}).status_code == 422
    r = c.post("/api/auth/signup", json={**base, "email": "me@gmail.com", "gender": "F"})
    assert r.status_code == 422 and "kangwon.ac.kr" in r.json()["detail"]
    r = c.post("/api/auth/signup", json={**base, "password": "short", "gender": "F"})
    assert r.status_code == 422
    assert c.post("/api/auth/signup", json={**base, "gender": "F"}).status_code == 200
    assert c.post("/api/auth/signup", json={**base, "gender": "F"}).status_code == 409   # 중복


def test_login_routes_by_role_and_tab(env):
    env.student("가람")
    d = env.driver(available=False)
    c = env.client()
    student_email = "20260001@kangwon.ac.kr"
    r = c.post("/api/auth/login", json={"login": student_email, "password": PASSWORD, "tab": "student"})
    assert r.status_code == 200 and r.json()["redirect"] == "/passenger"
    r = c.post("/api/auth/login", json={"login": student_email, "password": PASSWORD, "tab": "driver"})
    assert r.status_code == 422                                   # 기사님 탭은 이메일을 받지 않는다
    driver_id = d.user["login_id"]
    assert driver_id and d.user["email"] is None                  # 기사님은 아이디로 가입
    r = c.post("/api/auth/login", json={"login": driver_id, "password": PASSWORD, "tab": "student"})
    assert r.status_code == 403 and "기사님 탭" in r.json()["detail"]
    r = c.post("/api/auth/login", json={"login": driver_id.upper(), "password": PASSWORD, "tab": "driver"})
    assert r.status_code == 200 and r.json()["redirect"] == "/driver"
    assert c.post("/api/auth/login", json={"login": student_email, "password": "wrong999", "tab": "student"}).status_code == 401
    env.admin()  # 학생 탭에서 운영자 아이디 → /admin


def test_driver_signup_requires_valid_login_id(env):
    c = env.client("driver")
    base = {"password": PASSWORD, "name": "김기사", "car_number": "강원 31바 1234"}
    assert c.post("/api/auth/signup/driver", json={**base, "login_id": "a@b.kr"}).status_code == 422
    assert c.post("/api/auth/signup/driver", json={**base, "login_id": "ab"}).status_code == 422
    assert c.post("/api/auth/signup/driver", json={**base, "login_id": "kim_driver"}).status_code == 200
    assert c.post("/api/auth/signup/driver", json={**base, "login_id": "kim_driver"}).status_code == 409
    assert env.client("admin").post("/api/auth/login", json={"login": "admin", "password": "adminpass123",
                                                             "tab": "student"}).json()["redirect"] == "/admin"


def test_demo_students_can_log_in_with_user_id():
    from app.config import Settings
    from tests.conftest import Env
    env = Env(Settings(admin_password="adminpass123", demo_password="testpass5678"))
    c = env.client()
    r = c.post("/api/auth/login", json={"login": "user1", "password": "testpass5678", "tab": "student"})
    assert r.status_code == 200 and r.json()["redirect"] == "/passenger"
    r = c.post("/api/auth/login", json={"login": "20260001@kangwon.ac.kr", "password": "testpass5678", "tab": "student"})
    assert r.status_code == 200                                    # 학교 이메일도 그대로 된다
    d = env.client("driver")
    assert d.post("/api/auth/login", json={"login": "driver2", "password": "testpass5678", "tab": "driver"}).status_code == 200


def test_old_v2_database_is_migrated_in_place(tmp_path):
    import sqlite3
    from app.db import Database
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, password_hash TEXT,
            role TEXT NOT NULL DEFAULT 'passenger', nickname TEXT NOT NULL, gender TEXT, penalty INTEGER NOT NULL DEFAULT 0,
            is_demo INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, origin TEXT NOT NULL,
            dest TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL, seats INTEGER NOT NULL DEFAULT 1,
            detour_ratio REAL NOT NULL, status TEXT NOT NULL, solo_m REAL NOT NULL, solo_fare INTEGER NOT NULL,
            solo_duration_s REAL NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO users (email, role, nickname, created_at) VALUES ('admin@kangwon.ac.kr', 'admin', '운영자', 'x');
        INSERT INTO users (email, role, nickname, created_at) VALUES ('testdriver@moata.kr', 'driver', '박시험', 'x');
        INSERT INTO users (email, role, nickname, gender, created_at) VALUES ('2026@kangwon.ac.kr', 'passenger', '주현', 'F', 'x');
        PRAGMA user_version = 2;""")
    c.close()
    db = Database(path)
    rows = {r["nickname"]: r for r in db.all("SELECT * FROM users")}
    assert rows["운영자"]["login_id"] == "admin" and rows["운영자"]["email"] is None
    assert rows["박시험"]["login_id"] == "testdriver"
    assert rows["주현"]["email"] == "2026@kangwon.ac.kr"           # 학생은 그대로
    assert path.exists() and db.scalar("PRAGMA user_version") == 4
    assert rows["주현"]["trust"] == 40 and rows["주현"]["points"] == 500000   # 버전 4: 신뢰도·포인트


def test_role_permissions_are_checked_on_server(env):
    a = env.student("가람")
    assert a.get("/api/admin/overview").status_code == 401        # 승객 쿠키로는 운영 API를 못 쓴다
    assert a.get("/api/driver/state").status_code == 401
    # 승객 세션 토큰을 운영 쿠키 이름으로 바꿔 끼워도 세션의 앱이 달라 거부된다
    token = a.cookies.get("moata_passenger")
    fake = env.client("admin")
    fake.cookies.set("moata_admin", token)
    assert fake.get("/api/admin/overview").status_code == 401
    assert env.admin().get("/api/admin/overview").status_code == 200
    assert env.client().get("/api/me/state").status_code == 401   # 로그인 안 함


def test_other_users_group_is_forbidden(env):
    a, b, c = env.student("별"), env.student("솔"), env.student("달")
    ask(a, GATE, STATION)
    ask(b, GATE_2, TERMINAL)
    gid = state(a)["active"]["group"]["id"]
    assert c.get(f"/api/groups/{gid}").status_code == 403
    rb = state(b)["active"]["request"]["id"]
    assert a.post(f"/api/groups/{gid}/accept", json={"request_id": rb}).status_code == 403   # 남의 요청


def test_gender_cannot_be_changed_by_user_but_admin_can(env):
    a = env.student("가람", "F")
    r = a.patch("/api/me", json={"gender": "M"})
    assert r.status_code == 403
    assert a.patch("/api/me", json={"nickname": "새이름"}).json()["nickname"] == "새이름"
    adm = env.admin()
    r = adm.patch(f"/api/admin/users/{a.user['id']}/gender", json={"gender": "M"})
    assert r.status_code == 200 and r.json()["gender"] == "M"


def test_logout_ends_session(env):
    a = env.student("가람")
    assert a.get("/api/auth/me").status_code == 200
    a.post("/api/auth/logout")
    assert a.get("/api/auth/me").status_code == 401


def test_genders_never_grouped_even_when_everything_else_matches(env):
    f, m = env.student("가람", "F"), env.student("한결", "M")
    ask(f, GATE, STATION)
    ask(m, GATE, STATION)
    res = env.svc.run_matching()
    assert res["created"] == []
    assert state(f)["active"]["request"]["status"] == "waiting"
