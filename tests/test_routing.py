"""카카오 길찾기 응답 파싱을 가짜 응답으로 확인한다(실제 네트워크 호출 없음)."""
import httpx

from app.config import Settings
from app.geo import Point
from app.routing import KAKAO_BASE, EstimateRouteProvider, KakaoRouteProvider, estimate_taxi_fare

A = Point(37.8690, 127.7440, "A")
A2 = Point(37.86905, 127.74405, "A 바로 옆")  # 30m 안이라 하나로 합쳐진다
B = Point(37.8545, 127.7450, "B")
C = Point(37.8510, 127.7330, "C")


def fake_kakao(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == "KakaoAK test-key"
        return httpx.Response(200, json={
            "trans_id": "x",
            "routes": [{
                "result_code": 0, "result_msg": "길찾기 성공",
                "summary": {"distance": 3600, "duration": 600, "fare": {"taxi": 6900, "toll": 0}},
                "sections": [
                    {"distance": 2100, "duration": 350, "roads": [{"vertexes": [127.744, 37.869, 127.745, 37.8545]}]},
                    {"distance": 1500, "duration": 250, "roads": [{"vertexes": [127.745, 37.8545, 127.733, 37.851]}]},
                ],
            }],
        })
    return handler


def test_kakao_waypoints_parsing_and_point_merge():
    seen = []
    client = httpx.Client(base_url=KAKAO_BASE, transport=httpx.MockTransport(fake_kakao(seen)))
    p = KakaoRouteProvider("test-key", Settings(), client=client)
    r = p.route([A, A2, B, C])
    assert r.provider == "kakao"
    assert r.legs_m == [0.0, 2100.0, 1500.0]  # A와 A2 사이는 합쳐져 0m
    assert r.total_m == 3600 and r.taxi_fare == 6900
    assert r.polyline[0] == [37.869, 127.744]
    req = seen[0]
    assert req.url.path == "/v1/waypoints/directions"
    assert b'"waypoints"' in req.content


def test_kakao_failure_falls_back_to_estimate():
    def boom(request):
        return httpx.Response(401, json={"msg": "unauthorized"})
    client = httpx.Client(base_url=KAKAO_BASE, transport=httpx.MockTransport(boom))
    r = KakaoRouteProvider("test-key", Settings(), client=client).route([A, B])
    assert r.provider == "estimate(fallback)"
    assert r.total_m > 0


def test_estimate_fare_follows_table():
    cfg = Settings()
    assert estimate_taxi_fare(1000, 0, cfg.fare) == cfg.fare.base_fare
    longer = estimate_taxi_fare(5000, 0, cfg.fare)
    assert longer > cfg.fare.base_fare
    r = EstimateRouteProvider(cfg).route([A, B])
    assert r.taxi_fare >= cfg.fare.base_fare
