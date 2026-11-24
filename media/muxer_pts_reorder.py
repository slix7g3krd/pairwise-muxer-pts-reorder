"""封装时间戳重排工具集。

修复要点：
1. 存在 B 帧时按解码顺序（DTS）重排，而不是照搬 PTS，避免播放器时间轴倒跳；
2. 不同来源片段（1/90000 与 1/1000）统一换算到公共 timebase，用分数精确运算避免截断；
3. 开头负 DTS 通过 delay + 编辑列表（edit list）整体平移，首帧不再被丢弃；
4. 异步封装线程先写帧数据、最后写尾部索引，并置完成标志，seek 到片尾不再失败；
5. 丢帧后按累计时长重算后续 PTS，字幕与画面保持同步；
6. 写出前校验 DTS 单调性与总时长。
"""

from fractions import Fraction

# 公共 timebase：1/90000 与 1/1000 的最小公倍数粒度，取 1/90000（90000 是 1000 的整数倍）
COMMON_TIMEBASE = Fraction(1, 90000)


def _as_packet(p):
    """把输入规整为 packet dict：{pts, dts, duration}。"""
    if isinstance(p, dict):
        return {
            "pts": p.get("pts", p.get("dts", 0)),
            "dts": p.get("dts", p.get("pts", 0)),
            "duration": p.get("duration", 0),
        }
    if isinstance(p, (tuple, list)):
        pts = p[0] if len(p) > 0 else 0
        dts = p[1] if len(p) > 1 else pts
        dur = p[2] if len(p) > 2 else 0
        return {"pts": pts, "dts": dts, "duration": dur}
    return {"pts": p, "dts": p, "duration": 0}


def dts_reorder(packets):
    """按 DTS（解码顺序）重排 packet，B 帧场景下 PTS 仅表示显示顺序。

    返回按 (dts, 原始序号) 稳定排序后的 packet 列表，保证确定性。
    """
    if not isinstance(packets, (list, tuple)):
        packets = [packets]
    indexed = [(_as_packet(p), i) for i, p in enumerate(packets)]
    indexed.sort(key=lambda t: (t[0]["dts"], t[1]))
    return [p for p, _ in indexed]


def timebase_convert(value, src_tb=(1, 90000), dst_tb=COMMON_TIMEBASE):
    """把 value 从 src_tb 精确换算到 dst_tb（分数运算，不截断）。

    src_tb/dst_tb 可为 Fraction、(num, den) 元组或标量。
    """
    def _tb(tb):
        if isinstance(tb, Fraction):
            return tb
        if isinstance(tb, (tuple, list)):
            return Fraction(tb[0], tb[1])
        return Fraction(tb)

    src, dst = _tb(src_tb), _tb(dst_tb)
    if isinstance(value, (list, tuple)):
        return [timebase_convert(v, src, dst) for v in value]
    seconds = Fraction(value) * src
    return seconds / dst


def edit_list_delay(packets, delay=None):
    """处理开头负 DTS：计算 delay 并整体平移，返回 (平移后的 packets, 编辑列表偏移)。

    编辑列表偏移即媒体时间相对呈现时间的位移，播放器据此跳过前置的解码提前量，
    首帧不会因负 DTS 被丢弃。
    """
    if not isinstance(packets, (list, tuple)):
        packets = [packets]
    pkts = [_as_packet(p) for p in packets]
    if delay is None:
        min_dts = min((p["dts"] for p in pkts), default=0)
        delay = -min_dts if min_dts < 0 else 0
    shifted = [
        {"pts": p["pts"] + delay, "dts": p["dts"] + delay, "duration": p["duration"]}
        for p in pkts
    ]
    return shifted, delay


def index_last_write(events):
    """异步封装写序：帧数据先写，索引最后写，并追加完成标志。

    输入为写事件序列（含 "frame"/"index" 等种类），返回重排后的事件列表：
    所有非索引事件保持原序在前，索引事件在最后，末尾追加 "complete" 标志。
    """
    if not isinstance(events, (list, tuple)):
        events = [events]

    def _kind(e):
        return e.get("type") if isinstance(e, dict) else e

    frames = [e for e in events if _kind(e) != "index"]
    indexes = [e for e in events if _kind(e) == "index"]
    return frames + indexes + [{"type": "complete", "done": True}]


def recompute_after_drop(packets, dropped=None):
    """丢帧后按累计时长重算后续 PTS/DTS，保持音画与字幕同步。

    dropped 为要丢弃的下标集合。保留帧的显示/解码时间戳按存活帧时长重新累计。
    """
    if not isinstance(packets, (list, tuple)):
        packets = [packets]
    dropped = set(dropped or [])
    kept = [_as_packet(p) for i, p in enumerate(packets) if i not in dropped]
    out, pts_acc, dts_acc = [], 0, 0
    for p in kept:
        out.append({"pts": pts_acc, "dts": dts_acc, "duration": p["duration"]})
        pts_acc += p["duration"]
        dts_acc += p["duration"]
    return out


