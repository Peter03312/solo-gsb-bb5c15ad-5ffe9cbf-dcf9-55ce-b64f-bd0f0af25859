"""巡查接口的整体拒绝条件：任一无效输入 -> HTTP 400 中文原因。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SQUARE = {"type": "Polygon",
          "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]}


def good_area():
    return {"id": "A1", "valid_from": "2026-01-01T00:00:00Z", "geometry": SQUARE}


def good_vessel(vid="V1", times=("2026-03-01T00:00:00Z", "2026-03-01T00:00:10Z"),
                p1=(-10, 5), p2=(30, 5)):
    return {"vessel_id": vid, "track": [
        {"time": times[0], "lon": p1[0], "lat": p1[1]},
        {"time": times[1], "lon": p2[0], "lat": p2[1]},
    ]}


def patrol_raw(payload):
    return client.post("/api/v1/patrol", json=payload)


def patrol(areas, vessels, threshold=2):
    return patrol_raw({"areas": areas, "vessels": vessels, "threshold": threshold})


def test_duplicate_vessel_ids_rejected():
    r = patrol([good_area()], [good_vessel("X"), good_vessel("X")])
    assert r.status_code == 400
    assert "vessel_id 重复" in r.text


def test_threshold_zero_or_negative_rejected():
    for n in (0, -1):
        r = patrol([good_area()], [good_vessel(), good_vessel("V2")], n)
        assert r.status_code == 400
        assert "正整数" in r.text


def test_threshold_float_or_bool_rejected():
    for n in (1.5, 2.0, True, False):
        r = patrol_raw({"areas": [good_area()],
                        "vessels": [good_vessel(), good_vessel("V2")],
                        "threshold": n})
        assert r.status_code == 400, (n, r.text)
        assert "正整数" in r.text


def test_threshold_missing_rejected():
    r = patrol_raw({"areas": [good_area()], "vessels": [good_vessel(), good_vessel("V2")]})
    assert r.status_code == 400


def test_vessels_empty_rejected():
    r = patrol([good_area()], [], 1)
    assert r.status_code == 400


def test_track_too_short_rejected():
    v = {"vessel_id": "V1", "track": [{"time": "2026-03-01T00:00:00Z", "lon": 1, "lat": 1}]}
    r = patrol([good_area()], [v, good_vessel("V2")], 1)
    assert r.status_code == 400
    assert "至少需要 2" in r.text


def test_track_time_not_strictly_increasing_rejected():
    v = good_vessel("V1", times=("2026-03-01T00:00:10Z", "2026-03-01T00:00:10Z"))
    r = patrol([good_area()], [v, good_vessel("V2")], 1)
    assert r.status_code == 400
    assert "递增" in r.text
    assert "vessels[0]" in r.text


def test_coordinate_out_of_range_rejected():
    v = good_vessel("V1", p2=(200, 5))
    r = patrol([good_area()], [v, good_vessel("V2")], 1)
    assert r.status_code == 400
    assert "经度越界" in r.text


def test_bad_geometry_rejected():
    bad = {"type": "Polygon", "coordinates": [[[0, 0], [10, 10], [10, 0], [0, 10], [0, 0]]]}
    r = patrol([{"id": "A1", "valid_from": "2026-01-01T00:00:00Z", "geometry": bad}],
               [good_vessel(), good_vessel("V2")], 1)
    assert r.status_code == 400
    assert "自交" in r.text


def test_duplicate_area_ids_rejected():
    r = patrol([good_area(), good_area()], [good_vessel(), good_vessel("V2")], 1)
    assert r.status_code == 400
    assert "areas[1].id 重复" in r.text


def test_invalid_validity_window_rejected():
    a = {"id": "A1",
         "valid_from": "2026-03-01T00:00:20Z",
         "valid_to": "2026-03-01T00:00:10Z",
         "geometry": SQUARE}
    r = patrol([a], [good_vessel(), good_vessel("V2")], 1)
    assert r.status_code == 400
    assert "valid_to" in r.text


def test_extra_fields_rejected():
    payload = {"areas": [good_area()], "vessels": [good_vessel()], "threshold": 1,
               "unexpected": 1}
    r = patrol_raw(payload)
    assert r.status_code == 400


def test_not_json_object():
    r = patrol_raw(["nope"])
    assert r.status_code == 400


def test_vessel_extra_field_rejected():
    v = good_vessel()
    v["mmsi"] = "x"
    r = patrol([good_area()], [v], 1)
    assert r.status_code == 400
