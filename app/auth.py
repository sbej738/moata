"""계정: 비밀번호 해시(pbkdf2, 표준 라이브러리), 이메일 형식 확인, 세션 토큰.

- 비밀번호는 'pbkdf2_sha256$반복수$솔트$해시' 형태로만 저장한다.
- 로그인 유지는 앱별 HttpOnly 쿠키(moata_passenger / moata_driver / moata_admin)로 한다.
  같은 브라우저에서 승객 앱과 기사님 앱을 동시에 열어 시연할 수 있게 앱마다 쿠키를 나눴다.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

ITERATIONS = 200_000
APPS = ("passenger", "driver", "admin")
ROLE_HOME = {"passenger": "/passenger", "driver": "/driver", "admin": "/admin"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
LOGIN_ID_RE = re.compile(r"^[a-z0-9_]{4,20}$")


def normalize_login_id(login_id: str) -> str:
    return login_id.strip().lower()


def login_id_ok(login_id: str) -> bool:
    """기사님·운영자 아이디: 영문 소문자·숫자·밑줄 4~20자."""
    return bool(LOGIN_ID_RE.match(login_id))


def cookie_name(app: str) -> str:
    return f"moata_{app}"


def hash_password(password: str, iterations: int | None = None) -> str:
    iterations = iterations or ITERATIONS
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iters, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters)).hex()
    return hmac.compare_digest(check, digest)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def email_ok(email: str) -> bool:
    return bool(EMAIL_RE.match(email))


def student_email_ok(email: str, domain: str) -> bool:
    """학교 이메일은 형식만 확인한다(인증 메일은 보내지 않는다)."""
    return email_ok(email) and email.endswith("@" + domain.lower())


def password_problem(password: str) -> str | None:
    if len(password) < 8:
        return "비밀번호는 8자 이상이어야 해요"
    if not (re.search(r"[A-Za-z]", password) and re.search(r"\d", password)):
        return "비밀번호에 영문과 숫자를 함께 넣어 주세요"
    return None