def determinism(packets):
    """确定性排序：按 (dts, pts, 原始序号) 稳定排序，相同输入必得相同输出。"""
    if not isinstance(packets, (list, tuple)):
        packets = [packets]
    indexed = [(_as_packet(p), i) for i, p in enumerate(packets)]
    indexed.sort(key=lambda t: (t[0]["dts"], t[0]["pts"], t[1]))
    return [p for p, _ in indexed]


def monotonic_check(packets):
    """校验 DTS 单调非递减，返回违规位置列表（空列表表示通过）。"""
    if not isinstance(packets, (list, tuple)):
        packets = [packets]
    pkts = [_as_packet(p) for p in packets]
    violations = []
    for i in range(1, len(pkts)):
        if pkts[i]["dts"] < pkts[i - 1]["dts"]:
            violations.append(i)
    return violations


def validation(packets, expected_duration=None):
    """写出前校验：DTS 单调性 + 总时长，返回校验报告 dict。"""
    if not isinstance(packets, (list, tuple)):
        packets = [packets]
    pkts = [_as_packet(p) for p in packets]
    violations = monotonic_check(pkts)
    total = sum(p["duration"] for p in pkts)
    report = {
        "monotonic": not violations,
        "violations": violations,
        "total_duration": total,
        "duration_ok": expected_duration is None or total <= expected_duration,
    }
    report["ok"] = report["monotonic"] and report["duration_ok"]
    return report


def audit(packets):
    """审计：汇总重排、timebase、delay、单调性等关键指标，返回报告 dict。"""
    if not isinstance(packets, (list, tuple)):
        packets = [packets]
    pkts = [_as_packet(p) for p in packets]
    shifted, delay = edit_list_delay(pkts)
    ordered = dts_reorder(shifted)
    return {
        "count": len(pkts),
        "delay": delay,
        "reordered": [p["dts"] for p in pkts] != [p["dts"] for p in ordered],
        "monotonic_violations": monotonic_check(ordered),
        "total_duration": sum(p["duration"] for p in pkts),
    }


def fallback(value, default=0):
    """兜底：非法时间戳（None/NaN/负无穷等）回退到 default，保证流程不中断。"""
    if value is None:
        return default
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return default
    return value


def duration_clamp(duration, max_duration=None, min_duration=0):
    """时长钳制到 [min_duration, max_duration]，防止总时长越界。"""
    duration = fallback(duration, min_duration)
    if duration < min_duration:
        return min_duration
    if max_duration is not None and duration > max_duration:
        return max_duration
    return duration


def metrics(packets):
    """统计指标：帧数、总时长、首末 DTS、是否单调。"""
    if not isinstance(packets, (list, tuple)):
        packets = [packets]
    pkts = [_as_packet(p) for p in packets]
    dts_list = [p["dts"] for p in pkts]
    return {
        "frames": len(pkts),
        "total_duration": sum(p["duration"] for p in pkts),
        "first_dts": dts_list[0] if dts_list else None,
        "last_dts": dts_list[-1] if dts_list else None,
        "monotonic": not monotonic_check(pkts),
    }


def write_frame(state, frame=None):
    """向封装状态写入一帧：只追加帧数据，不写索引（索引由 trailer 最后写）。"""
    if not isinstance(state, dict):
        state = {"frames": [], "index": [], "complete": False}
    state.setdefault("frames", [])
    state.setdefault("index", [])
    state.setdefault("complete", False)
    if frame is not None:
        state["frames"].append(_as_packet(frame))
    return state


def seek_index(index, target_dts=None):
    """在索引中按 DTS 二分查找，返回不大于 target 的最后一项下标。

    索引必须已完整写入（complete 标志为真）才能 seek 到片尾。
    """
    if isinstance(index, dict):
        if not index.get("complete", False):
            return -1
        index = index.get("index", [])
    if not isinstance(index, (list, tuple)) or not index:
        return -1
    entries = [_as_packet(e) for e in index]
    if target_dts is None:
        return len(entries) - 1
    lo, hi, ans = 0, len(entries) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if entries[mid]["dts"] <= target_dts:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def trailer(state):
    """收尾：根据已写帧生成索引并最后写入，置完成标志，返回最终状态。"""
    if not isinstance(state, dict):
        state = {"frames": [], "index": [], "complete": False}
    frames = state.get("frames", [])
    ordered = dts_reorder(frames) if frames else []
    state["index"] = [
        {"dts": p["dts"], "pts": p["pts"], "duration": p["duration"]} for p in ordered
    ]
    state["complete"] = True
    return state
