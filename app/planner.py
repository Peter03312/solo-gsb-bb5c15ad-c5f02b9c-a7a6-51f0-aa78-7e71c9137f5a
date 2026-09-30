"""疏散编排核心算法。

时间模型
--------
- 当前分钟从 0 起；左闭右开的占用与封闭语义。
- 一条通道 travel_minutes = d、组在 t 分钟进入，则占用该通道的分钟为
  [t, t+d) 的每一个整数分钟；到达通道终点（下一节点）的分钟为 t+d。
- 封闭 [c_start, c_end) 与占用 [t, t+d) 相交（t < c_end and t+d > c_start）即冲突。
- 每组不可拆分，通行期间每分钟“各组人数之和 <= 通道每分钟容量”。

编排规则
--------
按 优先级降序 → 最早出发分钟升序 → 组编号升序 逐组贪心安排：
选取“截止前最早到达”的路线与出发时间；同到达依次按 出口编号、
通道编号序列、出发分钟 择优。路线为简单路径（中途不等待，等待等价于推迟出发）。
"""
from __future__ import annotations

from .models import Closure, Edge, Exit, Group

# 不可行原因码
REASON_NO_PATH = "no_path_to_exit"                 # 图上不存在到出口的路径
REASON_CAPACITY = "no_capacity_before_deadline"    # 容量/封闭导致截止前无法安排
REASON_DEADLINE_PASSED = "deadline_already_passed"  # 截止分钟已过


class PlanningError(Exception):
    """结构性冲突（如封闭触及已出发组剩余路线），整次变更必须拒绝。"""


# ---------------------------------------------------------------- 基础工具

