"""SQLite 저장소. MVP라 ORM 없이 sqlite3만 쓴다(실서비스는 PostgreSQL + PostGIS 권장)."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

log = logging.getLogger("moata.db")

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE,                 -- 학생 로그인용. 데모 동승자·기사님·운영자는 비어 있다
    login_id      TEXT,                        -- 기사님·운영자 로그인 아이디
    password_hash TEXT,
    role          TEXT NOT NULL DEFAULT 'passenger' CHECK (role IN ('passenger', 'driver', 'admin')),
    nickname      TEXT NOT NULL,
    gender        TEXT CHECK (gender IS NULL OR gender IN ('F', 'M')),
    penalty       INTEGER NOT NULL DEFAULT 0,
    trust         INTEGER NOT NULL DEFAULT 40,     -- 개인 신뢰도 0~100
    points        INTEGER NOT NULL DEFAULT 0,      -- 모아타 포인트(가상 금액) 쓸 수 있는 잔액
    rides_done    INTEGER NOT NULL DEFAULT 0,      -- 문제없이 끝낸 운행 수
    is_demo       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    CHECK (role != 'passenger' OR gender IS NOT NULL)   -- 승객은 성별 필수(동성 매칭)
);

CREATE TABLE IF NOT EXISTS trust_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    delta       INTEGER NOT NULL,
    trust_after INTEGER NOT NULL,
    kind        TEXT NOT NULL,      -- ride / recovery / milestone / late_cancel / no_show / report / join
    reason      TEXT NOT NULL DEFAULT '',
    group_id    INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_tx (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    amount        INTEGER NOT NULL,  -- 잔액 기준 +/-
    kind          TEXT NOT NULL,     -- grant / topup / hold / release / pay / comp_in / comp_out
    balance_after INTEGER NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    group_id      INTEGER,
    request_id    INTEGER,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drivers (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id),
    name        TEXT NOT NULL,
    car_number  TEXT NOT NULL,
    car_model   TEXT NOT NULL DEFAULT '',
    car_color   TEXT NOT NULL DEFAULT '',
    available   INTEGER NOT NULL DEFAULT 0,      -- 운행 가능 스위치
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    app         TEXT NOT NULL,                    -- passenger | driver | admin
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    type            TEXT NOT NULL DEFAULT 'quick' CHECK (type IN ('quick', 'reserved')),
    origin          TEXT NOT NULL,      -- {"lat","lng","name"}
    dest            TEXT NOT NULL,
    window_start    TEXT NOT NULL,      -- 'YYYY-MM-DDTHH:MM' (현지 시각)
    window_end      TEXT NOT NULL,
    seats           INTEGER NOT NULL DEFAULT 1,
    detour_ratio    REAL NOT NULL,
    detour_min      INTEGER,            -- 승객이 고른 우회 허용(분). detour_ratio는 이 값을 단독 소요 시간으로 나눈 값
    fallback        TEXT NOT NULL DEFAULT 'solo' CHECK (fallback IN ('solo', 'cancel')),
    status          TEXT NOT NULL,      -- waiting/proposed/confirmed/riding/dropped/done/cancelled/expired
    group_id        INTEGER,
    solo_m          REAL NOT NULL,      -- 길찾기 기준 단독 거리
    solo_fare       INTEGER NOT NULL,   -- 단독 예상 요금
    solo_duration_s REAL NOT NULL,
    excluded        TEXT NOT NULL DEFAULT '[]',  -- 거절해서 다시 묶지 않을 요청 id
    note            TEXT NOT NULL DEFAULT '',
    notice_ack_at   TEXT,               -- 탑승 전 신고 안내 확인 시각
    hold_amount     INTEGER NOT NULL DEFAULT 0,  -- 잡아 둔 예약금(포인트)
    base_share      INTEGER,            -- 수락할 때의 예상 몫(남이 빠져도 이 금액 기준으로 보상)
    surcharge_rate  REAL NOT NULL DEFAULT 0,     -- 신뢰도가 낮아 더 내는 비율(수락 때 정함)
    no_show         INTEGER NOT NULL DEFAULT 0,
    paid_at         TEXT,               -- '송금 완료' 표시 시각(실제 결제 없음)
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    status           TEXT NOT NULL,          -- proposed/confirmed/riding/completed/cancelled
    is_solo          INTEGER NOT NULL DEFAULT 0,
    member_ids       TEXT NOT NULL,          -- [request_id, ...]
    accepted         TEXT NOT NULL DEFAULT '[]',
    stops            TEXT NOT NULL,          -- [{"request_id","kind"}, ...]
    legs_m           TEXT NOT NULL,
    total_m          REAL NOT NULL,
    duration_s       REAL NOT NULL,
    est_fare         INTEGER NOT NULL,
    est_toll         INTEGER NOT NULL DEFAULT 0,
    polyline         TEXT NOT NULL DEFAULT '[]',
    provider         TEXT NOT NULL,
    departure        TEXT NOT NULL,
    expires_at       TEXT,                   -- 제안 응답 마감
    progress         INTEGER NOT NULL DEFAULT -1,  -- 마지막으로 도착한 정차 지점 인덱스
    odometer         TEXT NOT NULL DEFAULT '[]',   -- 정차 지점별 누적 주행거리
    final_fare       INTEGER,
    settlement       TEXT,
    driver_id        INTEGER,                -- 배차 수락한 기사(users.id)
    offer_driver_id  INTEGER,                -- 지금 배차를 제안받은 기사
    offer_expires_at TEXT,
    offer_reason     TEXT NOT NULL DEFAULT '',
    offer_source     TEXT NOT NULL DEFAULT '',  -- 배차 이유를 쓴 곳(gemini/groq/rule)
    declined_drivers TEXT NOT NULL DEFAULT '[]',
    confirmed_at     TEXT,
    arriving_at      TEXT,                   -- 기사님이 '곧 도착' 알림을 보낸 시각
    wait_started_at  TEXT,                   -- 기사님이 탑승 지점에 도착해 기다리기 시작한 시각(노쇼 판정)
    wait_request_id  INTEGER,
    match_method     TEXT NOT NULL DEFAULT '',  -- ai/cp-sat/greedy/solo
    match_reason     TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id          INTEGER,
    request_id        INTEGER,
    reporter_id       INTEGER,
    kind              TEXT NOT NULL,      -- emergency/driving/fare/companion
    category          TEXT NOT NULL,
    note              TEXT NOT NULL DEFAULT '',
    target_request_id INTEGER,
    lat               REAL,
    lng               REAL,
    location_source   TEXT NOT NULL DEFAULT '',  -- gps | last_stop | none
    snapshot          TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL DEFAULT 'received' CHECK (status IN ('received', 'in_progress', 'resolved')),
    confirmed         INTEGER NOT NULL DEFAULT 0,   -- 운영자가 사실로 확인(대상 신뢰도 감점)
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('high', 'medium', 'low', 'info')),
    title       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    request_id  INTEGER,
    group_id    INTEGER,
    report_id   INTEGER,
    count       INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    acked_at    TEXT,
    acked_by    INTEGER
);

CREATE TABLE IF NOT EXISTS ai_decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,           -- match | dispatch | summary
    used_ai     INTEGER NOT NULL DEFAULT 0,
    provider    TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    method      TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    output      TEXT NOT NULL DEFAULT '{}',
    result      TEXT NOT NULL DEFAULT '{}',
    error       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login ON users(login_id);
CREATE INDEX IF NOT EXISTS idx_trust_user ON trust_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_wallet_user ON wallet_tx(user_id, id);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id);
CREATE INDEX IF NOT EXISTS idx_groups_status ON groups(status);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(acked_at, severity);
CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_created ON ai_decisions(kind, created_at);
"""

