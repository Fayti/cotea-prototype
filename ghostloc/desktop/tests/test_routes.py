import pytest

from ghostloc_desktop import routes


def test_haversine_paris_dubai():
    assert 5_200_000 < routes.haversine((48.8584, 2.2945), (25.1972, 55.2744)) < 5_300_000


def test_great_circle_endpoints():
    path = routes.great_circle((48.85, 2.29), (25.19, 55.27), 50)
    assert len(path) == 51
    assert abs(path[0][0] - 48.85) < 1e-6 and abs(path[-1][1] - 55.27) < 1e-6


def test_walk_reaches_destination_at_speed():
    path = [(0.0, 0.0), (0.0, 0.01)]  # ~1,1 km
    steps = list(routes.walk(path, speed_kmh=36.0))  # 10 m/s
    assert steps[0] == ((0.0, 0.0), 0.0)
    assert steps[-1][0] == (0.0, 0.01)
    assert 110 <= len(steps) <= 113
    distances = [d for _, d in steps]
    assert distances == sorted(distances)


def test_road_path_falls_back_to_straight_line(monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(routes.requests, "get", boom)
    path = routes.road_path((43.70, 7.26), (43.74, 7.42), "voiture")
    assert path[0] == pytest.approx((43.70, 7.26)) and path[-1] == pytest.approx((43.74, 7.42))
