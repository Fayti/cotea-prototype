"""Calcul des trajets : route réelle (OSRM) ou arc de grand cercle pour l'avion."""

import math
from typing import Iterator

import requests

EARTH_M = 6_371_000.0

# Vitesses par défaut en km/h.
MODES = {"marche": 5.0, "velo": 16.0, "voiture": 50.0, "avion": 850.0}
OSRM = {
    "marche": "https://routing.openstreetmap.de/routed-foot/route/v1/driving",
    "velo": "https://routing.openstreetmap.de/routed-bike/route/v1/driving",
    "voiture": "https://routing.openstreetmap.de/routed-car/route/v1/driving",
}

Point = tuple[float, float]


def haversine(a: Point, b: Point) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_M * math.asin(min(1.0, math.sqrt(h)))


def great_circle(a: Point, b: Point, steps: int) -> list[Point]:
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = haversine(a, b) / EARTH_M
    if d == 0:
        return [a, b]
    points = []
    for i in range(steps + 1):
        f = i / steps
        A = math.sin((1 - f) * d) / math.sin(d)
        B = math.sin(f * d) / math.sin(d)
        x = A * math.cos(lat1) * math.cos(lng1) + B * math.cos(lat2) * math.cos(lng2)
        y = A * math.cos(lat1) * math.sin(lng1) + B * math.cos(lat2) * math.sin(lng2)
        z = A * math.sin(lat1) + B * math.sin(lat2)
        points.append((math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))))
    return points


def road_path(a: Point, b: Point, mode: str) -> list[Point]:
    """Géométrie routière ; ligne droite si le service de routage ne répond pas."""
    if mode in OSRM:
        try:
            url = f"{OSRM[mode]}/{a[1]},{a[0]};{b[1]},{b[0]}?overview=full&geometries=geojson"
            resp = requests.get(url, timeout=10, headers={"User-Agent": "GhostLoc/1.0"})
            coords = resp.json()["routes"][0]["geometry"]["coordinates"]
            if len(coords) >= 2:
                return [a] + [(lat, lng) for lng, lat in coords] + [b]
        except Exception:
            pass
    return great_circle(a, b, max(2, int(haversine(a, b) / 20_000)))


def path_length(path: list[Point]) -> float:
    return sum(haversine(path[i], path[i + 1]) for i in range(len(path) - 1))


def walk(path: list[Point], speed_kmh: float, tick_s: float = 1.0) -> Iterator[tuple[Point, float]]:
    """Positions successives le long du trajet, une par tick, avec la distance parcourue."""
    step = speed_kmh / 3.6 * tick_s
    travelled = 0.0
    seg = 0
    seg_pos = 0.0
    yield path[0], 0.0
    while seg < len(path) - 1:
        remaining = step
        while remaining > 0 and seg < len(path) - 1:
            seg_len = haversine(path[seg], path[seg + 1])
            left = seg_len - seg_pos
            if remaining < left:
                seg_pos += remaining
                travelled += remaining
                remaining = 0
            else:
                travelled += left
                remaining -= left
                seg += 1
                seg_pos = 0.0
        if seg >= len(path) - 1:
            yield path[-1], travelled
            return
        a, b = path[seg], path[seg + 1]
        seg_len = haversine(a, b) or 1.0
        f = seg_pos / seg_len
        yield (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f), travelled
