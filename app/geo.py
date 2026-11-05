"""좌표 계산 도우미: 직선거리, 방향 벡터, 코사인 유사도."""
from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class Point:
    lat: float
    lng: float
    name: str = ""

    def key(self) -> tuple[float, float]:
        return (round(self.lat, 5), round(self.lng, 5))

    def to_dict(self) -> dict:
        return {"lat": self.lat, "lng": self.lng, "name": self.name}

    @staticmethod
    def from_dict(d: dict) -> "Point":
        return Point(float(d["lat"]), float(d["lng"]), d.get("name", ""))


def haversine_m(a: Point, b: Point) -> float:
    """두 좌표 사이의 대원거리(m)."""
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = p2 - p1
    dl = math.radians(b.lng - a.lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def vector_m(a: Point, b: Point) -> tuple[float, float]:
    """a → b 이동을 평면(동쪽 x, 북쪽 y, 단위 m)으로 근사한 벡터."""
    mean_lat = math.radians((a.lat + b.lat) / 2)
    dx = math.radians(b.lng - a.lng) * EARTH_RADIUS_M * math.cos(mean_lat)
    dy = math.radians(b.lat - a.lat) * EARTH_RADIUS_M
    return dx, dy


def direction_cos(a_from: Point, a_to: Point, b_from: Point, b_to: Point) -> float:
    """두 이동(출발→도착) 방향의 코사인 유사도. 이동 거리가 0이면 1로 본다."""
    ax, ay = vector_m(a_from, a_to)
    bx, by = vector_m(b_from, b_to)
    na, nb = math.hypot(ax, ay), math.hypot(bx, by)
    if na < 1 or nb < 1:
        return 1.0
    return (ax * bx + ay * by) / (na * nb)
