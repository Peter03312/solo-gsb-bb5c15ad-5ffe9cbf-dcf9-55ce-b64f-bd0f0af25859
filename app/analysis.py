"""请求分析编排：测线插值、逐线段求交、时间区间合并与生效区间裁切。

单测线分析见 :func:`analyze`；多船舶巡查并发分析见 :func:`patrol`。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .geopoly import EPS, Poly, segment_closed_intervals, union_closed, wrap_delta


class AnalysisError(ValueError):
    """请求语义错误（坐标越界、时间不递增等），映射为 400。"""


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

def parse_utc(value, label: str) -> datetime:
    """解析 ISO 8601 时间字符串；无时区的朴素时间按 UTC 处理。"""
    if not isinstance(value, str):
        raise AnalysisError(f"{label} 必须是 ISO 8601 时间字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AnalysisError(f"{label} 无法解析为时间: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_utc(dt: datetime) -> str:
    """格式化为带 Z 的 UTC ISO 8601（秒级，不足秒带小数）。"""
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 区间工具（闭区间 -> 左闭右开时间区间）
# ---------------------------------------------------------------------------

def to_half_open(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """丢弃零长/点区间，其余视为 [a, b)。"""
    return [(a, b) for a, b in intervals if b - a > EPS]


def clip_half_open(
    intervals: list[tuple[float, float]], lo: float, hi: float
) -> list[tuple[float, float]]:
    """左闭右开区间与 [lo, hi) 裁切。"""
    out: list[tuple[float, float]] = []
    for a, b in intervals:
        c, d = max(a, lo), min(b, hi)
        if d - c > EPS:
            out.append((c, d))
    return out


def merge_half_open(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """合并相交或端点相接（[a,b) 与 [b,c) 相接）的左闭右开区间。"""
    if not intervals:
        return []
    items = sorted(intervals)
    merged: list[tuple[float, float]] = []
    ca, cb = items[0]
    for a, b in items[1:]:
        if a <= cb + EPS:
            if b > cb:
                cb = b
        else:
            merged.append((ca, cb))
            ca, cb = a, b
    merged.append((ca, cb))
    return merged


# ---------------------------------------------------------------------------
# 输入解析与单测线停留区间
# ---------------------------------------------------------------------------

def _validate_effective(valid_from: datetime, valid_to: datetime | None, label: str) -> None:
    if valid_to is not None and valid_to <= valid_from:
        raise AnalysisError(f"{label} 的 valid_to 必须晚于 valid_from")


def _parse_track(points, track_label: str) -> tuple[list[datetime], list[tuple[float, float]]]:
    """解析并校验一条测线：至少两点、时间严格递增。返回 (UTC 时刻, 经纬度)。"""
    if len(points) < 2:
        raise AnalysisError(f"{track_label} 至少需要 2 个按时间排列的点")

    times: list[datetime] = []
    coords: list[tuple[float, float]] = []
    for i, pt in enumerate(points):
        times.append(parse_utc(pt.time, f"{track_label}[{i}].time"))
        coords.append((pt.lon, pt.lat))

    for i in range(len(times) - 1):
        if times[i + 1] <= times[i]:
            raise AnalysisError(
                f"{track_label}[{i + 1}].time 必须严格晚于 {track_label}[{i}].time"
                "（时间必须严格递增）"
            )
    return times, coords


def _parse_area_specs(areas) -> list[tuple[object, list[Poly], datetime, datetime | None]]:
    """解析保护区列表：编号去重、生效区间与几何校验。"""
    from .geopoly import parse_geojson

    area_specs: list[tuple[object, list[Poly], datetime, datetime | None]] = []
    seen_ids: set[object] = set()
    for idx, area in enumerate(areas):
        aid = area.id
        if aid in seen_ids:
            raise AnalysisError(f"areas[{idx}].id 重复: {aid!r}")
        seen_ids.add(aid)
        vf = parse_utc(area.valid_from, f"areas[{idx}].valid_from")
        vt = parse_utc(area.valid_to, f"areas[{idx}].valid_to") if area.valid_to else None
        _validate_effective(vf, vt, f"areas[{idx}]")
        polys = parse_geojson(area.geometry, label=f"areas[{idx}]")
        area_specs.append((aid, polys, vf, vt))
    return area_specs


def _track_stay_intervals(
    area_specs: list[tuple[object, list[Poly], datetime, datetime | None]],
    times: list[datetime],
    coords: list[tuple[float, float]],
) -> dict[int, list[tuple[float, float]]]:
    """一条测线在各保护区内的停留区间（相对该测线首点的秒，左闭右开，已合并）。

    沿用短弧插值与边界归属规则；不做保护区生效区间裁切（由调用方处理）。
    """
    t0 = times[0]
    t_rel = [(t - t0).total_seconds() for t in times]
    area_intervals: dict[int, list[tuple[float, float]]] = {i: [] for i in range(len(area_specs))}

    for seg in range(len(times) - 1):
        lon1, lat1 = coords[seg]
        lon2, lat2 = coords[seg + 1]
        lon2_unwrapped = lon1 + wrap_delta(lon2 - lon1)
        p1 = (lon1, lat1)
        p2 = (lon2_unwrapped, lat2)
        seg_t0, seg_t1 = t_rel[seg], t_rel[seg + 1]

        for ai, (_, polys, _, _) in enumerate(area_specs):
            closed = segment_closed_intervals(p1, p2, polys)
            closed = union_closed(closed)
            for s0, s1 in to_half_open(closed):
                area_intervals[ai].append((seg_t0 + s0 * (seg_t1 - seg_t0),
                                           seg_t0 + s1 * (seg_t1 - seg_t0)))

    return {ai: merge_half_open(ivs) for ai, ivs in area_intervals.items()}


def _clip_validity(
    intervals: list[tuple[float, float]],
    vf: datetime,
    vt: datetime | None,
    base: datetime,
) -> list[tuple[float, float]]:
    """把相对 base 的左闭右开秒区间与保护区 [valid_from, valid_to) 裁切后再合并。"""
    lo = (vf - base).total_seconds()
    hi = float("inf") if vt is None else (vt - base).total_seconds()
    return merge_half_open(clip_half_open(intervals, lo, hi))


# ---------------------------------------------------------------------------
# 单测线分析（原接口，响应保持兼容）
# ---------------------------------------------------------------------------

def analyze(areas, points) -> list[dict]:
    """执行单测线分析。

    areas: 已由 Pydantic 解析的 ProtectedArea 列表
    points: 已解析的 TrackPoint 列表
    """
    times, coords = _parse_track(points, "track")
    area_specs = _parse_area_specs(areas)

    t0 = times[0]
    stay = _track_stay_intervals(area_specs, times, coords)

    results: list[dict] = []
    for ai, (aid, _, vf, vt) in enumerate(area_specs):
        intervals = _clip_validity(stay[ai], vf, vt, t0)

        entries = []
        total = 0.0
        for a, b in intervals:
            total += b - a
            entries.append({
                "enter": format_utc(t0 + timedelta(seconds=a)),
                "leave": format_utc(t0 + timedelta(seconds=b)),
                "duration_seconds": round(b - a, 6),
            })

        results.append({
            "id": aid,
            "inside": bool(intervals),
            "total_duration_seconds": round(total, 6),
            "entries": entries,
        })

    return results


# ---------------------------------------------------------------------------
# 多船舶巡查：同一保护区内的并发停留
# ---------------------------------------------------------------------------

def _sweep_concurrent(
    per_vessel_intervals: list[list[tuple[float, float]]], threshold: int
) -> list[tuple[float, float, int]]:
    """扫描线统计并发船数，返回达标最大连续时段。

    输入为每艘船各自的一组左闭右开区间（同一船的区间已合并且互不重叠）。
    返回 ``[(start, end, peak_vessel_count), ...]``，时间轴为调用方的相对秒：

    * 船数在事件点变化但始终 ``>= threshold`` 时时段不断开；
    * 端点相接（一艘 ``[a,b)`` 离开、另一艘 ``[b,c)`` 进入）保持连续；
    * 单点触碰在更上游已丢弃，不产生零长时段。
    """
    events: list[tuple[float, int]] = []
    for intervals in per_vessel_intervals:
        for a, b in intervals:
            events.append((a, +1))
            events.append((b, -1))
    if not events:
        return []

    events.sort(key=lambda e: e[0])
    # 聚合同一时刻（容差 EPS 秒）的所有船数增减，避免跨船舍入误差造成假缝隙。
    groups: list[list] = []
    for t, delta in events:
        if groups and t - groups[-1][0] <= EPS:
            groups[-1][1] += delta
        else:
            groups.append([t, delta])

    # 相邻事件之间船数恒定，切出达标片段 [t, t_next)。
    qualifying: list[tuple[float, float, int]] = []
    count = 0
    for i, (t, delta) in enumerate(groups):
        count += delta
        if i + 1 >= len(groups):
            break
        t_next = groups[i + 1][0]
        if t_next - t > EPS and count >= threshold:
            qualifying.append((t, t_next, count))

    # 合并相邻的达标片段（船数变化但不断开；端点相接天然连续）。
    runs: list[tuple[float, float, int]] = []
    for a, b, c in qualifying:
        if runs and a <= runs[-1][1] + EPS:
            pa, _, pc = runs[-1]
            runs[-1] = (pa, b, max(pc, c))
        else:
            runs.append((a, b, c))
    return runs


def patrol(areas, vessels, threshold: int) -> list[dict]:
    """多船舶巡查并发分析。

    areas: 已解析的 ProtectedArea 列表（按输入顺序返回结果）
    vessels: 已解析的 VesselTrack 列表（vessel_id 唯一，每条测线至少两点）
    threshold: 正整数 N，船数不少于 N 才算并发时段
    """
    # 先校验全部测线（编号唯一、时间严格递增），再校验保护区（含几何）；
    # 任一无效整体报错，不产生部分结果。
    parsed_vessels: list[tuple[object, list[datetime], list[tuple[float, float]]]] = []
    seen_vids: set[object] = set()
    for vi, vessel in enumerate(vessels):
        vid = vessel.vessel_id
        if vid in seen_vids:
            raise AnalysisError(f"vessels[{vi}].vessel_id 重复: {vid!r}")
        seen_vids.add(vid)
        times, coords = _parse_track(vessel.track, f"vessels[{vi}].track")
        parsed_vessels.append((vid, times, coords))

    area_specs = _parse_area_specs(areas)

    vessel_count = len(parsed_vessels)
    # 统一到最早测线首点的绝对时间轴，不同船舶的时间范围可以不一致。
    base = min(times[0] for _, times, _ in parsed_vessels)

    # per_vessel[vi][ai] = 全局相对秒的有效停留区间（已按生效区间裁切）
    per_vessel: list[dict[int, list[tuple[float, float]]]] = []
    for _, times, coords in parsed_vessels:
        offset = (times[0] - base).total_seconds()
        stay = _track_stay_intervals(area_specs, times, coords)
        global_intervals: dict[int, list[tuple[float, float]]] = {}
        for ai, (_, _, vf, vt) in enumerate(area_specs):
            shifted = [(a + offset, b + offset) for a, b in stay[ai]]
            global_intervals[ai] = _clip_validity(shifted, vf, vt, base)
        per_vessel.append(global_intervals)

    results: list[dict] = []
    for ai, (aid, _, _, _) in enumerate(area_specs):
        if threshold > vessel_count:
            runs: list[tuple[float, float, int]] = []
        else:
            runs = _sweep_concurrent([m[ai] for m in per_vessel], threshold)

        periods = []
        total = 0.0
        for a, b, peak in runs:
            duration = b - a
            total += duration
            periods.append({
                "start": format_utc(base + timedelta(seconds=a)),
                "end": format_utc(base + timedelta(seconds=b)),
                "peak_vessel_count": peak,
                "duration_seconds": round(duration, 6),
            })

        results.append({
            "id": aid,
            "threshold": threshold,
            "vessel_count": vessel_count,
            "periods": periods,
            "total_concurrent_seconds": round(total, 6),
        })

    return results