def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并相交区间（区间均为左闭右开）。"""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged: list[tuple[int, int]] = [intervals[0]]
    for s, e in intervals[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def _build_adjacency(edges: dict[int, Edge]) -> dict[str, list[Edge]]:
    adj: dict[str, list[Edge]] = {}
    for e in edges.values():
        adj.setdefault(e.source, []).append(e)
    for lst in adj.values():
        lst.sort(key=lambda x: x.id)
    return adj


def _path_offsets(route: list[Edge]) -> dict[int, int]:
    """route 中每条边进入时刻相对出发分钟的偏移。"""
    offsets: dict[int, int] = {}
    elapsed = 0
    for e in route:
        offsets[e.id] = elapsed
        elapsed += e.travel_minutes
    return offsets


def _travel_time(route: list[Edge]) -> int:
    return sum(e.travel_minutes for e in route)


def validate_scenario(
    edges: dict[int, Edge],
    exits: dict[int, Exit],
    closures: dict[int, Closure],
    groups: dict[int, Group],
) -> None:
    if not edges:
        raise PlanningError("至少需要一条通道")
    if not exits:
        raise PlanningError("至少需要一个出口")
    nodes = {n for e in edges.values() for n in (e.source, e.target)}
    for x in exits.values():
        if x.node not in nodes:
            raise PlanningError(f"出口 {x.id} 所在节点 {x.node} 不存在")
    for c in closures.values():
        if c.edge_id not in edges:
            raise PlanningError(f"封闭记录 {c.id} 引用了不存在的通道 {c.edge_id}")
    for g in groups.values():
        if g.source not in nodes:
            raise PlanningError(f"人群组 {g.id} 的起点 {g.source} 不存在")


# ---------------------------------------------------------------- 路径枚举

def _enumerate_candidate_paths(
    source: str,
    edges: dict[int, Edge],
    exits: dict[int, Exit],
    max_paths: int,
) -> tuple[list[tuple[list[Edge], Exit]], int]:
    """枚举 source 到任意出口节点的全部简单路径（含零长度路径）。

    返回 (候选, 被截断的路径数)。每个出口节点只取编号最小的出口作为终点。
    """
    exit_node_best = {x.node: x for x in sorted(exits.values(), key=lambda x: x.id)}
    adj = _build_adjacency(edges)

    candidates: list[tuple[list[Edge], Exit]] = []
    truncated = 0
    stack: list[tuple[str, set[str], list[Edge]]] = [(source, {source}, [])]

    while stack:
        node, visited, path = stack.pop()
        if node in exit_node_best:
            candidates.append((list(path), exit_node_best[node]))
            continue  # 出口即终点，不再展开（途经出口必被更短路径支配）
        # 深搜：出边按 id 降序入栈，保证小 id 先被处理
        for e in reversed(adj.get(node, [])):
            if e.target not in visited:
                if len(candidates) >= max_paths:
                    truncated += 1
                    continue
                stack.append((e.target, visited | {e.target}, path + [e]))
    return candidates, truncated


# ---------------------------------------------------------------- 固定占用

def stamp_fixed_occupancy(
    route: list[Edge],
    departure: int,
    people: int,
    occupancy: dict[int, dict[int, int]],
    now: int,
    closures_by_edge: dict[int, list[Closure]],
    group_id: int,
) -> None:
    """将一条已固定（已出发或新排定）路线写入占用表，并校验容量/封闭。

    仅登记 minute >= now 的占用：当前分钟推进后，过去分钟不再参与后续编排。
    """
    offsets = _path_offsets(route)
    for e in route:
        entry = departure + offsets[e.id]
        for minute in range(max(entry, now), entry + e.travel_minutes):
            # 封闭检查
            for c in closures_by_edge.get(e.id, []):
                if c.start_minute <= minute < c.end_minute:
                    raise PlanningError(
                        f"组 {group_id} 的固定路线在第 {minute} 分钟占用通道 "
                        f"{e.id}，与封闭时段 [{c.start_minute},{c.end_minute}) 冲突"
                    )
            used = occupancy.setdefault(e.id, {}).get(minute, 0) + people
            if used > e.capacity_per_minute:
                raise PlanningError(
                    f"组 {group_id} 的固定路线在第 {minute} 分钟使通道 {e.id} "
                    f"占用 {used} 超过容量 {e.capacity_per_minute}"
                )
            occupancy[e.id][minute] = used


def closure_conflicts_with_fixed(
    closure: Closure,
    edges: dict[int, Edge],
    fixed: list[Group],
    now: int,
) -> int | None:
    """若封闭触及任一已出发组的剩余路线，返回冲突组编号；否则 None。"""
    for g in fixed:
        route = [edges[eid] for eid in g.route]
        offsets = _path_offsets(route)
        for e in route:
            entry = g.departure + offsets[e.id]
            end = entry + e.travel_minutes
            if e.id == closure.edge_id and end > now:
                # 剩余占用与左闭右开封闭相交
                if max(entry, now) < closure.end_minute and end > closure.start_minute:
                    return g.id
    return None


# ---------------------------------------------------------------- 单组编排

def _earliest_departure_for_path(
    route: list[Edge],
    group: Group,
    lower_bound: int,
    occupancy: dict[int, dict[int, int]],
    closures_by_edge: dict[int, list[Closure]],
    horizon: int,
) -> int | None:
    """给定路线，返回 [lower_bound, horizon] 内最早可行出发分钟；无则 None。

    horizon 为满足“到达 <= deadline”的最迟出发分钟。
    """
    offsets = _path_offsets(route)
    forbidden: list[tuple[int, int]] = []

    # 1) 封闭时段：组在分钟 minute 占用边 e ⇔ dep ∈ [minute-off-d+1, minute-off]
    #    封闭 [cs,ce) 与占用 [dep+off, dep+off+d) 相交的整数 dep 区间为
    #    [cs-d-off+1, ce-off-1]
    for e in route:
        off = offsets[e.id]
        for c in closures_by_edge.get(e.id, []):
            lo = c.start_minute - e.travel_minutes - off + 1
            hi = c.end_minute - off - 1
            s, t_end = max(lo, lower_bound), min(hi, horizon)
            if s <= t_end:
                forbidden.append((s, t_end))

    # 2) 容量：对已有占用的每个分钟，反推出发分钟禁入区间
    for e in route:
        off = offsets[e.id]
        minutes_used = occupancy.get(e.id, {})
        for minute, used in minutes_used.items():
            if used + group.people <= e.capacity_per_minute:
                continue
            if minute < lower_bound + off or minute >= horizon + off + e.travel_minutes:
                continue
            # 组在 minute 占用该边 ⇔ dep ∈ [minute-off-travel+1, minute-off]
            lo = minute - off - e.travel_minutes + 1
            hi = minute - off
            s, t_end = max(lo, lower_bound), min(hi, horizon)
            if s <= t_end:
                forbidden.append((s, t_end))

    merged = merge_intervals(forbidden)

    t = lower_bound
    for s, e in merged:
        if t < s:
            return t
        t = max(t, e + 1)
    if t <= horizon:
        return t
    return None


def schedule_group(
    group: Group,
    lower_bound: int,
    edges: dict[int, Edge],
    exits: dict[int, Exit],
    closures_by_edge: dict[int, list[Closure]],
    occupancy: dict[int, dict[int, int]],
    max_paths: int,
) -> None:
    """就地编排单个组：写入 route/departure/arrival/status/reason。"""
    group.route = []
    group.departure = None
    group.arrival = None
    group.reason = None

    if lower_bound > group.deadline:
        group.status = "blocked"
        group.reason = REASON_DEADLINE_PASSED
        return

    candidates, truncated = _enumerate_candidate_paths(
        group.source, edges, exits, max_paths
    )
    if truncated:
        # 路径枚举触达上限时结果可能不是最优；默认上限 10000，可配置调高
        raise PlanningError(
            f"组 {group.id} 的可行简单路径数超过上限 {max_paths}，"
            "请调高 EVAC_MAX_PATHS 后重试"
        )
    if not candidates:
        group.status = "blocked"
        group.reason = REASON_NO_PATH
        return

    best: tuple[int, int, tuple[int, ...], int, list[Edge]] | None = None
    for route, exit_ in candidates:
        # 组不可拆分：任一路段每分钟容量小于组人数，该路线恒不可行
        if any(e.capacity_per_minute < group.people for e in route):
            continue
        travel = _travel_time(route)
        horizon = group.deadline - travel
        if horizon < lower_bound:
            continue  # 即便立刻出发也赶不上截止
        dep = _earliest_departure_for_path(
            route, group, lower_bound, occupancy, closures_by_edge, horizon
        )
        if dep is None:
            continue
        key = (
            dep + travel,          # 1. 最早到达
            exit_.id,              # 2. 出口编号
            tuple(e.id for e in route),  # 3. 通道编号序列
            dep,                   # 4. 出发分钟
        )
        if best is None or key < best[:4]:
            best = (*key, route)  # type: ignore[assignment]

    if best is None:
        group.status = "blocked"
        group.reason = REASON_CAPACITY
        return

    arrival, _exit_id, _seq, departure, route = best
    group.route = [e.id for e in route]
    group.departure = departure
    group.arrival = arrival
    group.status = "scheduled"
    stamp_fixed_occupancy(
        route, departure, group.people, occupancy,
        now=lower_bound, closures_by_edge=closures_by_edge, group_id=group.id,
    )


# ---------------------------------------------------------------- 整体重排

def replan(
    edges: dict[int, Edge],
    exits: dict[int, Exit],
    closures: dict[int, Closure],
    groups: dict[int, Group],
    now: int,
    max_paths: int = 10000,
) -> list[Group]:
    """以 now 为当前分钟对全部组重排。

    已出发组（departure < now）路线与占用固定；其余组全部清空后按
    优先级 → 最早出发 → 编号 顺序重新贪心编排。返回 groups 列表。
    """
    validate_scenario(edges, exits, closures, groups)
    closures_by_edge: dict[int, list[Closure]] = {}
    for c in closures.values():
        closures_by_edge.setdefault(c.edge_id, []).append(c)

    occupancy: dict[int, dict[int, int]] = {}
    fixed = [g for g in groups.values()
             if g.status == "scheduled" and g.departure is not None and g.departure < now]
    fixed_ids = {g.id for g in fixed}
    candidates = [g for g in groups.values() if g.id not in fixed_ids]

    # 固定已出发组：路线占用不变（只登记 now 之后的部分）
    for g in sorted(fixed, key=lambda x: (x.departure, x.id)):
        route = [edges[eid] for eid in g.route]
        stamp_fixed_occupancy(
            route, g.departure, g.people, occupancy,
            now=now, closures_by_edge=closures_by_edge, group_id=g.id,
        )

    order = sorted(
        candidates,
        key=lambda g: (-g.priority, g.earliest_departure, g.id),
    )
    for g in order:
        schedule_group(
            g, lower_bound=max(now, g.earliest_departure),
            edges=edges, exits=exits,
            closures_by_edge=closures_by_edge,
            occupancy=occupancy, max_paths=max_paths,
        )
    return list(groups.values())
