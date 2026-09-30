"""多船舶巡查 /api/v1/patrol 端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
}
DATELINE_BAND = {
    "type": "Polygon",
    "coordinates": [[[170, -10], [170, 10], [-170, 10], [-170, -10], [170, -10]]],
}


def area(aid="A1", geom=SQUARE, vf="2026-03-01T00:00:00Z", vt=None):
    a = {"id": aid, "valid_from": vf, "geometry": geom}
    if vt:
        a["valid_to"] = vt
    return a


def vessel(vid, points):
    return {"vessel_id": vid,
            "track": [{"time": t, "lon": lon, "lat": lat} for t, lon, lat in points]}


def stationary(vid, x, y, t0, t1):
    def ts(s):
        return f"2026-03-01T00:{s // 60:02d}:{s % 60:02d}Z"

    return vessel(vid, [(ts(t0), x, y), (ts(t1), x, y)])


def patrol(areas, vessels, n=2):
    return client.post("/api/v1/patrol",
                       json={"areas": areas, "vessels": vessels, "threshold": n})


def test_basic_two_vessel_overlap():
    # 60s 内 -10->30（速度 40/60）：区内 [15,30]；
    # 第二艘 10..70 走同样路径：区内 [25,40]；交集 [25,30) = 5s
    v1 = vessel("V1", [
        ("2026-03-01T00:00:00Z", -10, 5),
        ("2026-03-01T00:01:00Z", 30, 5),
    ])
    v2 = vessel("V2", [
        ("2026-03-01T00:00:10Z", -10, 5),
        ("2026-03-01T00:01:10Z", 30, 5),
    ])
    r = patrol([area()], [v1, v2], 2)
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["id"] == "A1"
    assert res["threshold"] == 2
    assert res["vessel_count"] == 2
    assert res["total_concurrent_seconds"] == 5.0
    assert len(res["periods"]) == 1
    p = res["periods"][0]
    assert p["start"] == "2026-03-01T00:00:25Z"
    assert p["end"] == "2026-03-01T00:00:30Z"
    assert p["peak_vessel_count"] == 2
    assert p["duration_seconds"] == 5.0


def test_count_changes_but_period_continues_peak_is_max():
    # 三艘静止在区内：[0,60] / [20,80] / [30,40]
    v1 = stationary("V1", 5, 5, 0, 60)
    v2 = stationary("V2", 5, 5, 20, 80)
    v3 = stationary("V3", 5, 5, 30, 40)

    r = patrol([area()], [v1, v2, v3], 2)
    res = r.json()["results"][0]
    # 船数 2->3->2 但始终达标：不断开，峰值取 3，累计 40s
    assert len(res["periods"]) == 1
    p = res["periods"][0]
    assert p["start"] == "2026-03-01T00:00:20Z"
    assert p["end"] == "2026-03-01T00:01:00Z"
    assert p["peak_vessel_count"] == 3
    assert p["duration_seconds"] == 40.0
    assert res["total_concurrent_seconds"] == 40.0

    # N=3 只剩中间 10s
    r3 = patrol([area()], [v1, v2, v3], 3)
    res3 = r3.json()["results"][0]
    assert len(res3["periods"]) == 1
    assert res3["periods"][0]["start"] == "2026-03-01T00:00:30Z"
    assert res3["periods"][0]["end"] == "2026-03-01T00:00:40Z"
    assert res3["periods"][0]["peak_vessel_count"] == 3
    assert res3["total_concurrent_seconds"] == 10.0


def test_gap_splits_periods():
    # V1 往返穿过：区内 [10,20) 与 [50,60)；V2 静止 [15,55)
    v1 = vessel("V1", [
        ("2026-03-01T00:00:00Z", -10, 5),
        ("2026-03-01T00:00:30Z", 20, 5),
        ("2026-03-01T00:00:40Z", 20, 5),
        ("2026-03-01T00:01:10Z", -10, 5),
    ])
    v2 = stationary("V2", 5, 5, 15, 55)
    r = patrol([area()], [v1, v2], 2)
    res = r.json()["results"][0]
    # 中间 20..50 只有一艘 -> 断成两个时段，各 5s
    assert len(res["periods"]) == 2
    assert [p["start"] for p in res["periods"]] == [
        "2026-03-01T00:00:15Z",
        "2026-03-01T00:00:50Z",
    ]
    assert [p["end"] for p in res["periods"]] == [
        "2026-03-01T00:00:20Z",
        "2026-03-01T00:00:55Z",
    ]
    assert all(p["peak_vessel_count"] == 2 for p in res["periods"])
    assert res["total_concurrent_seconds"] == 10.0


def test_adjacent_half_open_intervals_stay_connected():
    # V1 静止区内 [0,20)，V2 静止区内 [20,40)：t=20 一走一来，
    # 左闭右开下任一时刻都恰好 1 艘 -> N=1 单一时段 [0,40)，峰值 1
    v1 = stationary("V1", 5, 5, 0, 20)
    v2 = stationary("V2", 5, 5, 20, 40)
    r = patrol([area()], [v1, v2], 1)
    res = r.json()["results"][0]
    assert len(res["periods"]) == 1
    p = res["periods"][0]
    assert p["start"] == "2026-03-01T00:00:00Z"
    assert p["end"] == "2026-03-01T00:00:40Z"
    assert p["peak_vessel_count"] == 1
    assert p["duration_seconds"] == 40.0
    assert res["total_concurrent_seconds"] == 40.0

    # N=2：任何时刻都不足 2 艘
    r2 = patrol([area()], [v1, v2], 2)
    assert r2.json()["results"][0]["periods"] == []
    assert r2.json()["results"][0]["total_concurrent_seconds"] == 0.0


def test_single_point_touch_creates_no_period():
    v1 = stationary("V1", 5, 5, 0, 30)
    # V2 的直线仅在 t=10 擦过正方形顶点 (10,0)：单点触碰不计时长
    v2 = vessel("V2", [
        ("2026-03-01T00:00:00Z", 11, 1),
        ("2026-03-01T00:00:20Z", 9, -1),
    ])
    r = patrol([area()], [v1, v2], 2)
    res = r.json()["results"][0]
    assert res["periods"] == []
    assert res["total_concurrent_seconds"] == 0.0

    # N=1 只统计 V1 的停留，触碰点不产生零长时段
    r1 = patrol([area()], [v1, v2], 1)
    periods = r1.json()["results"][0]["periods"]
    assert len(periods) == 1
    assert periods[0]["start"] == "2026-03-01T00:00:00Z"
    assert periods[0]["end"] == "2026-03-01T00:00:30Z"


def test_validity_window_clips_concurrency():
    # 保护区生效 [10,40)：V1 截成 [10,40)，V2 [20,80) 截成 [20,40)
    v1 = stationary("V1", 5, 5, 0, 60)
    v2 = stationary("V2", 5, 5, 20, 80)
    a = area(vf="2026-03-01T00:00:10Z", vt="2026-03-01T00:00:40Z")
    r = patrol([a], [v1, v2], 2)
    res = r.json()["results"][0]
    assert len(res["periods"]) == 1
    p = res["periods"][0]
    assert p["start"] == "2026-03-01T00:00:20Z"
    assert p["end"] == "2026-03-01T00:00:40Z"
    assert p["peak_vessel_count"] == 2
    assert p["duration_seconds"] == 20.0


def test_threshold_above_vessel_count_returns_empty():
    v1 = stationary("V1", 5, 5, 0, 60)
    v2 = stationary("V2", 5, 5, 0, 60)
    r = patrol([area()], [v1, v2], 3)
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["periods"] == []
    assert res["total_concurrent_seconds"] == 0.0
    assert res["vessel_count"] == 2
    assert res["threshold"] == 3


def test_results_follow_area_order_and_empty_area_echoed():
    far = {"type": "Polygon",
           "coordinates": [[[40, 40], [50, 40], [50, 50], [40, 50], [40, 40]]]}
    v1 = stationary("V1", 5, 5, 0, 10)
    v2 = stationary("V2", 5, 5, 5, 15)
    r = patrol([area("A1"), area("A2", far)], [v1, v2], 2)
    results = r.json()["results"]
    assert [x["id"] for x in results] == ["A1", "A2"]
    assert len(results[0]["periods"]) == 1
    assert results[0]["periods"][0]["start"] == "2026-03-01T00:00:05Z"
    assert results[0]["periods"][0]["end"] == "2026-03-01T00:00:10Z"
    assert results[1]["periods"] == []
    assert results[1]["total_concurrent_seconds"] == 0.0


def test_dateline_band_stationary_concurrency():
    v1 = stationary("V1", 175, 0, 0, 20)
    v2 = stationary("V2", 175, 0, 10, 30)
    r = patrol([area(geom=DATELINE_BAND)], [v1, v2], 2)
    res = r.json()["results"][0]
    assert len(res["periods"]) == 1
    assert res["periods"][0]["start"] == "2026-03-01T00:00:10Z"
    assert res["periods"][0]["end"] == "2026-03-01T00:00:20Z"
    assert res["total_concurrent_seconds"] == 10.0


def test_vessels_with_disjoint_time_ranges():
    # 时间范围完全不相交：无并发
    v1 = stationary("V1", 5, 5, 0, 20)
    v2 = vessel("V2", [
        ("2026-03-02T00:00:00Z", 5, 5),
        ("2026-03-02T00:00:20Z", 5, 5),
    ])
    r = patrol([area()], [v1, v2], 2)
    assert r.json()["results"][0]["periods"] == []

    # N=1 时段来自不同日期，互不相连，返回两个时段（时间轴为 UTC）
    r1 = patrol([area()], [v1, v2], 1)
    periods = r1.json()["results"][0]["periods"]
    assert [p["start"] for p in periods] == [
        "2026-03-01T00:00:00Z",
        "2026-03-02T00:00:00Z",
    ]


def test_integer_vessel_ids_and_offsets():
    v1 = vessel(100, [
        ("2026-03-01T08:00:00+08:00", -10, 5),
        ("2026-03-01T08:01:00+08:00", 30, 5),
    ])
    v2 = vessel(200, [
        ("2026-03-01T00:00:10Z", -10, 5),
        ("2026-03-01T00:01:10Z", 30, 5),
    ])
    r = patrol([area()], [v1, v2], 2)
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["total_concurrent_seconds"] == 5.0
