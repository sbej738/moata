"""FastAPI 진입점. 입력 검증(pydantic)과 로그인·역할 확인만 하고, 업무 규칙은 service에 둔다.

실행:  uvicorn app.main:app --reload
화면:  /           로그인(학생·기사님·운영자)
       /passenger  승객 앱(PWA)
       /driver     기사님 앱(PWA)
       /admin      운영 페이지(PC)
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
from .config import BASE_DIR, Settings, settings
from .db import Database
from .demo_places import DEMO_CENTER, DEMO_PLACES
from .geo import Point
from .safety import REPORT_TABS
from .service import ServiceError, TaxiShareService

log = logging.getLogger("moata")
STATIC_DIR = BASE_DIR / "static"
APP_HEADER = "X-Moata-App"


# --- 요청 본문 스키마 -------------------------------------------------------
class PointIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    name: str = ""

    def to_point(self) -> Point:
        return Point(self.lat, self.lng, self.name)


class SignupIn(BaseModel):
    email: str
    password: str
    nickname: str
    gender: Literal["F", "M"]          # 기본값 없음: 빠지면 422


class DriverSignupIn(BaseModel):
    login_id: str                      # 기사님은 이메일이 아니라 아이디로 가입·로그인
    password: str
    name: str
    car_number: str
    car_model: str = ""
    car_color: str = ""


class LoginIn(BaseModel):
    login: str                         # 학생은 학교 이메일, 기사님·운영자는 아이디
    password: str
    tab: Literal["student", "driver"] = "student"


class ProfileIn(BaseModel):
    nickname: str | None = None
    gender: Literal["F", "M"] | None = None


class QuoteIn(BaseModel):
    origin: PointIn
    dest: PointIn


class RideRequestIn(BaseModel):
    type: Literal["quick", "reserved"] = "quick"
    origin: PointIn
    dest: PointIn
    window_start: str | None = None
    window_end: str | None = None
    seats: int = 1
    detour_ratio: float | None = None
    detour_min: int | None = Field(None, ge=1, le=60)   # 우회 허용(분). 있으면 detour_ratio 대신 쓴다
    fallback: Literal["solo", "cancel"] = "solo"
    companion_same_gender: bool = False


class MemberAction(BaseModel):
    request_id: int


class FareIn(BaseModel):
    final_fare: int | None = None
    toll: int = 0
    toll_leg: int | None = None


class ReportIn(BaseModel):
    group_id: int | None = None
    kind: str
    category: str
    note: str = Field("", max_length=500)
    target_request_id: int | None = None
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)


class AvailabilityIn(BaseModel):
    available: bool
    lat: float | None = None
    lng: float | None = None


class LocationIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class SettingsIn(BaseModel):
    auto_match: bool


class MatchIn(BaseModel):
    use_ai: bool = True


class ReportStatusIn(BaseModel):
    status: Literal["received", "in_progress", "resolved"]


class GenderIn(BaseModel):
    gender: Literal["F", "M"]


class SeedIn(BaseModel):
    count: int = Field(4, ge=1, le=12)
    gender: Literal["F", "M"] = "F"
    type: Literal["quick", "reserved"] = "quick"
    origin: PointIn | None = None
    dest: PointIn | None = None


# --- 앱 생성 ---------------------------------------------------------------
def create_app(cfg: Settings = settings, db_path: str | Path | None = None, start_matcher: bool = True,
               service: TaxiShareService | None = None) -> FastAPI:
    svc = service or TaxiShareService(Database(db_path or cfg.db_path), cfg)
    cfg = svc.cfg
    try:
        made = svc.seed_accounts()
        if any(made.values()):
            log.info("계정을 만들었습니다: %s (비밀번호는 .env 값)", made)
    except ServiceError as exc:
        log.warning("계정 준비 실패: %s", exc.message)

    def periodic():
        result = svc.run_matching(batch=svc.auto_match, trigger="periodic")
        if result["created"] or result["solo"] or result["expired"] or result["dissolved"]:
            log.info("주기 작업: 새 그룹 %s, 단독 %s, 만료 %s, 해산 %s", result["created"], result["solo"],
                     result["expired"], result["dissolved"])
        svc.dispatch_cycle()
        svc.summarize_ops()

    async def matcher_loop():
        while True:
            await asyncio.sleep(cfg.match_interval_sec)
            try:
                await asyncio.to_thread(periodic)
            except Exception:  # 주기 작업이 죽지 않도록
                log.exception("주기 작업 실패")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(matcher_loop()) if start_matcher else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="모아타(MOATA) 택시 동승", lifespan=lifespan)
    app.state.service = svc

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    # --- 로그인 확인 -------------------------------------------------------
    def session_for(request: Request, app_name: str) -> dict | None:
        return svc.session_user(request.cookies.get(auth.cookie_name(app_name)), app_name)

    def need(app_name: str, role: str):
        def dep(request: Request) -> dict:
            user = session_for(request, app_name)
            if not user:
                raise ServiceError("로그인이 필요해요", 401)
            if user["role"] != role:
                raise ServiceError("이 화면을 쓸 권한이 없어요", 403)
            return user
        return dep

    passenger = need("passenger", "passenger")
    driver = need("driver", "driver")
    admin = need("admin", "admin")

    def any_user(request: Request) -> tuple[dict, str]:
        app_name = request.headers.get(APP_HEADER, "passenger")
        if app_name not in auth.APPS:
            raise ServiceError("앱 이름이 올바르지 않아요", 400)
        user = session_for(request, app_name)
        if not user:
            raise ServiceError("로그인이 필요해요", 401)
        return user, app_name

    def set_session(response: Response, user_id: int, app_name: str) -> None:
        token = svc.create_session(user_id, app_name)
        response.set_cookie(auth.cookie_name(app_name), token, max_age=cfg.session_days * 86400, httponly=True,
                            samesite="lax", secure=cfg.cookie_secure, path="/")

    def auto_match_soon(background: BackgroundTasks, trigger: str) -> None:
        if svc.auto_match:
            background.add_task(svc.run_matching, True, None, trigger)

    # --- 공통 --------------------------------------------------------------
    @app.get("/api/health")
    def health():
        return {"ok": True, "db": svc.db.scalar("SELECT 1") == 1, "time": svc._stamp(),
                "ai_providers": [p["provider"] for p in svc.router.status()]}

    @app.get("/api/config")
    def get_config():
        return {
            "app_name": "모아타", "app_name_en": "MOATA",
            "map_provider": cfg.map_provider, "route_provider": cfg.route_provider, "kakao_js_key": cfg.kakao_js_key,
            "ai_enabled": svc.router.enabled, "ai_providers": [{"provider": p["provider"], "model": p["model"]}
                                                               for p in svc.router.status()],
            "demo_center": DEMO_CENTER, "demo_places": DEMO_PLACES, "region": "삼척시",
            "report_tabs": REPORT_TABS, "demo_tools": cfg.demo_tools,
            "student_email_domain": cfg.student_email_domain,
            "params": {
                "capacity": cfg.capacity, "min_overlap_min": cfg.min_overlap_min,
                "origin_radius_m": cfg.origin_radius_m, "min_direction_cos": cfg.min_direction_cos,
                "default_detour_ratio": cfg.default_detour_ratio, "same_gender_only": True,
                "match_interval_sec": cfg.match_interval_sec, "deadline_min": cfg.deadline_min,
                "quick_window_min": cfg.quick_window_min, "reserve_min_lead_min": cfg.reserve_min_lead_min,
                "reserve_max_days": cfg.reserve_max_days, "min_discount": cfg.min_discount,
                "avg_speed_kmh": cfg.avg_speed_kmh, "dispatch_offer_sec": cfg.dispatch_offer_sec,
                "dispatch_lead_min": cfg.dispatch_lead_min, "default_detour_min": cfg.default_detour_min,
                "trust_low": cfg.trust_low, "trust_low_surcharge": cfg.trust_low_surcharge,
                "trust_low_min_discount": cfg.trust_low_min_discount, "deposit_ratio": cfg.deposit_ratio,
                "no_show_wait_min": cfg.no_show_wait_min, "late_cancel_min": cfg.late_cancel_min,
            },
            "now": svc.now().strftime("%Y-%m-%dT%H:%M"),
        }

    # --- 계정 --------------------------------------------------------------
    @app.post("/api/auth/signup")
    def signup(body: SignupIn, response: Response):
        user = svc.signup_student(body.email, body.password, body.nickname, body.gender)
        set_session(response, user["id"], "passenger")
        return {"user": user, "app": "passenger", "redirect": auth.ROLE_HOME["passenger"]}

    @app.post("/api/auth/signup/driver")
    def signup_driver(body: DriverSignupIn, response: Response):
        user = svc.signup_driver(body.login_id, body.password, body.name, body.car_number, body.car_model, body.car_color)
        set_session(response, user["id"], "driver")
        return {"user": user, "app": "driver", "redirect": auth.ROLE_HOME["driver"]}

    @app.post("/api/auth/login")
    def login(body: LoginIn, response: Response):
        user, app_name = svc.login(body.login, body.password, body.tab)
        set_session(response, user["id"], app_name)
        return {"user": user, "app": app_name, "redirect": auth.ROLE_HOME[app_name]}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        app_name = request.headers.get(APP_HEADER, "passenger")
        if app_name in auth.APPS:
            svc.logout(request.cookies.get(auth.cookie_name(app_name)))
            response.delete_cookie(auth.cookie_name(app_name), path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(ctx=Depends(any_user)):
        user, app_name = ctx
        return {"user": svc.me(user), "app": app_name}

    @app.patch("/api/me")
    def update_me(body: ProfileIn, user=Depends(passenger)):
        return svc.update_profile(user, body.nickname, body.gender)

    # --- 승객 --------------------------------------------------------------
    @app.post("/api/quote")
    def quote(body: QuoteIn, _=Depends(any_user)):
        return svc.quote(body.origin.to_point(), body.dest.to_point())

    @app.get("/api/me/state")
    def my_state(user=Depends(passenger)):
        return svc.user_state(user["id"])

    @app.get("/api/me/wallet")
    def my_wallet(user=Depends(passenger)):
        return svc.wallet(user)

    @app.post("/api/me/points/topup")
    def topup(user=Depends(passenger)):
        if not cfg.demo_tools:
            raise ServiceError("데모 충전이 꺼져 있어요", 403)
        return svc.topup(user, 100000)

    @app.post("/api/requests")
    def create_request(body: RideRequestIn, background: BackgroundTasks, user=Depends(passenger)):
        r = svc.create_request(user["id"], body.origin.to_point(), body.dest.to_point(), body.window_start,
                               body.window_end, body.seats, body.detour_ratio, body.fallback, body.type,
                               body.companion_same_gender, body.detour_min)
        if body.type == "quick":
            auto_match_soon(background, "quick_request")
        return r

    @app.get("/api/requests/{request_id}/prediction")
    def predict_request(request_id: int, user=Depends(passenger)):
        svc.own_request(user, request_id)
        return svc.predict_request(request_id)

    @app.post("/api/requests/{request_id}/cancel")
    def cancel_request(request_id: int, user=Depends(passenger)):
        svc.own_request(user, request_id)
        return svc.cancel_request(request_id)

    @app.post("/api/requests/{request_id}/notice-ack")
    def notice_ack(request_id: int, user=Depends(passenger)):
        return svc.ack_notice(user, request_id)

    @app.post("/api/requests/{request_id}/paid")
    def paid(request_id: int, user=Depends(passenger)):
        return svc.mark_paid(user, request_id)

    @app.get("/api/groups/{group_id}")
    def get_group(group_id: int, ctx=Depends(any_user)):
        user, app_name = ctx
        g = svc.get_group_row(group_id)
        if app_name == "admin":
            return svc.group_view(group_id)
        if app_name == "driver":
            if user["id"] not in (g["driver_id"], g["offer_driver_id"]):
                raise ServiceError("내 운행이 아니에요", 403)
            return svc.group_view(group_id, driver_view=True)
        mine = [rid for rid in g["member_ids"] if svc.get_request(rid)["user_id"] == user["id"]]
        if not mine:
            raise ServiceError("내 운행이 아니에요", 403)
        return svc.group_view(group_id, viewer_request_id=mine[0])

    @app.post("/api/groups/{group_id}/accept")
    def accept(group_id: int, body: MemberAction, user=Depends(passenger)):
        r = svc.own_request(user, body.request_id)
        svc.accept(group_id, r["id"])
        return svc.group_view(group_id, viewer_request_id=r["id"])

    @app.post("/api/groups/{group_id}/reject")
    def reject(group_id: int, body: MemberAction, background: BackgroundTasks, user=Depends(passenger)):
        r = svc.own_request(user, body.request_id)
        out = svc.reject(group_id, r["id"])
        auto_match_soon(background, "reject")
        return out

    @app.post("/api/demo/seed-near")
    def seed_near(background: BackgroundTasks, user=Depends(passenger)):
        if not cfg.demo_tools:
            raise ServiceError("시연 도구가 꺼져 있어요", 403)
        created = svc.seed_near(user, 2)
        auto_match_soon(background, "demo")
        return {"created": len(created)}

    # --- 신고(승객·기사님) -------------------------------------------------
    @app.post("/api/reports")
    def report(body: ReportIn, ctx=Depends(any_user)):
        user, _ = ctx
        return svc.report(user, body.group_id, body.kind, body.category, body.note, body.target_request_id,
                          body.lat, body.lng)

    # --- 기사님 ------------------------------------------------------------
    @app.get("/api/driver/state")
    def driver_state(user=Depends(driver)):
        return svc.driver_state(user)

    @app.post("/api/driver/availability")
    def availability(body: AvailabilityIn, user=Depends(driver)):
        return svc.set_available(user, body.available, body.lat, body.lng)

    @app.post("/api/driver/location")
    def location(body: LocationIn, user=Depends(driver)):
        return svc.update_location(user, body.lat, body.lng)

    @app.post("/api/driver/offers/{group_id}/accept")
    def accept_offer(group_id: int, user=Depends(driver)):
        return svc.accept_offer(user, group_id)

    @app.post("/api/driver/offers/{group_id}/decline")
    def decline_offer(group_id: int, user=Depends(driver)):
        return svc.decline_offer(user, group_id)

    @app.post("/api/driver/groups/{group_id}/start")
    def start_trip(group_id: int, user=Depends(driver)):
        return svc.start_trip(user, group_id)

    @app.post("/api/driver/groups/{group_id}/arrive")
    def arrive_pickup(group_id: int, user=Depends(driver)):
        return svc.arrive_pickup(user, group_id)

    @app.post("/api/driver/groups/{group_id}/no-show")
    def no_show(group_id: int, user=Depends(driver)):
        return svc.no_show(user, group_id)

    @app.post("/api/driver/groups/{group_id}/advance")
    def advance(group_id: int, user=Depends(driver)):
        return svc.advance(user, group_id)

    @app.post("/api/driver/groups/{group_id}/arriving")
    def arriving(group_id: int, user=Depends(driver)):
        return svc.notify_arriving(user, group_id)

    @app.post("/api/driver/groups/{group_id}/preview")
    def preview(group_id: int, body: FareIn, user=Depends(driver)):
        return svc.fare_preview(user, group_id, body.final_fare, body.toll, body.toll_leg)

    @app.post("/api/driver/groups/{group_id}/complete")
    def complete(group_id: int, body: FareIn, user=Depends(driver)):
        return svc.complete(user, group_id, body.final_fare, body.toll, body.toll_leg)

    # --- 운영 --------------------------------------------------------------
    @app.get("/api/admin/overview")
    def admin_overview(_=Depends(admin)):
        return svc.admin_overview()

    @app.post("/api/admin/match")
    def admin_match(body: MatchIn | None = None, _=Depends(admin)):
        return svc.run_matching(batch=True, use_ai=(body.use_ai if body else True), trigger="manual")

    @app.post("/api/admin/dispatch")
    def admin_dispatch(_=Depends(admin)):
        return svc.dispatch_cycle()

    @app.post("/api/admin/settings")
    def admin_settings(body: SettingsIn, _=Depends(admin)):
        svc.set_setting("ai_auto_match", body.auto_match)
        return {"auto_match": svc.auto_match}

    @app.post("/api/admin/alerts/{alert_id}/ack")
    def ack_alert(alert_id: int, user=Depends(admin)):
        return svc.ack_alert(alert_id, user)

    @app.post("/api/admin/reports/{report_id}")
    def report_status(report_id: int, body: ReportStatusIn, _=Depends(admin)):
        return svc.update_report(report_id, body.status)

    @app.post("/api/admin/reports/{report_id}/confirm")
    def confirm_report(report_id: int, _=Depends(admin)):
        return svc.confirm_report(report_id)

    @app.get("/api/admin/users")
    def admin_users(_=Depends(admin)):
        return svc.list_users()

    @app.patch("/api/admin/users/{user_id}/gender")
    def admin_gender(user_id: int, body: GenderIn, _=Depends(admin)):
        return svc.admin_set_gender(user_id, body.gender)

    @app.post("/api/admin/summary")
    def admin_summary(_=Depends(admin)):
        out = svc.summarize_ops(force=True)
        if out is None:
            raise ServiceError("AI 키가 없어 요약을 만들 수 없어요", 409)
        return out

    @app.get("/api/admin/ai")
    def admin_ai(_=Depends(admin)):
        return {"enabled": svc.router.enabled, "providers": svc.router.status(), "auto_match": svc.auto_match}

    @app.post("/api/admin/demo/seed")
    def admin_seed(body: SeedIn, background: BackgroundTasks, _=Depends(admin)):
        created = svc.seed_demo(body.count, body.gender, body.origin.to_point() if body.origin else None,
                                body.dest.to_point() if body.dest else None, type=body.type)
        if body.type == "quick":
            auto_match_soon(background, "demo")
        return {"created": len(created)}

    @app.post("/api/admin/demo/reset")
    def admin_reset(_=Depends(admin)):
        svc.reset()
        return {"ok": True}

    # --- 화면 · PWA --------------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def no_stale_static(request, call_next):
        """화면 파일(JS·CSS)은 매번 바뀌었는지 확인하게 한다. 안 바뀌었으면 304라 가볍고,
        고친 코드가 일반 새로고침만으로 반영된다(브라우저가 예전 파일을 그대로 쓰지 않게)."""
        response = await call_next(request)
        if request.url.path.startswith("/static/") and "/vendor/" not in request.url.path:
            response.headers["Cache-Control"] = "no-cache"
        return response

    def page(name: str):
        return FileResponse(STATIC_DIR / name, headers={"Cache-Control": "no-cache"})

    @app.get("/")
    def index():
        return page("login.html")

    @app.get("/passenger")
    def passenger_page():
        return page("passenger.html")

    @app.get("/driver")
    def driver_page():
        return page("driver.html")

    @app.get("/admin")
    def admin_page():
        return page("admin.html")

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript",
                            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