JSON_COLUMNS = {"origin", "dest", "excluded", "member_ids", "accepted", "stops", "legs_m",
                "polyline", "odometer", "settlement", "declined_drivers", "snapshot", "payload", "output", "result"}


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.lock = threading.RLock()  # 매칭·상태 변경을 한 번에 하나씩 처리
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._move_old_schema()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate_v2()
        self._migrate_v3()
        self._conn.executescript(SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

    def _move_old_schema(self) -> None:
        """예전 스키마 파일이면 지우지 않고 '.v1-날짜.bak'으로 옮긴 뒤 새로 만든다."""
        p = Path(self.path)
        if not p.exists():
            return
        conn = sqlite3.connect(self.path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            has_users = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
        finally:
            conn.close()
        if has_users and version < 2:
            backup = p.with_name(f"{p.stem}.v{version or 1}-{datetime.now():%Y%m%d%H%M%S}.bak{p.suffix}")
            p.rename(backup)
            log.warning("예전 DB 스키마를 %s로 옮기고 새로 만듭니다", backup.name)

    def _migrate_v2(self) -> None:
        """버전 2 → 3: 기사님·운영자 아이디(login_id), 우회 허용 분(detour_min) 칸을 더한다. 데이터는 그대로 둔다."""
        c = self._conn
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if not cols or "login_id" in cols:
            return
        c.execute("ALTER TABLE users ADD COLUMN login_id TEXT")
        c.execute("ALTER TABLE requests ADD COLUMN detour_min INTEGER")
        # 기존 기사님은 이메일 앞부분, 운영자는 'admin'을 아이디로 쓰고, 이메일 로그인은 없앤다
        c.execute("UPDATE users SET login_id = lower(substr(email, 1, instr(email, '@') - 1)), email = NULL "
                  "WHERE role = 'driver' AND email IS NOT NULL")
        c.execute("UPDATE users SET login_id = 'admin', email = NULL WHERE role = 'admin'")
        c.commit()
        log.warning("DB를 스키마 버전 3으로 옮겼습니다(기사님·운영자 아이디 로그인)")

    def _migrate_v3(self) -> None:
        """버전 3 → 4: 신뢰도·포인트·예약금·노쇼 칸을 더한다. 기존 승객은 신뢰도 40, 포인트 500,000P로 시작."""
        c = self._conn
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if not cols or "trust" in cols:
            return
        alters = [
            "ALTER TABLE users ADD COLUMN trust INTEGER NOT NULL DEFAULT 40",
            "ALTER TABLE users ADD COLUMN points INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN rides_done INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE requests ADD COLUMN hold_amount INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE requests ADD COLUMN base_share INTEGER",
            "ALTER TABLE requests ADD COLUMN surcharge_rate REAL NOT NULL DEFAULT 0",
            "ALTER TABLE requests ADD COLUMN no_show INTEGER NOT NULL DEFAULT 0",
        ]
        group_cols = {r[1] for r in c.execute("PRAGMA table_info(groups)").fetchall()}
        if group_cols:
            alters += ["ALTER TABLE groups ADD COLUMN wait_started_at TEXT",
                       "ALTER TABLE groups ADD COLUMN wait_request_id INTEGER"]
        report_cols = {r[1] for r in c.execute("PRAGMA table_info(reports)").fetchall()}
        if report_cols:
            alters.append("ALTER TABLE reports ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0")
        for sql in alters:
            c.execute(sql)
        c.execute("UPDATE users SET points = 500000 WHERE role = 'passenger'")
        c.commit()
        log.warning("DB를 스키마 버전 4로 옮겼습니다(신뢰도·포인트)")

    @contextmanager
    def tx(self):
        with self.lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # -- 조회 도우미 -------------------------------------------------------
    @staticmethod
    def decode(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for k in JSON_COLUMNS & d.keys():
            if d[k] is not None:
                d[k] = json.loads(d[k])
        return d

    def one(self, sql: str, *args) -> dict | None:
        with self.lock:
            return self.decode(self._conn.execute(sql, args).fetchone())

    def all(self, sql: str, *args) -> list[dict]:
        with self.lock:
            return [self.decode(r) for r in self._conn.execute(sql, args).fetchall()]

    def scalar(self, sql: str, *args):
        with self.lock:
            row = self._conn.execute(sql, args).fetchone()
            return row[0] if row else None

    def reset(self, keep_accounts: bool = True) -> None:
        """운행 기록을 지운다. keep_accounts면 로그인 계정(데모 동승자 제외)과 기사 정보는 남긴다."""
        with self.tx() as c:
            # 운행 기록을 지우기 전에 잡혀 있던 예약금을 돌려준다
            c.execute("UPDATE users SET points = points + (SELECT COALESCE(SUM(hold_amount), 0) FROM requests "
                      "WHERE requests.user_id = users.id)")
            c.executescript("DELETE FROM reports; DELETE FROM alerts; DELETE FROM ai_decisions; "
                            "DELETE FROM groups; DELETE FROM requests;")
            if keep_accounts:
                # 로그인 없는 데모 동승자는 내역(포인트·신뢰도·세션)부터 지우고 계정을 지운다(외래 키)
                demo = "SELECT id FROM users WHERE is_demo = 1 AND email IS NULL AND login_id IS NULL AND role = 'passenger'"
                for table in ("wallet_tx", "trust_events", "sessions"):
                    c.execute(f"DELETE FROM {table} WHERE user_id IN ({demo})")
                c.execute(f"DELETE FROM users WHERE id IN ({demo})")
                c.execute("UPDATE users SET penalty = 0")
            else:
                c.executescript("DELETE FROM wallet_tx; DELETE FROM trust_events; DELETE FROM sessions; "
                                "DELETE FROM drivers; DELETE FROM users;")
            c.execute("DELETE FROM sqlite_sequence WHERE name IN ('reports','alerts','ai_decisions','groups','requests')")


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)
