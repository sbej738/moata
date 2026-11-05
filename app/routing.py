"""경로·거리·예상 택시요금 계산.

- KakaoRouteProvider: 카카오모빌리티 길찾기 REST API(자동차, 다중 경유지)를 호출한다.
  응답의 구간(section)별 거리와 fare.taxi(예상 택시요금), fare.toll(통행료)을 쓴다.
- EstimateRouteProvider: 키가 없을 때 쓰는 데모용 추정기. 직선거리 × 도로 보정 계수로
  거리를 잡고, 요금표(config.FareTable)로 요금을 계산한다.

매칭 단계의 수많은 후보 비교에는 API를 쓰지 않고 estimate_distance_m()만 쓴다(기획서 4장).
API는 최종 그룹 경로를 확정할 때만 호출한다.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import httpx

from .config import FareTable, Settings, settings
from .geo import Point, haversine_m

log = logging.getLogger("taxi_share.routing")

KAKAO_BASE = "https://apis-navi.kakaomobility.com"
MERGE_RADIUS_M = 30.0  # 이 거리 안의 연속 정차 지점은 하나로 합쳐 API에 보낸다


@dataclass
class RouteResult:
    legs_m: list[float]          # 연속한 두 지점 사이 구간 거리(m), len = len(points) - 1
    total_m: float
    duration_s: float
    taxi_fare: int               # 예상 택시요금(원)
    toll: int = 0                # 예상 통행료(원)
    polyline: list[list[float]] = field(default_factory=list)  # [[lat, lng], ...]
    provider: str = "estimate"

    def to_dict(self) -> dict:
        return {
            "legs_m": [round(x) for x in self.legs_m],
            "total_m": round(self.total_m),
            "duration_s": round(self.duration_s),
            "taxi_fare": self.taxi_fare,
            "toll": self.toll,
            "polyline": self.polyline,
            "provider": self.provider,
        }


def estimate_distance_m(a: Point, b: Point, cfg: Settings = settings) -> float:
    """매칭용 값싼 도로거리 추정치."""
    return haversine_m(a, b) * cfg.road_factor


def estimate_taxi_fare(distance_m: float, duration_s: float, table: FareTable) -> int:
    """요금표로 계산한 예상 미터 요금(100원 단위)."""
    fare = table.base_fare
    extra = distance_m - table.base_distance_m
    if extra > 0:
        fare += math.ceil(extra / table.unit_distance_m) * table.unit_fare
    slow_sec = duration_s * table.slow_ratio
    fare += int(slow_sec // table.time_unit_sec) * table.unit_fare
    return int(round(fare / 100.0) * 100)


class EstimateRouteProvider:
    name = "estimate"

    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg

    def route(self, points: list[Point]) -> RouteResult:
        legs = [estimate_distance_m(a, b, self.cfg) for a, b in zip(points, points[1:])]
        total = sum(legs)
        duration = total / (self.cfg.avg_speed_kmh * 1000 / 3600) if total else 0.0
        fare = estimate_taxi_fare(total, duration, self.cfg.fare) if total else 0
        return RouteResult(
            legs_m=legs,
            total_m=total,
            duration_s=duration,
            taxi_fare=fare,
            polyline=[[p.lat, p.lng] for p in points],
            provider=self.name,
        )


class KakaoApiError(RuntimeError):
    pass


class KakaoRouteProvider:
    """카카오모빌리티 자동차 길찾기 / 다중 경유지 길찾기."""

    name = "kakao"

    def __init__(self, rest_key: str, cfg: Settings = settings, client: httpx.Client | None = None):
        self.cfg = cfg
        self.headers = {"Authorization": f"KakaoAK {rest_key}"}
        self.client = client or httpx.Client(base_url=KAKAO_BASE, timeout=8.0)
        self.fallback = EstimateRouteProvider(cfg)

    # -- 공개 메서드 -------------------------------------------------------
    def route(self, points: list[Point]) -> RouteResult:
        compact, index_map = _merge_close_points(points)
        if len(compact) < 2:
            return RouteResult([0.0] * (len(points) - 1), 0.0, 0.0, 0, polyline=[[p.lat, p.lng] for p in points], provider=self.name)
        try:
            data = self._request(compact)
            return self._parse(data, points, compact, index_map)
        except (httpx.HTTPError, KakaoApiError, KeyError, IndexError, ValueError) as exc:
            log.warning("카카오 길찾기 실패, 추정값으로 대체: %s", exc)
            result = self.fallback.route(points)
            result.provider = "estimate(fallback)"
            return result

    # -- 내부 구현 ---------------------------------------------------------
    def _request(self, pts: list[Point]) -> dict:
        if len(pts) == 2:
            resp = self.client.get(
                "/v1/directions",
                headers=self.headers,
                params={
                    "origin": f"{pts[0].lng},{pts[0].lat}",
                    "destination": f"{pts[1].lng},{pts[1].lat}",
                    "priority": "RECOMMEND",
                    "summary": "false",
                },
            )
        else:
            if len(pts) - 2 > 30:
                raise KakaoApiError("경유지는 최대 30개까지 가능합니다")
            body = {
                "origin": {"x": pts[0].lng, "y": pts[0].lat},
                "destination": {"x": pts[-1].lng, "y": pts[-1].lat},
                "waypoints": [{"x": p.lng, "y": p.lat} for p in pts[1:-1]],
                "priority": "RECOMMEND",
                "summary": False,
            }
            resp = self.client.post("/v1/waypoints/directions", headers=self.headers, json=body)
        resp.raise_for_status()
        return resp.json()

    def _parse(self, data: dict, points: list[Point], compact: list[Point], index_map: list[int]) -> RouteResult:
        route = data["routes"][0]
        if route.get("result_code", 0) != 0:
            raise KakaoApiError(f"{route.get('result_code')}: {route.get('result_msg')}")
        summary = route["summary"]
        sections = route.get("sections", [])
        compact_legs = [float(s["distance"]) for s in sections]
        if len(compact_legs) != len(compact) - 1:
            raise KakaoApiError("구간 수가 경유지 수와 맞지 않습니다")

        # 합쳐서 보낸 지점을 원래 지점 순서로 펼친다(합쳐진 지점 사이 구간은 0m).
        legs: list[float] = []
        for i in range(len(points) - 1):
            a, b = index_map[i], index_map[i + 1]
            legs.append(compact_legs[a] if b == a + 1 else 0.0)

        polyline: list[list[float]] = []
        for s in sections:
            for road in s.get("roads", []):
                v = road.get("vertexes", [])
                for j in range(0, len(v) - 1, 2):
                    polyline.append([v[j + 1], v[j]])  # [lat, lng]
        fare = summary.get("fare", {}) or {}
        return RouteResult(
            legs_m=legs,
            total_m=float(summary["distance"]),
            duration_s=float(summary["duration"]),
            taxi_fare=int(fare.get("taxi", 0)),
            toll=int(fare.get("toll", 0)),
            polyline=polyline or [[p.lat, p.lng] for p in points],
            provider=self.name,
        )


def _merge_close_points(points: list[Point]) -> tuple[list[Point], list[int]]:
    """연속한 지점이 MERGE_RADIUS_M 안이면 하나로 합친다. index_map[i] = 원래 i번 지점의 압축 인덱스."""
    compact: list[Point] = []
    index_map: list[int] = []
    for p in points:
        if compact and haversine_m(compact[-1], p) <= MERGE_RADIUS_M:
            index_map.append(len(compact) - 1)
        else:
            compact.append(p)
            index_map.append(len(compact) - 1)
    return compact, index_map


class CachedProvider:
    """같은 좌표열 요청은 다시 호출하지 않는다(쿼터 절약). API 실패로 대체한 추정값은 캐시하지 않는다."""

    def __init__(self, inner, maxsize: int = 2048):
        self.inner = inner
        self.name = inner.name
        self.maxsize = maxsize
        self._cache: dict[tuple, RouteResult] = {}

    def route(self, points: list[Point]) -> RouteResult:
        key = tuple(p.key() for p in points)
        r = self._cache.get(key)
        if r is None:
            r = self.inner.route(points)
            if "fallback" not in r.provider:
                if len(self._cache) >= self.maxsize:
                    self._cache.pop(next(iter(self._cache)))  # 가장 오래된 항목부터 버린다
                self._cache[key] = r
        # 캐시된 객체가 바깥에서 바뀌지 않도록 복사본을 돌려준다
        return RouteResult(list(r.legs_m), r.total_m, r.duration_s, r.taxi_fare, r.toll, list(r.polyline), r.provider)


def make_provider(cfg: Settings = settings):
    if cfg.kakao_rest_key:
        return CachedProvider(KakaoRouteProvider(cfg.kakao_rest_key, cfg))
    return CachedProvider(EstimateRouteProvider(cfg))
